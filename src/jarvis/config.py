import json
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
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


# The Whisper model for each LANGUAGE, unless WHISPER_MODEL names one. For vi: VinAI's PhoWhisper-large with a
# Vietnamese-English code-switching LoRA merged in, converted to MLX by us; it keeps English words in mixed speech.
WHISPER_MODELS = {"en": "mlx-community/whisper-large-v3-turbo", "vi": "sangdth/phowhisper-large-vien-cs-mlx-16bit"}

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

    # The language the user speaks: Whisper listens for it (a guess per sentence misreads short Vietnamese ones)
    # and Jarvis greets in it. Replies follow whatever the user just said; agents always get English.
    language: Literal["en", "vi"] = "en"
    # MLX Whisper on the Apple GPU (Hugging Face repo id or local folder); empty = WHISPER_MODELS[language]
    whisper_model: str = ""

    # Owner voice check (sherpa-onnx ERes2Net): sentences scoring below this are dropped.
    # Tune it from the "[Speaker] score=" lines in the log; enroll with `jarvis enroll`.
    speaker_threshold: float = 0.35
    # Keep the last N sentences Whisper heard as WAVs in JARVIS_HOME/clips, dropped ones too; 0 = off. Your voice
    # goes to disk, so only while tracking down misses.
    save_segments: int = 0

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # Agent Watchers
    # Exact agents.json id that gets coding tasks when no agent is named; empty = first enabled agent
    default_coding_agent: str = ""

    # Pins an `opencode serve` started with this password; empty = OpenCode 2's own service (service.json)
    opencode_server_password: str = ""
    watch_config_path: str = str(JARVIS_HOME / "agents.json")  # a relative path counts from JARVIS_HOME
    # Hermes API server key (API_SERVER_KEY in ~/.hermes/.env)
    hermes_api_key: str = ""

    # OpenRouter model IDs
    executive_model: str = "deepseek/deepseek-v4.1-flash"
    # Picks facts out of each exchange for long-term memory; a fast model, run with thinking off
    extraction_model: str = "deepseek/deepseek-v4.1-flash"
    # OpenRouter decision model for the speech check (is a sentence for Jarvis?), used when Laya fails or is slow
    filter_model: str = "typesafe/jev-1.14"

    # Laya (self-hosted, HappyVoxel's GPU box) answers the speech check first; FILTER_MODEL is the fallback.
    # A console key (laya_...) from https://laya.happyvoxel.com; empty = FILTER_MODEL only. Keys expire after 90 days.
    laya_api_key: str = ""
    laya_base_url: str = "https://laya.happyvoxel.com"
    laya_model: str = "jarvis-intent"
    # Past this, Jarvis uses FILTER_MODEL's answer for the sentence (Laya ~31 ms on the box, plus the trip)
    laya_timeout_secs: float = 1.0

    @field_validator("watch_config_path")
    @classmethod
    def _from_home(cls, path: str) -> str:
        return str(JARVIS_HOME / Path(path).expanduser())  # an absolute path stays as it is

    @model_validator(mode="after")
    def _whisper_for_language(self) -> "Settings":
        self.whisper_model = self.whisper_model or WHISPER_MODELS[self.language]
        return self


settings = Settings()
