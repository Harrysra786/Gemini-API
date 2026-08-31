import asyncio
import unittest
from unittest.mock import AsyncMock

from gemini_webapi.types.image import _fetch_bytes_resilient


class ImageDownloadBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_fetch_has_a_short_timeout(self):
        async def hang(*_args, **_kwargs):
            await asyncio.sleep(60)

        client = type("Client", (), {"get": AsyncMock(side_effect=hang)})()
        started = asyncio.get_running_loop().time()
        content, content_type = await _fetch_bytes_resilient(
            "https://example.invalid/image", client, request_timeout=0.01
        )
        elapsed = asyncio.get_running_loop().time() - started
        self.assertIsNone(content)
        self.assertIsNone(content_type)
        self.assertLess(elapsed, 0.5)
