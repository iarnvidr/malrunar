# SPDX-License-Identifier: Apache-2.0
"""Focused regressions for daily b-roll MP4 generation."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from malrunar_obs.daily_video import assemble  # noqa: E402


class BrollGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_assembled_video_uses_yuv420p(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "chunk.ts"
            source.write_bytes(b"source")
            output = directory / "daily.mp4"

            async def fake_ffmpeg_output(*args):
                # assemble() atomically promotes FFmpeg's partial output on success.
                Path(args[-1]).write_bytes(b"assembled video")

            with mock.patch("malrunar_obs.daily_video._ffmpeg_output", new_callable=mock.AsyncMock) as ffmpeg:
                ffmpeg.side_effect = fake_ffmpeg_output
                result = await assemble([(source, [(0, 10)])], output, target_seconds=5)

            self.assertTrue(result["ready"])
            self.assertTrue(output.is_file())
            command = ffmpeg.await_args.args
            self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")


if __name__ == "__main__":
    unittest.main()
