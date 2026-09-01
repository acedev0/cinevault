from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MediaRecord:
    chat_id: int
    message_id: int
    dedupe_key: str
    kind: str
    title: str
    filename: str
    caption: str
    mime_type: str
    size_bytes: int
    duration_seconds: int
    width: int
    height: int
    message_date: str


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialise(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('video', 'photo', 'audio', 'file')),
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    message_date TEXT NOT NULL,
                    thumbnail_filename TEXT,
                    poster_filename TEXT,
                    poster_checked_at TEXT,
                    source_checked_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id),
                    UNIQUE(chat_id, dedupe_key)
                );
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            table_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media'"
            ).fetchone()
            table_sql = str(table_sql_row[0] or "") if table_sql_row else ""
            pre_migration_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(media)").fetchall()
            }
            if "source_checked_at" not in pre_migration_columns:
                connection.execute(
                    "ALTER TABLE media ADD COLUMN source_checked_at TEXT NOT NULL DEFAULT ''"
                )
            if "'audio'" not in table_sql or "'file'" not in table_sql:
                # SQLite cannot extend a CHECK constraint in place. Rebuild only
                # the metadata table, retaining ids and every existing record.
                connection.executescript(
                    """
                    DROP INDEX IF EXISTS idx_media_date;
                    DROP INDEX IF EXISTS idx_media_kind_date;
                    DROP INDEX IF EXISTS idx_media_title;
                    DROP INDEX IF EXISTS idx_media_source_checked;
                    CREATE TABLE media_upgrade (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        dedupe_key TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('video', 'photo', 'audio', 'file')),
                        title TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        caption TEXT NOT NULL DEFAULT '',
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0,
                        duration_seconds INTEGER NOT NULL DEFAULT 0,
                        width INTEGER NOT NULL DEFAULT 0,
                        height INTEGER NOT NULL DEFAULT 0,
                        message_date TEXT NOT NULL,
                        thumbnail_filename TEXT,
                        poster_filename TEXT,
                        poster_checked_at TEXT,
                        source_checked_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(chat_id, message_id),
                        UNIQUE(chat_id, dedupe_key)
                    );
                    INSERT INTO media_upgrade (
                        id, chat_id, message_id, dedupe_key, kind, title, filename,
                        caption, mime_type, size_bytes, duration_seconds, width,
                        height, message_date, thumbnail_filename, source_checked_at,
                        created_at, updated_at
                    )
                    SELECT id, chat_id, message_id, dedupe_key, kind, title, filename,
                           caption, mime_type, size_bytes, duration_seconds, width,
                           height, message_date, thumbnail_filename, source_checked_at,
                           created_at, updated_at
                    FROM media;
                    DROP TABLE media;
                    ALTER TABLE media_upgrade RENAME TO media;
                    """
                )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(media)").fetchall()
            }
            if "source_checked_at" not in columns:
                connection.execute(
                    "ALTER TABLE media ADD COLUMN source_checked_at TEXT NOT NULL DEFAULT ''"
                )
            if "poster_filename" not in columns:
                connection.execute("ALTER TABLE media ADD COLUMN poster_filename TEXT")
            if "poster_checked_at" not in columns:
                connection.execute("ALTER TABLE media ADD COLUMN poster_checked_at TEXT")
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_media_date
                    ON media(message_date DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_media_kind_date
                    ON media(kind, message_date DESC);
                CREATE INDEX IF NOT EXISTS idx_media_title
                    ON media(title COLLATE NOCASE);
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_source_checked "
                "ON media(source_checked_at ASC, id ASC)"
            )
            connection.execute("PRAGMA optimize")

    def upsert_media(self, record: MediaRecord) -> tuple[int, bool, str | None]:
        now = datetime.now(timezone.utc).isoformat()
        values = asdict(record)
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id, thumbnail_filename FROM media WHERE chat_id = ? AND dedupe_key = ?",
                (record.chat_id, record.dedupe_key),
            ).fetchone()
            connection.execute(
                "DELETE FROM media WHERE chat_id = ? AND message_id = ? AND dedupe_key <> ?",
                (record.chat_id, record.message_id, record.dedupe_key),
            )
            if existing:
                connection.execute(
                    """
                    UPDATE media SET
                        message_id = :message_id,
                        kind = :kind,
                        title = :title,
                        filename = :filename,
                        caption = :caption,
                        mime_type = :mime_type,
                        size_bytes = :size_bytes,
                        duration_seconds = :duration_seconds,
                        width = :width,
                        height = :height,
                        message_date = :message_date,
                        source_checked_at = :source_checked_at,
                        updated_at = :updated_at
                    WHERE chat_id = :chat_id AND dedupe_key = :dedupe_key
                    """,
                    {**values, "source_checked_at": now, "updated_at": now},
                )
                return int(existing["id"]), False, existing["thumbnail_filename"]

            cursor = connection.execute(
                """
                INSERT INTO media (
                    chat_id, message_id, dedupe_key, kind, title, filename,
                    caption, mime_type, size_bytes, duration_seconds, width,
                    height, message_date, source_checked_at, created_at, updated_at
                ) VALUES (
                    :chat_id, :message_id, :dedupe_key, :kind, :title, :filename,
                    :caption, :mime_type, :size_bytes, :duration_seconds, :width,
                    :height, :message_date, :source_checked_at, :created_at, :updated_at
                )
                """,
                {
                    **values,
                    "source_checked_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return int(cursor.lastrowid), True, None

    def mark_source_checked(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.executemany(
                "UPDATE media SET source_checked_at = ? WHERE message_id = ?",
                [(now, int(message_id)) for message_id in message_ids],
            )

    def reconciliation_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, message_id, thumbnail_filename, poster_filename FROM media "
                "ORDER BY source_checked_at ASC, id ASC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_media(self, media_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, message_id, thumbnail_filename, poster_filename FROM media WHERE id = ?",
                (int(media_id),),
            ).fetchone()
            if row:
                connection.execute("DELETE FROM media WHERE id = ?", (int(media_id),))
        return dict(row) if row else None

    def delete_by_message_ids(self, message_ids: list[int]) -> list[dict[str, Any]]:
        normalised = sorted({int(value) for value in message_ids})
        if not normalised:
            return []
        placeholders = ",".join("?" for _ in normalised)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT id, message_id, thumbnail_filename, poster_filename FROM media "
                f"WHERE message_id IN ({placeholders})",
                normalised,
            ).fetchall()
            connection.execute(
                f"DELETE FROM media WHERE message_id IN ({placeholders})",
                normalised,
            )
        return [dict(row) for row in rows]

    def set_thumbnail(self, media_id: int, filename: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE media SET thumbnail_filename = ?, updated_at = ? WHERE id = ?",
                (filename, datetime.now(timezone.utc).isoformat(), media_id),
            )

    def set_poster(self, media_id: int, filename: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection:
            connection.execute(
                "UPDATE media SET poster_filename = ?, poster_checked_at = ?, "
                "updated_at = ? WHERE id = ?",
                (filename, now, now, int(media_id)),
            )

    def poster_candidates(self, limit: int = 40) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, title, filename, thumbnail_filename FROM media "
                "WHERE kind = 'video' AND poster_filename IS NULL "
                "AND poster_checked_at IS NULL ORDER BY message_date DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_media(self, media_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        return dict(row) if row else None

    def get_by_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM media WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def list_media(
        self,
        *,
        query: str = "",
        kind: str = "all",
        page: int = 1,
        per_page: int = 36,
        sort: str = "newest",
        offset: int | None = None,
        seed: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if kind in {"video", "photo", "audio", "file"}:
            clauses.append("kind = ?")
            parameters.append(kind)
        if query.strip():
            term = f"%{query.strip()}%"
            clauses.append("(title LIKE ? OR filename LIKE ? OR caption LIKE ?)")
            parameters.extend((term, term, term))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = {
            "oldest": "message_date ASC, id ASC",
            "name": "title COLLATE NOCASE ASC, id DESC",
            "largest": "size_bytes DESC, id DESC",
        }.get(sort, "message_date DESC, id DESC")
        resolved_offset = (
            max(0, int(offset)) if offset is not None else max(0, page - 1) * per_page
        )
        with self.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM media {where}", parameters
                ).fetchone()[0]
            )
            if sort == "random":
                # Hashing each stable database id with a session seed produces one
                # deterministic permutation. Offset batches therefore never repeat
                # or reshuffle items while the user scrolls.
                matching_rows = connection.execute(
                    f"SELECT * FROM media {where} ORDER BY id ASC", parameters
                ).fetchall()
                normalised_seed = max(0, min(int(seed), 2_147_483_647))

                def random_key(row: sqlite3.Row) -> bytes:
                    value = f"{normalised_seed}:{int(row['id'])}".encode("ascii")
                    return hashlib.blake2b(value, digest_size=16).digest()

                matching_rows = sorted(matching_rows, key=random_key)
                rows = matching_rows[resolved_offset : resolved_offset + per_page]
            else:
                rows = connection.execute(
                    f"SELECT * FROM media {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
                    (*parameters, per_page, resolved_offset),
                ).fetchall()
        return [dict(row) for row in rows], total

    def playlist(
        self,
        *,
        query: str = "",
        kind: str = "video",
        sort: str = "newest",
        seed: int = 0,
    ) -> list[dict[str, Any]]:
        """Return a complete, ordered metadata-only playlist for watch navigation."""
        _, total = self.list_media(
            query=query,
            kind=kind,
            per_page=1,
            sort=sort,
            seed=seed,
        )
        if total == 0:
            return []
        rows, _ = self.list_media(
            query=query,
            kind=kind,
            per_page=total,
            sort=sort,
            seed=seed,
        )
        return rows

    def related(self, media_id: int, kind: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM media
                WHERE id <> ?
                ORDER BY CASE WHEN kind = ? THEN 0 ELSE 1 END,
                         message_date DESC, id DESC
                LIMIT ?
                """,
                (media_id, kind, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN kind = 'video' THEN 1 ELSE 0 END) AS videos,
                       SUM(CASE WHEN kind = 'photo' THEN 1 ELSE 0 END) AS photos,
                       SUM(CASE WHEN kind = 'audio' THEN 1 ELSE 0 END) AS audios,
                       SUM(CASE WHEN kind = 'file' THEN 1 ELSE 0 END) AS files
                FROM media
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "videos": int(row["videos"] or 0),
            "photos": int(row["photos"] or 0),
            "audios": int(row["audios"] or 0),
            "files": int(row["files"] or 0),
        }

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else default
