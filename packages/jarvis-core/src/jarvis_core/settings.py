"""The settings base every app shares: one .env in `JARVIS_HOME`, keys from the shell winning over it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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

    def __init__(self, **values: Any) -> None:
        if "_env_file" not in values:
            configured = self.model_config.get("env_file")
            candidates = (
                (configured,)
                if isinstance(configured, (str, Path))
                else tuple(configured or ())
            )
            paths = [Path(p) for p in candidates]
            if any(p.exists() and not os.access(p, os.R_OK) for p in paths):
                # Seatbelt denies reading `.env` inside workers and `verify_cmd`; skip blocked files cleanly.
                readable = tuple(p for p in paths if not p.exists() or os.access(p, os.R_OK))
                super().__init__(_env_file=readable or None, **values)
                return
        super().__init__(**values)
