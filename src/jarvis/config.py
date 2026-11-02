from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM API keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Speaker Verification (Picovoice Eagle)
    picovoice_access_key: str = ""
    voiceprint_path: str = "~/.jarvis/owner.voiceprint"
    speaker_similarity_threshold: float = 0.65

    # Local Always-On STT
    whisper_model: str = "distil-large-v3"

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # Agent Watchers
    opencode_server_url: str = "http://127.0.0.1:4096"
    watch_config_path: str = "./agents.json"

    # Models
    filter_model: str = "claude-3-5-haiku-latest"
    executive_model: str = "claude-sonnet-4-20250514"

    @property
    def resolved_voiceprint_path(self) -> Path:
        return Path(self.voiceprint_path).expanduser().resolve()


settings = Settings()
