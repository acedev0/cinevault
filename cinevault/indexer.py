from __future__ import annotations

import asyncio
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

from .database import Database, MediaRecord
from .film_bot import FilmBotClient


VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".vob",
    ".webm", ".wmv",
}
AUDIO_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus",
    ".wav", ".wma",
}


@dataclass(frozen=True, slots=True)
class PruneResult:
    checked: int
    removed: int


def _normalise_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value[:180] or "Untitled media"


def record_from_message(message: Any, chat_id: int) -> MediaRecord | None:
    file = getattr(message, "file", None)
    photo = getattr(message, "photo", None)
    document = getattr(message, "document", None)
    mime_type = str(getattr(file, "mime_type", "") or "")
    filename = str(getattr(file, "name", "") or "")

    if document:
        for attribute in getattr(document, "attributes", []) or []:
            if isinstance(attribute, DocumentAttributeFilename) and not filename:
                filename = attribute.file_name or ""

    extension = Path(filename).suffix.casefold()
    is_video = bool(
        getattr(message, "video", None)
        or getattr(message, "video_note", None)
        or mime_type.startswith("video/")
        or extension in VIDEO_EXTENSIONS
    )
    is_audio = bool(
        getattr(message, "audio", None)
        or getattr(message, "voice", None)
        or mime_type.startswith("audio/")
        or extension in AUDIO_EXTENSIONS
    )
    if not photo and not document:
        return None

    kind = "photo" if photo else "video" if is_video else "audio" if is_audio else "file"
    media_object = photo or document
    media_identifier = getattr(media_object, "id", None)
    if media_identifier is None:
        return None
    dedupe_key = f"{kind}:{media_identifier}"

    width = int(getattr(file, "width", 0) or 0)
    height = int(getattr(file, "height", 0) or 0)
    duration = int(float(getattr(file, "duration", 0) or 0))
    if document:
        for attribute in getattr(document, "attributes", []) or []:
            if isinstance(attribute, DocumentAttributeVideo):
                duration = int(float(attribute.duration or duration or 0))
                width = int(attribute.w or width or 0)
                height = int(attribute.h or height or 0)
            elif isinstance(attribute, DocumentAttributeAudio):
                duration = int(float(attribute.duration or duration or 0))
    elif photo:
        sizes = [size for size in getattr(photo, "sizes", []) or [] if hasattr(size, "w")]
        if sizes:
            largest = max(
                sizes,
                key=lambda item: int(getattr(item, "w", 0)) * int(getattr(item, "h", 0)),
            )
            width = int(getattr(largest, "w", 0) or 0)
            height = int(getattr(largest, "h", 0) or 0)

    caption = str(getattr(message, "message", "") or "").strip()
    if filename:
        title = _normalise_title(Path(filename).stem.replace("_", " ").replace("-", " "))
    elif caption:
        title = _normalise_title(caption.splitlines()[0])
    else:
        title = f"{kind.title()} {message.id}"
    if not filename:
        filename = f"{kind}-{message.id}{'.jpg' if kind == 'photo' else '.mp4'}"

    date = getattr(message, "date", None) or datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    size_bytes = int(getattr(file, "size", 0) or 0)
    if kind == "photo" and not size_bytes:
        size_bytes = max(
            (int(getattr(item, "size", 0) or 0) for item in getattr(photo, "sizes", []) or []),
            default=0,
        )

    resolved_mime = mime_type
    if not resolved_mime or resolved_mime == "application/octet-stream":
        resolved_mime = {
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".m4v": "video/mp4",
            ".mp4": "video/mp4",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
        }.get(extension) or mimetypes.guess_type(filename)[0] or resolved_mime

    return MediaRecord(
        chat_id=int(chat_id),
        message_id=int(message.id),
        dedupe_key=dedupe_key,
        kind=kind,
        title=title,
        filename=filename[:255],
        caption=caption[:4000],
        mime_type=resolved_mime or (
            "image/jpeg" if kind == "photo" else
            "video/mp4" if kind == "video" else
            "audio/mpeg" if kind == "audio" else
            "application/octet-stream"
        ),
        size_bytes=max(0, size_bytes),
        duration_seconds=max(0, duration),
        width=max(0, width),
        height=max(0, height),
        message_date=date.astimezone(timezone.utc).isoformat(),
    )


class MediaCatalog:
    def __init__(
        self,
        database: Database,
        telegram: FilmBotClient,
        thumbnail_dir: str | Path,
    ):
        self.database = database
        self.telegram = telegram
        self.thumbnail_dir = Path(thumbnail_dir).resolve()
        self._prune_lock = asyncio.Lock()

    def _remove_thumbnail(self, filename: str | None) -> None:
        if not filename:
            return
        path = self.thumbnail_dir / Path(filename).name
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    async def ingest(self, message: Any) -> dict[str, Any]:
        record = record_from_message(message, self.telegram.config.bot_id)
        if record is None:
            raise ValueError("The bot response does not contain a supported media file.")
        media_id, _, thumbnail_filename = self.database.upsert_media(record)
        destination = self.thumbnail_dir / f"{media_id}.webp"
        if not thumbnail_filename or not destination.is_file():
            if await self.telegram.build_thumbnail(message, destination):
                self.database.set_thumbnail(media_id, destination.name)
        item = self.database.get_media(media_id)
        if item is None:
            raise RuntimeError("The captured Telegram file could not be indexed.")
        return item

    async def purge_media(self, media_id: int) -> bool:
        deleted = self.database.delete_media(media_id)
        if not deleted:
            return False
        self._remove_thumbnail(deleted.get("thumbnail_filename"))
        self._remove_thumbnail(deleted.get("poster_filename"))
        return True

    async def purge_messages(self, message_ids: list[int]) -> int:
        removed = self.database.delete_by_message_ids(message_ids)
        for item in removed:
            self._remove_thumbnail(item.get("thumbnail_filename"))
            self._remove_thumbnail(item.get("poster_filename"))
        return len(removed)

    async def prune(self, limit: int = 100) -> PruneResult:
        if self._prune_lock.locked():
            return PruneResult(checked=0, removed=0)
        async with self._prune_lock:
            candidates = self.database.reconciliation_candidates(limit)
            message_ids = [int(item["message_id"]) for item in candidates]
            if not message_ids:
                return PruneResult(checked=0, removed=0)
            existing = await self.telegram.existing_message_ids(message_ids)
            missing = [message_id for message_id in message_ids if message_id not in existing]
            self.database.mark_source_checked(sorted(existing))
            removed = await self.purge_messages(missing)
            self.database.set_meta("last_prune_at", datetime.now(timezone.utc).isoformat())
            return PruneResult(checked=len(message_ids), removed=removed)
