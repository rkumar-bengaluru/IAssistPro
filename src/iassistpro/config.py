"""Service settings: one environment variable per field (upper-case field name)."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_VOICES = {"piper": "en_US-lessac-medium", "kokoro": "af_heart", "cartesia": ""}
MAX_ICE_PORTS = 10


class ConfigError(Exception):
    """A setting combination the service cannot run with (message names the setting, never a value)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    port: int = 8000
    transport: Literal["smallwebrtc", "websocket"] = "smallwebrtc"
    ice_ports: str = "40010-40012"
    ice_host: str = "127.0.0.1"
    stt_provider: str = "whisper"
    stt_model: str = "base.en"
    stt_beam: int = 1
    stt_threads: int = 4
    tts_provider: str = "piper"
    tts_voice: str = ""
    tts_threads: int = 4
    turn_stop: Literal["smart", "silence"] = "smart"
    silence_s: float = 0.8
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-5-5"
    models_dir: str = "/models"
    sessions_dir: str = "/data/sessions"
    result_ttl_h: int = 24
    connect_token_ttl_s: int = 120
    dev_client: bool = False
    debug_stereo_wav: bool = False
    log_level: str = "INFO"

    @property
    def vad_stop_s(self) -> float:
        return 0.2

    @property
    def ice_range(self) -> tuple[int, int]:
        try:
            lo_s, hi_s = self.ice_ports.split("-")
            lo, hi = int(lo_s), int(hi_s)
        except ValueError as e:
            raise ValueError("ICE_PORTS must look like 40010-40012") from e
        if lo > hi or hi - lo + 1 > MAX_ICE_PORTS:
            raise ValueError(f"ICE_PORTS must be an ascending range of at most {MAX_ICE_PORTS} ports")
        return lo, hi

    @property
    def tts_voice_resolved(self) -> str:
        return self.tts_voice or DEFAULT_VOICES.get(self.tts_provider, "")
