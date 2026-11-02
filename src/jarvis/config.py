from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLMs (OpenRouter for both the executive and the filter)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Local Always-On STT
    whisper_model: str = "distil-large-v3"

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # Agent Watchers
    # OpenCode v2 `serve` password (it prints a random one at startup unless OPENCODE_SERVER_PASSWORD is set)
    opencode_server_password: str = ""
    watch_config_path: str = "./agents.json"

    # OpenRouter model IDs
    executive_model: str = "deepseek/deepseek-v4.1-flash"
    filter_model: str = "deepseek/deepseek-v4.1-flash"


settings = Settings()
