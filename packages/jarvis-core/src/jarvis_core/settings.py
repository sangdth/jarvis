"""The settings base every app shares: one .env in `JARVIS_HOME`, keys from the shell winning over it."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from jarvis_core.home import JARVIS_HOME


class EnvSettings(BaseSettings):
    """
    `JARVIS_HOME/.env` holds every key, voice and brain alike. Shell exports win; an empty export falls
    back to the file (`env_ignore_empty`), as `jarvis init` assumes. Unknown keys are ignored, so one
    .env can carry the settings of every app.
    """

    model_config = SettingsConfigDict(
        env_file=JARVIS_HOME / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        str_strip_whitespace=True,  # a key exported with a trailing newline would break the HTTP header
    )
