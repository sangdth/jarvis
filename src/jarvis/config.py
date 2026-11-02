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
    # MLX Whisper on the Apple GPU (Hugging Face repo id)
    whisper_model: str = "mlx-community/distil-whisper-large-v3"

    # Ignore the mic while Jarvis speaks, so it doesn't hear itself through laptop speakers.
    # With headphones set false: you can then cut Jarvis off mid-sentence ("shut up").
    mute_mic_while_speaking: bool = True

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # Agent Watchers
    # Default coding agent to use when user says "build/code X" without naming an agent
    # Can be an exact agent ID (e.g. "opencode-frontend") or agent type/name ("opencode", "claude")
    default_coding_agent: str = "opencode-frontend"

    # OpenCode v2 `serve` password (it prints a random one at startup unless OPENCODE_SERVER_PASSWORD is set)
    opencode_server_password: str = ""
    watch_config_path: str = "./agents.json"

    # OpenRouter model IDs
    executive_model: str = "deepseek/deepseek-v4.1-flash"
    filter_model: str = "deepseek/deepseek-v4.1-flash"


settings = Settings()
