"""
Where Jarvis keeps its state. One home for every app in this repository: the voice harness, the brain
daemon (`jarvis_core.brain_home()`), the cockpit and the one-mic lock.

The installed Jarvis uses `~/.config/jarvis`; a dev checkout (`uv run jarvis`) uses the checkout's own
folder, found from the running code and never the current folder. The folder is gitignored in a checkout.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

PACKAGE = "jarvis-harness"
# The installed Jarvis keeps its state here; every Jarvis, dev or installed, keeps the one-mic lock here.
SHARED_HOME = Path("~/.config/jarvis").expanduser()
# Superbrain's own home before it moved into this repository; read until `jarvis init` moves it.
LEGACY_BRAIN_HOME = Path("~/.config/superbrain").expanduser()
BRAIN_DIRNAME = "brain"
BRAIN_TOKEN_FILE = "brain_token"


def install_info() -> dict:
    """pip's direct_url.json for Jarvis: `vcs_info` for a GitHub install, a file:// `url` for a checkout."""
    try:
        return json.loads(distribution(PACKAGE).read_text("direct_url.json") or "{}")
    except (PackageNotFoundError, ValueError):
        return {}


def workspace_root(start: Path | None = None) -> Path | None:
    """The checkout that holds this code: the nearest ancestor with a uv workspace pyproject.toml."""
    for parent in (start or Path(__file__).resolve()).parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if "[tool.uv.workspace]" in text:
            return parent
    return None


def checkout_root(info: dict | None = None) -> Path | None:
    """The checkout whose code is running (`uv run jarvis`), or None for the installed Jarvis."""
    info = install_info() if info is None else info
    if info.get("vcs_info"):
        return None
    url = info.get("url", "")
    if url.startswith("file://"):
        return Path(url.removeprefix("file://"))
    return workspace_root()


JARVIS_HOME = checkout_root() or SHARED_HOME
BRAIN_TOKEN_PATH = JARVIS_HOME / BRAIN_TOKEN_FILE


def shared_dir(path: Path, *, mode: int = 0o700) -> Path:
    """Make `path`, tightening the permissions of the shared home; a checkout keeps its own umask."""
    path.mkdir(parents=True, exist_ok=True)
    if path in (SHARED_HOME, JARVIS_HOME) or SHARED_HOME in path.parents:
        try:
            path.chmod(mode)
        except OSError:
            pass
    return path


def brain_home() -> Path:
    """
    The brain daemon's state folder. `SUPERBRAIN_HOME` (or `JARVIS_BRAIN_HOME`) wins; otherwise
    `JARVIS_HOME/brain`, falling back to the old `~/.config/superbrain` until `jarvis init` moves it.
    """
    explicit = os.environ.get("JARVIS_BRAIN_HOME") or os.environ.get("SUPERBRAIN_HOME")
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser()
    home = JARVIS_HOME / BRAIN_DIRNAME
    if home.exists():
        return home
    # An installed Jarvis keeps using the pre-merge folder until `jarvis init` moves it; a dev checkout
    # always uses its own, and attaches to the one daemon on the port commandeered by nobody (see supervisor).
    if JARVIS_HOME == SHARED_HOME and LEGACY_BRAIN_HOME.is_dir():
        return LEGACY_BRAIN_HOME
    return home


def env_file_value(name: str, *paths: Path) -> str:
    """A `NAME=value` line from the first file that sets it (a tiny reader: settings are not loaded yet)."""
    for path in paths or (JARVIS_HOME / ".env",):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    return ""


def brain_token() -> str:
    """The bearer token the daemon, the voice harness and the cockpit share; made on first use (0600)."""
    explicit = os.environ.get("SUPERBRAIN_API_KEY", "").strip()
    if not explicit:
        explicit = env_file_value("SUPERBRAIN_API_KEY", JARVIS_HOME / ".env", brain_home() / ".env")
    if explicit:
        return explicit
    if BRAIN_TOKEN_PATH.is_file():
        token = BRAIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    shared_dir(BRAIN_TOKEN_PATH.parent)
    BRAIN_TOKEN_PATH.write_text(f"{token}\n", encoding="utf-8")
    BRAIN_TOKEN_PATH.chmod(0o600)
    return token


def migrate_legacy_brain_home() -> Path | None:
    """Move `~/.config/superbrain` into `JARVIS_HOME/brain`; returns the new folder, or None if nothing moved."""
    if not LEGACY_BRAIN_HOME.is_dir():
        return None
    target = JARVIS_HOME / BRAIN_DIRNAME
    if target.exists():
        return None
    shared_dir(JARVIS_HOME)
    shutil.move(str(LEGACY_BRAIN_HOME), str(target))
    return target
