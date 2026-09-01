import re
from dataclasses import replace
from urllib.parse import urlsplit
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cinevault.app import create_app
from cinevault.config import AppSecrets, SecretVault
from cinevault.database import Database, MediaRecord
from cinevault.film_bot import BotOutcome
from cinevault.security import hash_password


VIDEO = bytes(range(256)) * 16
PHOTO = b"photo-bytes"


class PublicPage:
    def public(self):
        return {
            "id": "page-token",
            "message": "RESULTS FOUND: Spider Man",
            "actions": [
                {"id": "filter", "text": "LANGUAGE", "role": "filter"},
                {"id": "movie", "text": "465MB - Spider Man 1994", "role": "result"},
                {"id": "next", "text": "NEXT", "role": "navigation"},
            ],
        }


def returned_movie(message_id=20):
    document = SimpleNamespace(id=2020, attributes=[])
    return SimpleNamespace(
        id=message_id,
        file=SimpleNamespace(
            mime_type="application/octet-stream",
            name="Spider_Man_1994.mkv",
            width=1920,
            height=1080,
            duration=1320,
            size=len(VIDEO),
        ),
        photo=None,
        document=document,
        video=None,
        video_note=None,
        media=document,
        message="Spider Man",
        date=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


class FakeTelegram:
    instances = []

    def __init__(self, config):
        self.config = config
        self.connected = False
        self.existing = {10, 11, 20}
        self.queries = []
        self.delete_callback = None
        FakeTelegram.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def set_delete_callback(self, callback):
        self.delete_callback = callback

    async def search(self, query):
        self.queries.append(query)
        return PublicPage()

    async def activate(self, page, action):
        if action == "filter":
            return BotOutcome(kind="page", page=PublicPage())
        return BotOutcome(kind="media", media_message=returned_movie())

    async def get_message(self, message_id):
        if message_id not in self.existing:
            return None
        if message_id == 20:
            return returned_movie()
        return SimpleNamespace(id=message_id, media=object(), photo=object() if message_id == 11 else None)

    async def stream(self, _message_id, start, end, _file_size):
        payload = VIDEO[start : end + 1]
        for index in range(0, len(payload), 137):
            yield payload[index : index + 137]

    async def download_photo(self, message_id):
        if message_id != 11 or message_id not in self.existing:
            raise FileNotFoundError
        return PHOTO

    async def build_thumbnail(self, _message, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"thumb")
        return True

    async def existing_message_ids(self, message_ids):
        return set(message_ids) & self.existing


def make_config(tmp_path):
    FakeTelegram.instances.clear()
    config = AppSecrets(
        version=1,
        api_id=12345,
        api_hash="a" * 32,
        string_session="session",
        bot_reference="@FilterSECBot",
        bot_id=777,
        bot_title="FilterSEC",
        account_name="Ace",
        web_username="ace",
        password_hash=hash_password("correct-password"),
        cookie_secret="cookie-secret-" * 5,
        port=8765,
        prune_interval_seconds=3600,
    )
    SecretVault(tmp_path).save(config)
    database = Database(tmp_path / "cinevault.db")
    database.initialise()
    database.upsert_media(
        MediaRecord(
            chat_id=777,
            message_id=10,
            dedupe_key="video:10",
            kind="video",
            title="Range test",
            filename="range.mp4",
            caption="",
            mime_type="video/mp4",
            size_bytes=len(VIDEO),
            duration_seconds=10,
            width=1280,
            height=720,
            message_date="2026-08-29T12:00:00+00:00",
        )
    )
    database.upsert_media(
        MediaRecord(
            chat_id=777,
            message_id=11,
            dedupe_key="photo:11",
            kind="photo",
            title="Photo test",
            filename="photo.jpg",
            caption="",
            mime_type="image/jpeg",
            size_bytes=len(PHOTO),
            duration_seconds=0,
            width=640,
            height=360,
            message_date="2026-08-29T12:01:00+00:00",
        )
    )


def extract_csrf(html):
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match
    return match.group(1)


def login(client):
    page = client.get("/login")
    token = extract_csrf(page.text)
    response = client.post(
        "/login",
        data={"username": "ace", "password": "correct-password", "csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_login_one_page_interface_and_security_headers(tmp_path):
    make_config(tmp_path)
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        login(client)
        response = client.get("/")
        assert response.status_code == 200
        assert "Find it. Stream it." in response.text
        assert "Search Telegram film bot" in response.text
        assert "Infinite" in response.text
        assert "Popular in your vault" in response.text
        assert 'aria-label="Pagination"' not in response.text
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_bot_search_and_result_capture(tmp_path):
    make_config(tmp_path)
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        login(client)
        home = client.get("/")
        csrf = extract_csrf(home.text)
        search = client.post("/api/bot/search", data={"csrf": csrf, "query": "Spider Man"})
        assert search.status_code == 200
        assert search.json()["page"]["actions"][1]["role"] == "result"
        assert FakeTelegram.instances[-1].queries == ["Spider Man"]

        capture = client.post(
            "/api/bot/action",
            data={"csrf": csrf, "page": "page-token", "action": "movie"},
        )
        assert capture.status_code == 200
        assert capture.json()["watch_url"] == "/media/3"
        watch = client.get(capture.json()["watch_url"])
        assert watch.status_code == 200
        assert "Browser compatibility" in watch.text
        assert "Original file" in watch.text
        assert "Spider Man 1994" in watch.text


def test_bot_endpoints_require_auth_and_csrf(tmp_path):
    make_config(tmp_path)
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        assert client.post("/api/bot/search", data={"query": "Spider Man"}).status_code == 401
        login(client)
        assert client.post("/api/bot/search", data={"query": "Spider Man"}).status_code == 403


def test_range_streaming_and_deleted_file_cleanup(tmp_path):
    make_config(tmp_path)
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        login(client)
        partial = client.get("/stream/1", headers={"Range": "bytes=100-299"})
        assert partial.status_code == 206
        assert partial.content == VIDEO[100:300]
        assert partial.headers["content-range"] == f"bytes 100-299/{len(VIDEO)}"
        assert partial.headers["accept-ranges"] == "bytes"

        telegram = FakeTelegram.instances[-1]
        telegram.existing.remove(10)
        removed = client.get("/stream/1")
        assert removed.status_code == 410
        assert Database(tmp_path / "cinevault.db").get_media(1) is None
        assert client.get("/media/1").status_code == 404


def test_player_shortcuts_and_original_download_disposition(tmp_path):
    make_config(tmp_path)
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        login(client)
        watch = client.get("/media/1")
        assert 'data-player-speed="2"' in watch.text
        assert "Keyboard shortcuts" in watch.text
        script = client.get("/static/js/player.js").text
        for shortcut in ('code === "ArrowLeft"', 'code === "ArrowRight"', 'code === "KeyN"', 'code === "KeyF"'):
            assert shortcut in script
        download = client.head("/stream/1?download=1")
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")


def test_signed_vlc_link_streams_without_login_cookie(tmp_path):
    make_config(tmp_path)
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        login(client)
        csrf = extract_csrf(client.get("/").text)
        created = client.post("/api/media/1/link", data={"csrf": csrf, "days": "7"})
        assert created.status_code == 200
        link = created.json()["url"]
        assert "/public/stream/1/" in link
        client.cookies.clear()
        path = urlsplit(link).path
        response = client.get(path, headers={"Range": "bytes=32-95"})
        assert response.status_code == 206
        assert response.content == VIDEO[32:96]
        assert response.headers["accept-ranges"] == "bytes"


def test_public_mode_skips_login_but_private_mode_does_not(tmp_path):
    make_config(tmp_path)
    vault = SecretVault(tmp_path)
    vault.save(replace(vault.load(), access_mode="public"))
    app = create_app(tmp_path, telegram_factory=FakeTelegram)
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/login", follow_redirects=False).status_code == 303
        page = client.get("/")
        assert ">public<" in page.text
        assert 'action="/logout"' not in page.text
