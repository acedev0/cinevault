from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .database import Database


class PosterFetcher:
    """Best-effort poster enrichment through a sandboxed headless Chromium worker."""

    def __init__(self, database: Database, project_dir: str | Path, thumbnail_dir: str | Path):
        self.database = database
        self.project_dir = Path(project_dir).resolve()
        self.thumbnail_dir = Path(thumbnail_dir).resolve()
        self.script = self.project_dir / "scripts" / "poster-fetcher.mjs"
        bundled_node = self.project_dir.parent / ".node" / "bin" / "node"
        self.node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
        self.puppeteer = self.project_dir / "node_modules" / "puppeteer-core"
        configured_browser = Path(os.environ.get("CINEVAULT_CHROMIUM", ""))
        browser_candidates = [
            configured_browser if str(configured_browser) != "." else Path("/nonexistent"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/google-chrome"),
        ]
        browser_root = self.project_dir.parent / ".browser"
        bundled_browsers = list(browser_root.rglob("chrome-headless-shell")) if browser_root.is_dir() else []
        self.browser = next(
            (str(path) for path in [*bundled_browsers, *browser_candidates] if path.is_file()),
            "",
        )
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(
            self.node and self.browser and self.script.is_file() and self.puppeteer.is_dir()
        )

    async def enrich_missing(self, limit: int = 40) -> int:
        if not self.available or self._lock.locked():
            return 0
        async with self._lock:
            candidates = self.database.poster_candidates(limit)
            if not candidates:
                return 0
            self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
            jobs = [
                {
                    "id": int(item["id"]),
                    "title": str(item["title"]),
                    "filename": str(item["filename"]),
                    "source": str(
                        self.thumbnail_dir / Path(str(item.get("thumbnail_filename") or "")).name
                    ) if item.get("thumbnail_filename") else "",
                    "output": str(self.thumbnail_dir / f"poster-{int(item['id'])}.webp"),
                }
                for item in candidates
            ]
            process = await asyncio.create_subprocess_exec(
                str(self.node),
                str(self.script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.project_dir),
                env={**os.environ, "CINEVAULT_CHROMIUM": self.browser},
            )
            payload = json.dumps(jobs, separators=(",", ":")).encode("utf-8")
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(payload), timeout=max(90, len(jobs) * 12)
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return 0
            if process.returncode != 0:
                return 0
            try:
                results: list[dict[str, Any]] = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                return 0
            updated = 0
            result_map = {int(item.get("id", -1)): item for item in results}
            for candidate in candidates:
                media_id = int(candidate["id"])
                result = result_map.get(media_id, {})
                destination = self.thumbnail_dir / f"poster-{media_id}.webp"
                if result.get("ok") and destination.is_file() and destination.stat().st_size > 0:
                    self.database.set_poster(media_id, destination.name)
                    updated += 1
                elif media_id in result_map:
                    destination.unlink(missing_ok=True)
                    self.database.set_poster(media_id, None)
            return updated
