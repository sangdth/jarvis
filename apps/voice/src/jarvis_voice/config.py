"""The voice harness's settings: one `.env` in `JARVIS_HOME`, shared with the brain daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from jarvis_core.home import JARVIS_HOME
from jarvis_core.settings import EnvSettings

# The Whisper model for each LANGUAGE, unless WHISPER_MODEL names one. For vi: VinAI's PhoWhisper-large with a
# Vietnamese-English code-switching LoRA merged in, converted to MLX by us; it keeps English words in mixed speech.
WHISPER_MODELS = {"en": "mlx-community/whisper-large-v3-turbo", "vi": "sangdth/phowhisper-large-vien-cs-mlx-16bit"}


class Settings(EnvSettings):
    # LLMs (OpenRouter for both the executive and the filter)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # The language the user speaks: Whisper listens for it (a guess per sentence misreads short Vietnamese ones)
    # and Jarvis greets in it. Replies follow whatever the user just said; agents always get English.
    language: Literal["en", "vi"] = "en"
    # MLX Whisper on the Apple GPU (Hugging Face repo id or local folder); empty = WHISPER_MODELS[language]
    whisper_model: str = ""

    # Master check: Jarvis acts only on its master's voice (sherpa-onnx ERes2Net against owner_voice.npy, recorded
    # by `jarvis enroll`). On by default: `jarvis init` offers to record the voiceprint, and without it Jarvis
    # refuses to start rather than obey any voice. In a meeting, other voices are tagged `[Room]` and may still talk
    # to Jarvis (they are not dropped); they just cannot answer, approve or stop work.
    master_check: bool = True
    # With the master check on, sentences scoring below this are dropped. Tune it from the "[Speaker] score=" lines.
    speaker_threshold: float = 0.35
    # Keep the last N sentences Whisper heard as WAVs in JARVIS_HOME/clips, dropped ones too; 0 = off. Your voice
    # goes to disk, so only while tracking down misses.
    save_segments: int = 0
    # Smart Turn v3 (local, ~50 ms) decides from the sound whether a pause ends the sentence: a short pause ends it
    # when the voice says done, a pause mid-thought waits up to 1.5 s. Off: every 0.6 s pause ends the sentence.
    smart_turn: bool = True
    # `jarvis meeting`, master check on: work a guest asks for waits for the master's yes ("ask") or starts at once
    # ("allow"). Answering an agent, stopping or replacing work stays the master's either way.
    meeting_guests: Literal["ask", "allow"] = "ask"

    # TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"

    # A paired Mac's mic and speaker (the thin client, `defaults/remote/client.py`): the models, agents and memory
    # stay here. Always served, on the one Jarvis HTTP app, so the port is the brain's (`BRAIN_PORT`) and the token
    # is the brain token, unless `REMOTE_TOKEN` pins another. While a client is connected the ear follows whichever
    # Mac hears you; this Mac's hardware takes it back when the client goes.
    remote_token: str = ""

    # The brain (this repository's `apps/brain`): the always-on daemon `jarvis` supervises, with the voice in
    # the same process. Its HTTP/SSE/WS gateway is the only network surface Jarvis exposes.
    brain_host: str = "0.0.0.0"
    brain_port: int = 8643
    brain_url: str = ""
    # Its bearer token; empty = the one in JARVIS_HOME/brain_token, made on first use
    brain_api_key: str = ""

    # Agent Watchers
    # Exact agents.json id that gets coding tasks when no agent is named; empty = first enabled agent
    default_coding_agent: str = ""

    # Pins an `opencode serve` started with this password; empty = OpenCode 2's own service (service.json)
    opencode_server_password: str = ""
    watch_config_path: str = str(JARVIS_HOME / "agents.json")  # a relative path counts from JARVIS_HOME

    # OpenRouter model IDs
    executive_model: str = "deepseek/deepseek-v4.1-flash"
    # Picks facts out of each exchange for long-term memory; a fast model, run with thinking off
    extraction_model: str = "deepseek/deepseek-v4.1-flash"
    # OpenRouter decision model for the speech check (is a sentence for Jarvis?), used when Laya fails or is slow
    filter_model: str = "typesafe/jev-1.14"
    # Tidies spoken slips, fillers and long sentences (>18 words, or with a filler or CLEANUP_CUES phrase) before the
    # conversation model reads them; about 250-500 ms when it runs, never more than 1.5 s.
    transcript_cleanup: bool = True
    # The model for that cleanup, run with thinking off; empty = EXECUTIVE_MODEL
    cleanup_model: str = ""
    # Words the user says to fix a slip mid-sentence, comma-separated; a short sentence with one is cleaned too
    cleanup_cues: str = "sorry, no no, actually, scratch that, I mean, à quên, nhầm, ý mình là, ý anh là"

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

    @property
    def connect_host(self) -> str:
        """Loopback when bound to a wildcard address, otherwise the configured host."""
        return "127.0.0.1" if self.brain_host in ("", "0.0.0.0", "::") else self.brain_host

    @property
    def endpoint(self) -> str:
        return self.brain_url.rstrip("/") or f"http://{self.connect_host}:{self.brain_port}"


settings = Settings()
