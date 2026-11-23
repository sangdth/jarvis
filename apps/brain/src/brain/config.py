"""The brain's settings: one `.env` and one state home shared with the voice harness (`jarvis_core`)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from jarvis_core.home import JARVIS_HOME, brain_home, brain_token, workspace_root
from jarvis_core.settings import EnvSettings
from jarvis_core.soul import load_soul

# The checkout this daemon is part of. `repo='jarvis'` is this repository (voice, brain, cockpit);
# `repo='brain'` names the same checkout, for tasks written by voice before the app was renamed.
SELF_REPO = "jarvis"
SELF_REPOS = frozenset({SELF_REPO, "brain"})

# `BRAIN_HOME` selects the state directory; default `JARVIS_HOME/brain`,
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
    # Listen on all interfaces by default so thin clients on LAN / Tailscale can reach the Mac mini directly;
    # every route (except `/health`) is guarded by constant-time token auth (`BRAIN_API_KEY` / `JARVIS_TOKEN`).
    brain_host: str = "0.0.0.0"
    brain_port: int = 8643
    # Empty unless `.env` or the shell names one: the token `jarvis` makes in the state home, by default_factory
    brain_api_key: str = Field(default_factory=brain_token)

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
    # Per-run ceilings: a stuck loop stops at these, long before the daily budget would notice.
    max_builder_cost_usd: float = 15.0
    max_planner_cost_usd: float = 2.0
    max_improver_cost_usd: float = 5.0
    max_ephemeral_cost_usd: float = 0.50
    max_scout_cost_usd: float = 0.50
    check_timeout_seconds: int = 300
    # A conversational `pi` session is kept here and resumed by id; old ones are pruned.
    max_chat_sessions: int = 50
    chat_session_ttl_days: int = 14
    max_chat_session_mb: int = 50
    max_agent_followups_per_task: int = 3

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
    # An Improver-proposed harness fix lands as a draft task unless the owner opts into auto-ready.
    autonomous_harness_upgrades: bool = False

    # Interactive orchestration. A small question is one-shot `pi`; project work goes to a long-lived client
    # thread (`t3` by default). `orchestrator_model` routes a turn; empty = a fast default model.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    default_project_runner: Literal["t3", "pi"] = "t3"
    orchestrator_model: str = ""
    ephemeral_model: str = ""
    ephemeral_timeout_seconds: int = 120
    # T3 Code's app server. On loopback the brain starts `t3 serve` itself when nothing answers.
    t3_url: str = "http://127.0.0.1:3773"
    t3_auto_start: bool = True
    t3_start_timeout_seconds: float = 10.0
    t3_max_run_seconds: int = 1800
    # The whole monorepo before a self-upgrade lands: Python lint, types, tests and the canary, then Rust.
    self_upgrade_verify_cmd: str = (
        "uv run ruff check src apps packages tests && uv run mypy && uv run pytest && uv run jarvis self-check"
        " && if command -v cargo >/dev/null 2>&1; then cargo test --quiet --manifest-path apps/tui/Cargo.toml; fi"
    )

    @field_validator("home_dir", "projects_dir", "self_repo_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def allowed_repo_set(self) -> set[str]:
        configured = {item.strip().strip("/") for item in self.allowed_repos.split(",") if item.strip().strip("/")}
        configured |= SELF_REPOS  # this repository is always allowed; nothing else is until listed
        return configured

    @property
    def connect_host(self) -> str:
        """Loopback when bound to a wildcard address (`0.0.0.0` / `::`), otherwise `brain_host`."""
        return "127.0.0.1" if self.brain_host in ("", "0.0.0.0", "::") else self.brain_host

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.connect_host}:{self.brain_port}"

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
    def memory_dir(self) -> Path:
        return self.home_dir / "memory"

    @property
    def instructions_path(self) -> Path:
        return self.home_dir / "INSTRUCTIONS.md"

    @property
    def memory_index_path(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def reported_path(self) -> Path:
        return self.memory_dir / "reported.md"

    @property
    def bookmarks_path(self) -> Path:
        return self.memory_dir / "bookmarks.json"

    @property
    def chat_sessions_dir(self) -> Path:
        return self.sessions_dir / "chat"

    @property
    def skills_dir(self) -> Path:
        return self.home_dir / "skills"

    @property
    def traces_dir(self) -> Path:
        return self.home_dir / "traces"


settings = Settings()
