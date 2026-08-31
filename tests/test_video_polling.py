import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gemini_webapi.types.video import GeneratedVideo, Video


class VideoPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_video_uses_the_requested_short_poll_interval(self):
        video = GeneratedVideo(url="https://example.invalid/video")
        with (
            patch.object(
                Video, "_download_file", new=AsyncMock(side_effect=["206", "C:/video.mp4"])
            ),
            patch("gemini_webapi.types.video.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = await video._perform_save(
                object(), Path("."), "video.mp4", False, poll_interval=0.25
            )

        self.assertEqual(result["video"], "C:/video.mp4")
        sleep.assert_awaited_once_with(0.25)
