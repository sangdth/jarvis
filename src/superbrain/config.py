from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPERBRAIN_HOME = Path("~/.config/superbrain").expanduser()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SUPERBRAIN_HOME / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        str_strip_whitespace=True,
    )

    home_dir: Path = SUPERBRAIN_HOME
    host: str = "127.0.0.1"
    port: int = 8643
    superbrain_api_key: str = ""

    # Comma-separated relative repo paths under projects_dir (e.g. "jarvis,HuggerHustle/platform-v2").
    # Empty means no repo is allowed for autonomous execution until explicitly configured.
    allowed_repos: str = ""
    projects_dir: Path = Path("~/Projects").expanduser()

    # Budget & execution limits
    daily_budget_usd: float = 20.0
    max_run_seconds: int = 900
    verify_timeout_seconds: int = 120
    reconcile_interval_seconds: float = 10.0
    lease_seconds: int = 1200

    # Pi binary and models
    pi_bin: str = "/Users/sang/.local/bin/pi"
    planner_model: str = "sonnet:medium"
    builder_model: str = "sonnet:high"
    default_mode: Literal["ASSIST", "AUTONOMOUS"] = "ASSIST"
    sandbox_enabled: bool = True

    @field_validator("home_dir", "projects_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def allowed_repo_set(self) -> set[str]:
        return {item.strip().strip("/") for item in self.allowed_repos.split(",") if item.strip().strip("/")}

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


settings = Settings()
