"""The user's frontmost window, captured only when they point at it ("this error", "on my screen")."""

from __future__ import annotations

import asyncio
import base64
import io
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger

from jarvis.config import JARVIS_HOME, settings

# Only the user can read it. A dev checkout keeps JARVIS_HOME in the repo, so `.gitignore` lists `/screen/`.
SCREEN_DIR = JARVIS_HOME / "screen"
# An agent may still be reading the previous one when the next task comes.
KEEP = 5
# The long side sent to the vision model; past this, text gets no easier to read and the upload grows.
VISION_SIZE = 1568


class ScreenError(Exception):
    """A capture or a look that failed; `status` is what the tool returns to the conversation model."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class ScreenCapture:
    path: Path
    app: str
    title: str


def _front_window() -> tuple[int | None, str, str]:
    """The top visible normal window: (window id or None, app name, window title)."""
    import Quartz

    # The list runs front to back. NSWorkspace's front app is no use here: it updates only on a Cocoa run loop, and
    # Jarvis runs asyncio, so it would keep naming the app that was in front at the first look.
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if window.get("kCGWindowLayer") == 0 and window.get("kCGWindowAlpha", 1) > 0:
            name, title = str(window.get("kCGWindowOwnerName") or ""), str(window.get("kCGWindowName") or "")
            return int(window["kCGWindowNumber"]), name, title
    return None, "", ""  # Mission Control, a locked screen: the main display instead


async def capture_front_window(folder: Path = SCREEN_DIR) -> ScreenCapture:
    """Save the frontmost window as a PNG only the user can read; keep the newest KEEP. Raises ScreenError."""
    import Quartz

    # Without Screen Recording permission macOS returns only the wallpaper; say so instead of sending that.
    if not Quartz.CGPreflightScreenCaptureAccess():
        logger.warning("[Screen] No Screen Recording permission: allow this terminal in System Settings > Privacy.")
        raise ScreenError("screen_permission_missing")
    window, app, title = _front_window()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}.png"
    # -x: no shutter sound, -o: no shadow; -l one window, else the main display.
    target = ["-o", "-l", str(window)] if window is not None else ["-m"]
    proc = await asyncio.create_subprocess_exec("screencapture", "-x", *target, str(path))
    if await proc.wait() != 0 or not path.exists():
        logger.warning(f"[Screen] screencapture failed (exit {proc.returncode}).")
        raise ScreenError("screen_failed")
    os.chmod(path, 0o600)
    for old in sorted(folder.glob("*.png"))[:-KEEP]:  # names sort by time
        old.unlink(missing_ok=True)
    logger.info(f"[Screen] captured {app!r} {title!r} to {path.name}")
    return ScreenCapture(path=path, app=app, title=title)


def _jpeg_data_url(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as png:
        image = png.convert("RGB")
    image.thumbnail((VISION_SIZE, VISION_SIZE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


async def ask_screen(capture: ScreenCapture, question: str, http: httpx.AsyncClient) -> str:
    """One stateless EXECUTIVE_MODEL call about the screenshot; the image never enters the conversation or memory."""
    if not settings.openrouter_api_key:
        raise ScreenError("no_model")
    image = await asyncio.to_thread(_jpeg_data_url, capture.path)
    body = {
        "model": settings.executive_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"This is the user's {capture.app} window ({capture.title}). Answer in two short"
                        f" spoken sentences, no markdown, in the user's language: {question}",
                    },
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
        # Speed over depth: the user is waiting on a spoken answer.
        "reasoning": {"enabled": False},
        "provider": {"sort": "latency"},
    }
    try:
        resp = await http.post("/chat/completions", json=body)
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"]).strip()
    except httpx.HTTPStatusError as exc:
        # 400 or 404 when EXECUTIVE_MODEL takes no images.
        logger.warning(f"[Screen] {settings.executive_model} could not look ({exc.response.status_code}).")
        raise ScreenError("model_has_no_vision" if exc.response.status_code in (400, 404) else "screen_failed") from exc
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning(f"[Screen] Looking at the screen failed ({exc!r}).")
        raise ScreenError("screen_failed") from exc
