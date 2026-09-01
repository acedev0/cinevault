import os
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cinevault.config import AppSecrets, SecretVault
from cinevault.cli import set_access_mode
from cinevault.database import Database, MediaRecord
from cinevault.indexer import MediaCatalog, record_from_message
from cinevault.security import hash_password, verify_password


def sample_config() -> AppSecrets:
    return AppSecrets(
        version=1,
        api_id=12345,
        api_hash="a" * 32,
        string_session="session-secret",
        bot_reference="@FilterSECBot",
        bot_id=777,
        bot_title="FilterSEC",
        account_name="Ace",
        web_username="ace",
        password_hash=hash_password("a-long-password"),
        cookie_secret="c" * 64,
        port=8765,
    )


def sample_record(message_id=10, dedupe_key="video:900") -> MediaRecord:
    return MediaRecord(
        chat_id=777,
        message_id=message_id,
        dedupe_key=dedupe_key,
        kind="video",
        title="Example video",
        filename="example.mp4",
        caption="caption",
        mime_type="video/mp4",
        size_bytes=4096,
        duration_seconds=42,
        width=1920,
        height=1080,
        message_date="2026-08-29T12:00:00+00:00",
    )


def movie_message(message_id=10, *, filename="Spider_Man_1994.mkv", document_id=900):
    document = SimpleNamespace(id=document_id, attributes=[])
    return SimpleNamespace(
        id=message_id,
        file=SimpleNamespace(
            mime_type="application/octet-stream",
            name=filename,
            width=1920,
            height=1080,
            duration=1320,
            size=465 * 1024 * 1024,
        ),
        photo=None,
        document=document,
        video=None,
        video_note=None,
        media=document,
        message="Spider Man episode",
        date=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
    )


def test_password_hash_and_verify():
    encoded = hash_password("a-long-password")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "a-long-password")
    assert not verify_password(encoded, "wrong-password")


def test_encrypted_vault_round_trip_and_permissions(tmp_path):
    vault = SecretVault(tmp_path / "state")
    original = sample_config()
    vault.save(original)
    restored = vault.load()
    assert restored.api_hash == original.api_hash
    assert restored.string_session == original.string_session
    assert restored.bot_reference == "@FilterSECBot"
    assert restored.port == 8765
    assert oct(os.stat(vault.key_path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(vault.secrets_path).st_mode & 0o777) == "0o600"
    assert b"session-secret" not in vault.secrets_path.read_bytes()


def test_access_mode_toggle_preserves_existing_credentials(tmp_path):
    vault = SecretVault(tmp_path / "state")
    original = sample_config()
    vault.save(original)
    set_access_mode(vault.data_dir, "public")
    updated = vault.load()
    assert updated.access_mode == "public"
    assert updated.api_hash == original.api_hash
    assert updated.string_session == original.string_session
    assert updated.password_hash == original.password_hash
    assert updated.port == original.port


def test_database_deduplicates_same_telegram_media(tmp_path):
    database = Database(tmp_path / "media.db")
    database.initialise()
    first_id, first_new, _ = database.upsert_media(sample_record(message_id=10))
    second_id, second_new, _ = database.upsert_media(sample_record(message_id=22))
    assert first_id == second_id
    assert first_new is True
    assert second_new is False
    assert database.stats() == {
        "total": 1,
        "videos": 1,
        "photos": 0,
        "audios": 0,
        "files": 0,
    }
    assert database.get_media(first_id)["message_id"] == 22


def test_database_search_random_and_deletion(tmp_path):
    database = Database(tmp_path / "media.db")
    database.initialise()
    for message_id in range(1, 91):
        database.upsert_media(
            replace(
                sample_record(),
                message_id=message_id,
                dedupe_key=f"video:{message_id}",
                title=f"Video {message_id:03d}",
            )
        )
    first, total = database.list_media(sort="random", seed=123, per_page=36, offset=0)
    second, _ = database.list_media(sort="random", seed=123, per_page=36, offset=36)
    repeated, _ = database.list_media(sort="random", seed=123, per_page=36, offset=0)
    assert total == 90
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})
    assert [row["id"] for row in first] == [row["id"] for row in repeated]
    deleted = database.delete_by_message_ids([1, 2])
    assert {row["message_id"] for row in deleted} == {1, 2}
    assert database.stats()["total"] == 88


def test_generic_mkv_document_is_recognised_as_video():
    record = record_from_message(movie_message(), 777)
    assert record is not None
    assert record.kind == "video"
    assert record.dedupe_key == "video:900"
    assert record.title == "Spider Man 1994"
    assert record.mime_type == "video/x-matroska"


def test_audio_and_general_documents_are_recognised():
    audio = record_from_message(movie_message(filename="Soundtrack.flac", document_id=901), 777)
    archive = record_from_message(movie_message(filename="Subtitles.ass", document_id=902), 777)
    assert audio is not None and audio.kind == "audio" and audio.mime_type == "audio/flac"
    assert archive is not None and archive.kind == "file"


def test_database_migrates_v1_kind_constraint_without_losing_ids(tmp_path):
    path = tmp_path / "legacy.db"
    database = Database(path)
    with database.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE media (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, dedupe_key TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('video', 'photo')), title TEXT NOT NULL,
                filename TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '', mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0, duration_seconds INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                message_date TEXT NOT NULL, thumbnail_filename TEXT, source_checked_at TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(chat_id, message_id), UNIQUE(chat_id, dedupe_key)
            );
            CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO media VALUES (
                7, 777, 70, 'video:70', 'video', 'Legacy', 'legacy.mp4', '', 'video/mp4',
                10, 1, 100, 100, '2026-01-01T00:00:00+00:00', NULL,
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            """
        )
    database.initialise()
    assert database.get_media(7)["title"] == "Legacy"
    audio_id, created, _ = database.upsert_media(
        replace(sample_record(), message_id=71, dedupe_key="audio:71", kind="audio")
    )
    assert created and database.get_media(audio_id)["kind"] == "audio"


@pytest.mark.asyncio
async def test_catalog_ingests_preview_and_prunes_deleted_source(tmp_path):
    database = Database(tmp_path / "media.db")
    database.initialise()

    class FakeTelegram:
        config = sample_config()

        async def build_thumbnail(self, _message, destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"preview")
            return True

        async def existing_message_ids(self, _message_ids):
            return set()

    catalog = MediaCatalog(database, FakeTelegram(), tmp_path / "thumbs")
    item = await catalog.ingest(movie_message())
    thumbnail = tmp_path / "thumbs" / item["thumbnail_filename"]
    assert thumbnail.read_bytes() == b"preview"
    result = await catalog.prune()
    assert result == replace(result, checked=1, removed=1)
    assert database.get_media(item["id"]) is None
    assert not thumbnail.exists()
