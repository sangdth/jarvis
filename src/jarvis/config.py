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
    # Exact agents.json id that gets coding tasks when no agent is named; empty = first enabled agent
    default_coding_agent: str = ""

    # OpenCode v2 `serve` password (it prints a random one at startup unless OPENCODE_SERVER_PASSWORD is set)
    opencode_server_password: str = ""
    watch_config_path: str = "./agents.json"

    # OpenRouter model IDs
    executive_model: str = "deepseek/deepseek-v4.1-flash"
    filter_model: str = "deepseek/deepseek-v4.1-flash"
    # Decision model that labels each heard sentence (is it for Jarvis?); won the 2026-11-02 benchmark
    classifier_model: str = "typesafe/jev-1.14"


settings = Settings()
