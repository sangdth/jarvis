"""Superbrain's settings: one `.env` and one state home shared with the voice harness (`jarvis_core`)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from jarvis_core.home import JARVIS_HOME, brain_home, workspace_root
from jarvis_core.settings import EnvSettings
from jarvis_core.soul import load_soul

# The checkout this daemon lives in: `repo='jarvis'` is the monorepo (voice, brain, cockpit). `superbrain`
# still works for tasks queued before the merge, and while an old checkout is around.
SELF_REPO = "jarvis"
LEGACY_SELF_REPO = "superbrain"
SELF_REPOS = frozenset({SELF_REPO, LEGACY_SELF_REPO})

# `SUPERBRAIN_HOME` (or `JARVIS_BRAIN_HOME`) selects the state directory; default `JARVIS_HOME/brain`,
# falling back to the pre-merge `~/.config/superbrain` until `jarvis init` moves it. The Rust dashboard
# (`apps/tui`) resolves the same variable so both processes always agree on paths.
BRAIN_HOME = brain_home()


class Settings(EnvSettings):
    # The legacy `~/.config/superbrain/.env` is still read, but `JARVIS_HOME/.env` wins where both set a key.
    model_config = SettingsConfigDict(
        env_file=(BRAIN_HOME / ".env", JARVIS_HOME / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        str_strip_whitespace=True,
    )

    home_dir: Path = BRAIN_HOME
    # BRAIN_HOST/BRAIN_PORT, not HOST/PORT: one .env is shared with every other app, and a stray PORT
    # exported for a web dev server must not move the daemon.
    brain_host: str = "127.0.0.1"
    brain_port: int = 8643
    superbrain_api_key: str = ""

    # Comma-separated relative repo paths under projects_dir (e.g. "HuggerHustle/platform-v2").
    # This repository (`jarvis`) is always allowed; nothing else is until explicitly configured.
    allowed_repos: str = ""
    # The user's projects folder, from SOUL.md unless PROJECTS_DIR overrides it: one place to set it.
    projects_dir: Path = Field(default_factory=lambda: Path(load_soul().projects_dir).expanduser())
    # Where this repository's checkout is: the running one in dev, else `projects_dir/jarvis`.
    # `SELF_REPO_DIR` pins it for a test or a second checkout.
    self_repo_dir: Path = Field(
        default_factory=lambda: workspace_root() or (Path(load_soul().projects_dir).expanduser() / SELF_REPO)
    )

    # Budget & execution limits
    daily_budget_usd: float = 200.0
    max_run_seconds: int = 900
    verify_timeout_seconds: int = 120
    reconcile_interval_seconds: float = 10.0
    lease_seconds: int = 1200

    # Pi binary and optional per-role model overrides (empty = inherit ~/.pi/agent/settings.json)
    pi_bin: str = str(Path("~/.local/bin/pi").expanduser())
    planner_model: str = ""
    builder_model: str = ""
    improver_model: str = ""
    scout_model: str = ""
    default_mode: Literal["ASSIST", "AUTONOMOUS"] = "ASSIST"
    sandbox_enabled: bool = True
    max_personal_skills: int = 10
    auto_research_after_strikes: int = 0
    # The whole monorepo before a self-upgrade lands: Python lint, types, tests and the canary, then Rust.
    self_upgrade_verify_cmd: str = (
        "uv run ruff check apps packages tests && uv run mypy && uv run pytest && uv run superbrain self-check"
        " && if command -v cargo >/dev/null 2>&1; then cargo test --quiet --manifest-path apps/tui/Cargo.toml; fi"
    )

    @field_validator("home_dir", "projects_dir", "self_repo_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def allowed_repo_set(self) -> set[str]:
        configured = {item.strip().strip("/") for item in self.allowed_repos.split(",") if item.strip().strip("/")}
        configured.add(SELF_REPO)
        if (self.projects_dir / LEGACY_SELF_REPO).is_dir():  # a pre-merge checkout of the daemon itself
            configured.add(LEGACY_SELF_REPO)
        return configured

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.brain_host}:{self.brain_port}"

    @property
    def db_path(self) -> Path:
        return self.home_dir / "state.db"

    @property
    def worktrees_dir(self) -> Path:
        return self.home_dir / "worktrees"

    @property
    def sessions_dir(self) -> Path:
        return self.home_dir / "sessions"

    @property
    def notes_dir(self) -> Path:
        return self.home_dir / "memory" / "progress"

    @property
    def skills_dir(self) -> Path:
        return self.home_dir / "skills"

    @property
    def traces_dir(self) -> Path:
        return self.home_dir / "traces"


settings = Settings()
