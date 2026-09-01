import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from cinevault.film_bot import FilmBotClient


@pytest.mark.asyncio
async def test_thumbnail_preserves_portrait_orientation_and_fits_bounds(tmp_path):
    source = Image.new("RGB", (900, 1600), (25, 80, 140))
    payload = io.BytesIO()
    source.save(payload, "JPEG")

    class FakeDownloadClient:
        async def download_media(self, *_args, **_kwargs):
            return payload.getvalue()

    client = object.__new__(FilmBotClient)
    client.thumbnail_semaphore = asyncio.Semaphore(1)
    client.client = FakeDownloadClient()
    destination = tmp_path / "portrait.webp"
    message = SimpleNamespace(photo=object())
    assert await client.build_thumbnail(message, destination)
    with Image.open(destination) as thumbnail:
        assert thumbnail.height > thumbnail.width
        assert max(thumbnail.size) <= 720


@pytest.mark.asyncio
async def test_thumbnail_failure_does_not_create_a_broken_file(tmp_path):
    class FakeDownloadClient:
        async def download_media(self, *_args, **_kwargs):
            return b"not-an-image"

    client = object.__new__(FilmBotClient)
    client.thumbnail_semaphore = asyncio.Semaphore(1)
    client.client = FakeDownloadClient()
    destination = tmp_path / "broken.webp"
    assert not await client.build_thumbnail(SimpleNamespace(photo=None), destination)
    assert not destination.exists()
