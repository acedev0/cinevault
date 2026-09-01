import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cinevault.film_bot import FilmBotClient
from pytests.test_core import sample_config


class Button:
    def __init__(self, text, data=b"opaque"):
        self.text = text
        self.data = data


class Message:
    def __init__(self, message_id, text="", buttons=None, *, out=False, media=None):
        self.id = message_id
        self.message = text
        self.buttons = buttons
        self.out = out
        self.media = media
        self.document = media
        self.photo = None
        self.clicked = None
        self.on_click = None

    async def click(self, i, j):
        self.clicked = (i, j)
        if self.on_click:
            await self.on_click(i, j)


class WireClient:
    def __init__(self):
        self.messages = []
        self.active_sends = 0
        self.max_active_sends = 0

    async def send_message(self, _entity, query):
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        await asyncio.sleep(0.01)
        sent = Message(len(self.messages) + 1, query, out=True)
        self.messages.append(sent)
        response = Message(
            sent.id + 1,
            "RESULTS FOUND",
            [
                [Button("LANGUAGE"), Button("RESOLUTION")],
                [Button("465MB - Spider Man 1994")],
                [Button("1/86"), Button("NEXT")],
            ],
        )
        self.messages.append(response)
        self.active_sends -= 1
        return sent

    async def get_messages(self, _entity, *, ids=None, limit=None, min_id=0):
        if ids is not None:
            if isinstance(ids, list):
                return [next((item for item in self.messages if item.id == value), None) for value in ids]
            return next((item for item in self.messages if item.id == ids), None)
        selected = [item for item in self.messages if item.id > min_id]
        selected.sort(key=lambda item: item.id, reverse=True)
        return selected[:limit]


def make_client(wire):
    client = FilmBotClient(replace(sample_config(), string_session=""))
    client.client = wire
    client.entity = SimpleNamespace(id=777, bot=True)
    return client


@pytest.mark.asyncio
async def test_search_classifies_bot_buttons_and_hides_callback_data():
    wire = WireClient()
    client = make_client(wire)
    page = await client.search("Spider Man", timeout=0.5)
    public = page.public()
    assert [action["role"] for action in public["actions"]] == [
        "filter", "filter", "result", "status", "navigation"
    ]
    assert b"opaque" not in repr(public).encode()


@pytest.mark.asyncio
async def test_page_callback_uses_exact_position_and_detects_edit():
    wire = WireClient()
    client = make_client(wire)
    page = await client.search("Spider Man", timeout=0.5)
    telegram_message = wire.messages[-1]

    async def edit_page(_row, _column):
        telegram_message.message = "RESULTS FOUND · PAGE 2"
        telegram_message.buttons = [[Button("464MB - Spider Man S05E08")], [Button("PREV")]]

    telegram_message.on_click = edit_page
    next_action = next(action for action in page.actions if action.text == "NEXT")
    outcome = await client.activate(page.token, next_action.token, timeout=0.5)
    assert telegram_message.clicked == (2, 1)
    assert outcome.kind == "page"
    assert outcome.page.public()["actions"][0]["text"].startswith("464MB")


@pytest.mark.asyncio
async def test_result_callback_waits_for_new_file_in_same_chat():
    wire = WireClient()
    client = make_client(wire)
    page = await client.search("Spider Man", timeout=0.5)
    telegram_message = wire.messages[-1]

    async def send_file(_row, _column):
        file_message = Message(telegram_message.id + 1, "Spider Man", media=SimpleNamespace(id=99))
        file_message.date = datetime.now(timezone.utc)
        wire.messages.append(file_message)

    telegram_message.on_click = send_file
    result_action = next(action for action in page.actions if action.role == "result")
    outcome = await client.activate(page.token, result_action.token, timeout=0.5)
    assert outcome.kind == "media"
    assert outcome.media_message.id == 3


@pytest.mark.asyncio
async def test_bot_searches_are_serialized():
    wire = WireClient()
    client = make_client(wire)
    first, second = await asyncio.gather(
        client.search("Spider Man", timeout=0.5),
        client.search("Dune 2021", timeout=0.5),
    )
    assert first.message_id != second.message_id
    assert wire.max_active_sends == 1
