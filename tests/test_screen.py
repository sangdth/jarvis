import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from jarvis.screen import KEEP, ScreenCapture, ScreenError, ask_screen, capture_front_window


def quartz(allowed: bool) -> SimpleNamespace:
    return SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: allowed)


class TestCapture(unittest.IsolatedAsyncioTestCase):
    async def test_no_permission_captures_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"Quartz": quartz(False)}):
            with self.assertRaises(ScreenError) as caught:
                await capture_front_window(Path(tmp) / "screen")
            self.assertEqual(caught.exception.status, "screen_permission_missing")
            self.assertFalse((Path(tmp) / "screen").exists())

    async def test_front_window_is_saved_private_and_old_ones_go(self) -> None:
        async def screencapture(*args: str):
            Path(args[-1]).write_bytes(b"png")
            return MagicMock(wait=AsyncMock(return_value=0), returncode=0)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(sys.modules, {"Quartz": quartz(True)}),
            patch("jarvis.screen._front_window", return_value=(42, "Terminal", "zsh")),
            patch("asyncio.create_subprocess_exec", side_effect=screencapture) as run,
        ):
            folder = Path(tmp) / "screen"
            shots = [await capture_front_window(folder) for _ in range(KEEP + 2)]
            self.assertEqual(run.call_args.args[:5], ("screencapture", "-x", "-o", "-l", "42"))
            self.assertEqual(sorted(folder.glob("*.png")), [s.path for s in shots[-KEEP:]])
            self.assertEqual(stat.S_IMODE(os.stat(shots[-1].path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
            self.assertEqual((shots[-1].app, shots[-1].title), ("Terminal", "zsh"))


def window_list(*windows: dict) -> SimpleNamespace:
    return SimpleNamespace(
        kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=16, kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda options, window_id: list(windows),
    )


class TestFrontWindow(unittest.TestCase):
    def test_the_top_visible_normal_window_wins(self) -> None:
        from jarvis.screen import _front_window

        windows = window_list(
            {"kCGWindowLayer": 25, "kCGWindowNumber": 1, "kCGWindowOwnerName": "Control Center"},  # menu bar
            {"kCGWindowLayer": 0, "kCGWindowAlpha": 0, "kCGWindowNumber": 2, "kCGWindowOwnerName": "Overlay"},
            {"kCGWindowLayer": 0, "kCGWindowAlpha": 1, "kCGWindowNumber": 3, "kCGWindowOwnerName": "Safari",
             "kCGWindowName": "Update"},
            {"kCGWindowLayer": 0, "kCGWindowAlpha": 1, "kCGWindowNumber": 4, "kCGWindowOwnerName": "Terminal"},
        )
        with patch.dict(sys.modules, {"Quartz": windows}):
            self.assertEqual(_front_window(), (3, "Safari", "Update"))

    def test_no_window_falls_back_to_the_main_display(self) -> None:
        from jarvis.screen import _front_window

        with patch.dict(sys.modules, {"Quartz": window_list()}):
            self.assertEqual(_front_window(), (None, "", ""))


class TestAsk(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.shot = ScreenCapture(Path("/x.png"), "Safari", "Update")

    async def post(self, response: httpx.Response) -> AsyncMock:
        post = AsyncMock(return_value=response)
        self.patches = [
            patch("jarvis.screen._jpeg_data_url", return_value="data:image/jpeg;base64,AA"),
            patch.object(httpx.AsyncClient, "post", post),
            patch("jarvis.screen.settings", openrouter_api_key="k", openrouter_base_url="https://o/api/v1",
                  executive_model="m"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        return post

    async def test_one_stateless_call_with_the_image(self) -> None:
        request = httpx.Request("POST", "https://o/api/v1/chat/completions")
        post = await self.post(httpx.Response(200, json={"choices": [{"message": {"content": " It's an update. "}}]},
                                              request=request))
        self.assertEqual(await ask_screen(self.shot, "What is this?"), "It's an update.")
        body = post.await_args.kwargs["json"]
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["messages"][0]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,AA")

    async def test_a_model_without_vision_says_so(self) -> None:
        request = httpx.Request("POST", "https://o/api/v1/chat/completions")
        await self.post(httpx.Response(400, json={"error": "no image input"}, request=request))
        with self.assertRaises(ScreenError) as caught:
            await ask_screen(self.shot, "What is this?")
        self.assertEqual(caught.exception.status, "model_has_no_vision")

    async def test_the_jpeg_is_downscaled(self) -> None:
        import base64
        import io

        from PIL import Image

        from jarvis.screen import VISION_SIZE, _jpeg_data_url

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.png"
            Image.new("RGBA", (3000, 1000)).save(path)
            data = base64.b64decode(_jpeg_data_url(path).split(",", 1)[1])
        self.assertEqual(Image.open(io.BytesIO(data)).size, (VISION_SIZE, 523))


if __name__ == "__main__":
    unittest.main()
