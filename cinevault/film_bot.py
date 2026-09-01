from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import io
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from PIL import Image, ImageOps
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

from .config import AppSecrets


class TelegramUnavailable(RuntimeError):
    pass


class BotRequestError(RuntimeError):
    pass


DeleteCallback = Callable[[list[int]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class BotAction:
    token: str
    text: str
    role: str
    row: int
    column: int

    def public(self) -> dict[str, str]:
        return {"id": self.token, "text": self.text, "role": self.role}


@dataclass(slots=True)
class BotPage:
    token: str
    message_id: int
    text: str
    actions: list[BotAction]
    fingerprint: str
    created_at: float

    def public(self) -> dict[str, Any]:
        return {
            "id": self.token,
            "message": self.text,
            "actions": [action.public() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class BotOutcome:
    kind: str
    page: BotPage | None = None
    media_message: Any | None = None


def _button_role(text: str) -> str:
    folded = re.sub(r"\s+", " ", text).strip().casefold()
    if not folded:
        return "action"
    if re.search(r"(?:^|\s)(?:next|previous|prev|back)(?:\s|$)", folded):
        return "navigation"
    if re.fullmatch(r"[^\d]*\d+\s*/\s*\d+[^\d]*", folded):
        return "status"
    if any(value in folded for value in ("language", "resolution", "category", "quality", "genre")):
        return "filter"
    return "result"


def _message_fingerprint(message: Any) -> str:
    layout: list[str] = []
    for row in getattr(message, "buttons", None) or []:
        layout.append("\x1f".join(str(getattr(button, "text", "") or "") for button in row))
    raw = f"{int(getattr(message, 'id', 0))}\x1e{getattr(message, 'message', '')}\x1e{'|'.join(layout)}"
    return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=16).hexdigest()


def _is_media_message(message: Any) -> bool:
    return bool(
        message
        and not getattr(message, "out", False)
        and (getattr(message, "document", None) or getattr(message, "photo", None))
    )


class FilmBotClient:
    """Serialized user-account adapter for a third-party Telegram search bot.

    Callback data never leaves the server. Browser clients receive short random
    tokens that map back to an exact Telegram message and button position.
    """

    def __init__(self, config: AppSecrets, max_streams: int = 4):
        self.config = config
        self.client = TelegramClient(
            StringSession(config.string_session),
            config.api_id,
            config.api_hash,
            sequential_updates=True,
            auto_reconnect=True,
            connection_retries=5,
            request_retries=5,
            retry_delay=2,
            flood_sleep_threshold=60,
        )
        self.entity: Any | None = None
        self.stream_semaphore = asyncio.Semaphore(max_streams)
        self.thumbnail_semaphore = asyncio.Semaphore(4)
        self.transaction_lock = asyncio.Lock()
        self._pages: dict[str, BotPage] = {}
        self._delete_callback: DeleteCallback | None = None
        self._delete_event: Any | None = None

    async def connect(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise TelegramUnavailable(
                "The Telegram session is no longer authorised. Run CineVault setup again."
            )
        try:
            self.entity = await self.client.get_entity(self.config.bot_reference)
        except Exception:
            self.entity = await self.client.get_entity(self.config.bot_id)
        if not bool(getattr(self.entity, "bot", False)):
            raise TelegramUnavailable("The configured Telegram account is not a bot.")
        self._delete_event = events.MessageDeleted(chats=self.entity)
        self.client.add_event_handler(self._on_messages_deleted, self._delete_event)

    async def disconnect(self) -> None:
        if self._delete_event is not None:
            self.client.remove_event_handler(self._on_messages_deleted, self._delete_event)
        await self.client.disconnect()

    def set_delete_callback(self, callback: DeleteCallback) -> None:
        self._delete_callback = callback

    async def _on_messages_deleted(self, event: Any) -> None:
        if self._delete_callback is None:
            return
        result = self._delete_callback([int(value) for value in event.deleted_ids])
        if inspect.isawaitable(result):
            await result

    def _require_entity(self) -> Any:
        if self.entity is None:
            raise TelegramUnavailable("Telegram is not connected.")
        return self.entity

    async def _recent(self, *, after_id: int = 0, limit: int = 30) -> list[Any]:
        messages = await self.client.get_messages(
            self._require_entity(), limit=limit, min_id=max(0, int(after_id))
        )
        return sorted(
            [message for message in messages if message and not getattr(message, "out", False)],
            key=lambda message: int(message.id),
        )

    async def _latest_id(self) -> int:
        messages = await self.client.get_messages(self._require_entity(), limit=1)
        return int(messages[0].id) if messages else 0

    def _clean_pages(self) -> None:
        cutoff = time.monotonic() - 15 * 60
        self._pages = {
            token: page for token, page in self._pages.items() if page.created_at >= cutoff
        }
        if len(self._pages) > 200:
            newest = sorted(self._pages.values(), key=lambda page: page.created_at)[-200:]
            self._pages = {page.token: page for page in newest}

    def _store_page(self, message: Any) -> BotPage:
        actions: list[BotAction] = []
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for column_index, button in enumerate(row):
                text = str(getattr(button, "text", "") or "").strip()
                if not text or getattr(button, "data", None) is None:
                    continue
                actions.append(
                    BotAction(
                        token=secrets.token_urlsafe(18),
                        text=text[:180],
                        role=_button_role(text),
                        row=row_index,
                        column=column_index,
                    )
                )
        if not actions:
            raise BotRequestError("The bot response did not contain usable result buttons.")
        self._clean_pages()
        page = BotPage(
            token=secrets.token_urlsafe(18),
            message_id=int(message.id),
            text=str(getattr(message, "message", "") or "")[:5000],
            actions=actions,
            fingerprint=_message_fingerprint(message),
            created_at=time.monotonic(),
        )
        self._pages[page.token] = page
        return page

    async def _wait_for_search_page(self, after_id: int, timeout: float) -> BotPage:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in reversed(await self._recent(after_id=after_id)):
                if getattr(message, "buttons", None):
                    return self._store_page(message)
            await asyncio.sleep(0.45)
        raise BotRequestError("The bot did not return search buttons before the timeout.")

    async def search(self, query: str, timeout: float = 45.0) -> BotPage:
        cleaned = re.sub(r"\s+", " ", query).strip()
        if not 2 <= len(cleaned) <= 120:
            raise BotRequestError("Enter a search between 2 and 120 characters.")
        async with self.transaction_lock:
            sent = await self.client.send_message(self._require_entity(), cleaned)
            return await self._wait_for_search_page(int(sent.id), timeout)

    async def activate(
        self,
        page_token: str,
        action_token: str,
        timeout: float = 60.0,
    ) -> BotOutcome:
        async with self.transaction_lock:
            self._clean_pages()
            page = self._pages.get(page_token)
            if page is None:
                raise BotRequestError("These search results expired. Search again.")
            action = next((item for item in page.actions if item.token == action_token), None)
            if action is None:
                raise BotRequestError("That result button is no longer valid.")
            message = await self.client.get_messages(
                self._require_entity(), ids=page.message_id
            )
            if not message or _message_fingerprint(message) != page.fingerprint:
                raise BotRequestError("The bot changed these results. Search again.")
            rows = getattr(message, "buttons", None) or []
            if action.row >= len(rows) or action.column >= len(rows[action.row]):
                raise BotRequestError("The selected bot button no longer exists.")
            button = rows[action.row][action.column]
            if getattr(button, "data", None) is None:
                raise BotRequestError("External, payment, and sharing buttons are not supported.")

            baseline = await self._latest_id()
            await message.click(i=action.row, j=action.column)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                recent = await self._recent(after_id=baseline)
                media_candidates = [candidate for candidate in recent if _is_media_message(candidate)]
                if media_candidates:
                    return BotOutcome(kind="media", media_message=media_candidates[-1])
                page_candidates = [
                    candidate for candidate in recent if getattr(candidate, "buttons", None)
                ]
                if page_candidates:
                    return BotOutcome(kind="page", page=self._store_page(page_candidates[-1]))

                refreshed = await self.client.get_messages(
                    self._require_entity(), ids=page.message_id
                )
                if refreshed:
                    fingerprint = _message_fingerprint(refreshed)
                    if fingerprint != page.fingerprint and getattr(refreshed, "buttons", None):
                        return BotOutcome(kind="page", page=self._store_page(refreshed))
                await asyncio.sleep(0.45)
            raise BotRequestError("The bot did not return a file or another result page in time.")

    async def get_message(self, message_id: int):
        message = await self.client.get_messages(self._require_entity(), ids=int(message_id))
        if not message or not getattr(message, "media", None):
            return None
        return message

    async def existing_message_ids(self, message_ids: list[int]) -> set[int]:
        if not message_ids:
            return set()
        messages = await self.client.get_messages(
            self._require_entity(), ids=[int(value) for value in message_ids]
        )
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        return {
            int(message.id)
            for message in messages
            if message and getattr(message, "media", None)
        }

    async def download_photo(self, message_id: int) -> bytes:
        async with self.stream_semaphore:
            message = await self.get_message(message_id)
            if message is None or not getattr(message, "photo", None):
                raise FileNotFoundError("The Telegram photo no longer exists.")
            raw = await self.client.download_media(message, file=bytes)
            if not raw:
                raise FileNotFoundError("Telegram returned an empty photo.")
            return bytes(raw)

    async def stream(
        self,
        message_id: int,
        start: int,
        end: int,
        file_size: int,
    ) -> AsyncIterator[bytes]:
        async with self.stream_semaphore:
            message = await self.get_message(message_id)
            if message is None:
                raise FileNotFoundError("The Telegram media no longer exists.")
            remaining = end - start + 1
            iterator = self.client.iter_download(
                message.media,
                offset=start,
                request_size=512 * 1024,
                chunk_size=512 * 1024,
                file_size=file_size,
            )
            async for chunk in iterator:
                if not chunk or remaining <= 0:
                    break
                selected = bytes(chunk[:remaining])
                if selected:
                    remaining -= len(selected)
                    yield selected
                if remaining <= 0:
                    break

    async def build_thumbnail(self, message: Any, destination: Path) -> bool:
        """Store only Telegram's small preview, never the original film."""
        async with self.thumbnail_semaphore:
            try:
                if getattr(message, "photo", None):
                    raw = await self.client.download_media(message, file=bytes)
                else:
                    raw = await self.client.download_media(message, file=bytes, thumb=-1)
            except Exception:
                return False
            if not raw:
                return False
            try:
                with Image.open(io.BytesIO(raw)) as source:
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    image.thumbnail((720, 720), Image.Resampling.LANCZOS)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    image.save(destination, "WEBP", quality=82, method=4)
                    destination.chmod(0o600)
            except (OSError, ValueError):
                return False
            return True

    async def account_display_name(self) -> str:
        me = await self.client.get_me()
        first = getattr(me, "first_name", "") or ""
        last = getattr(me, "last_name", "") or ""
        username = getattr(me, "username", "") or ""
        return " ".join(part for part in (first, last) if part).strip() or username or "Telegram account"

    async def current_bot_id(self) -> int:
        return int(utils.get_peer_id(self._require_entity()))
