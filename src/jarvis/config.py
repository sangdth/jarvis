import json
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE = "jarvis-harness"
# The installed Jarvis keeps its state here; every Jarvis, dev or installed, keeps the one-mic lock here.
SHARED_HOME = Path("~/.config/jarvis").expanduser()


def install_info() -> dict:
    """pip's direct_url.json for Jarvis: `vcs_info` for a GitHub install, a file:// `url` for a checkout."""
    try:
        return json.loads(distribution(PACKAGE).read_text("direct_url.json") or "{}")
    except (PackageNotFoundError, ValueError):
        return {}


def checkout_root(info: dict | None = None) -> Path | None:
    """The checkout whose code is running (`uv run jarvis`), or None for the installed Jarvis."""
    info = install_info() if info is None else info
    if info.get("vcs_info"):
        return None
    url = info.get("url", "")
    return Path(url.removeprefix("file://")) if url.startswith("file://") else Path(__file__).resolve().parents[2]


# Jarvis's state: .env, agents.json, SOUL.md, the voiceprint, models and memory. The installed Jarvis uses
# SHARED_HOME; a dev checkout uses its own folder (found from the running code, never the current folder).
JARVIS_HOME = checkout_root() or SHARED_HOME


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=JARVIS_HOME / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLMs (OpenRouter for both the executive and the filter)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Local Always-On STT
    # MLX Whisper on the Apple GPU (Hugging Face repo id)
    whisper_model: str = "mlx-community/distil-whisper-large-v3"

    # Owner voice check (sherpa-onnx ERes2Net): sentences scoring below this are dropped.
    # Tune it from the "[Speaker] score=" lines in the log; enroll with `jarvis enroll`.
    speaker_threshold: float = 0.5

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # Agent Watchers
    # Exact agents.json id that gets coding tasks when no agent is named; empty = first enabled agent
    default_coding_agent: str = ""

    # OpenCode v2 `serve` password (it prints a random one at startup unless OPENCODE_SERVER_PASSWORD is set)
    opencode_server_password: str = ""
    watch_config_path: str = str(JARVIS_HOME / "agents.json")  # a relative path counts from JARVIS_HOME
    # Hermes API server key (API_SERVER_KEY in ~/.hermes/.env)
    hermes_api_key: str = ""

    # OpenRouter model IDs
    executive_model: str = "deepseek/deepseek-v4.1-flash"
    # Decision model that labels each heard sentence (is it for Jarvis?); won the 2026-11-02 benchmark
    filter_model: str = "typesafe/jev-1.14"

    @field_validator("watch_config_path")
    @classmethod
    def _from_home(cls, path: str) -> str:
        return str(JARVIS_HOME / Path(path).expanduser())  # an absolute path stays as it is


settings = Settings()
