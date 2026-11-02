from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM API keys
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    anthropic_api_key: str = ""

    # Local Always-On STT
    whisper_model: str = "distil-large-v3"

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # Agent Watchers
    opencode_server_url: str = "http://127.0.0.1:4096"
    watch_config_path: str = "./agents.json"

    # Models (OpenRouter model format)
    filter_model: str = "anthropic/claude-3.5-haiku"
    executive_model: str = "anthropic/claude-3.7-sonnet"


settings = Settings()
