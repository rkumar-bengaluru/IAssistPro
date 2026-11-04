"""Model assets downloaded once into MODELS_DIR, and the Health document built from their status."""

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import CONTRACT_VERSION, __version__
from .config import ConfigError, Settings
from .logs import log_event
from .providers import kokoro_paths, provider_info, required_keys

WHISPER_MB = {"base.en": 145, "small.en": 480}
PIPER_MB = 63
KOKORO_MB = 115
KOKORO_URLS = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}


@dataclass
class Asset:
    id: str
    size_mb: int
    fetch: Callable[[], None]


def _fetch_whisper(s: Settings) -> None:
    from faster_whisper import download_model

    try:
        download_model(s.stt_model, local_files_only=True)
    except Exception:  # not cached yet
        download_model(s.stt_model)


def _fetch_piper(s: Settings) -> None:
    d = Path(s.models_dir) / "piper"
    voice = s.tts_voice_resolved
    if (d / f"{voice}.onnx").exists() and (d / f"{voice}.onnx.json").exists():
        return
    from piper.download_voices import download_voice

    d.mkdir(parents=True, exist_ok=True)
    download_voice(voice, d)


def _download(url: str, path: Path) -> None:
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(path)


def _fetch_kokoro(s: Settings) -> None:
    for p in kokoro_paths(s):
        if not p.exists():
            _download(KOKORO_URLS[p.name], p)


def required_assets(s: Settings) -> list[Asset]:
    out: list[Asset] = []
    if s.stt_provider == "whisper":
        out.append(Asset(f"whisper:{s.stt_model}", WHISPER_MB.get(s.stt_model, 500), functools.partial(_fetch_whisper, s)))
    if s.tts_provider == "piper":
        out.append(Asset(f"piper:{s.tts_voice_resolved}", PIPER_MB, functools.partial(_fetch_piper, s)))
    elif s.tts_provider == "kokoro":
        out.append(Asset("kokoro:int8", KOKORO_MB, functools.partial(_fetch_kokoro, s)))
    return out


class ModelStatus:
    def __init__(self):
        self.state: Literal["loading_models", "ready", "error"] = "loading_models"
        self.done_mb = 0
        self.total_mb = 0
        self.current = ""
        self.error: str | None = None

    def snapshot(self) -> dict:
        return {"state": self.state, "done_mb": self.done_mb, "total_mb": self.total_mb, "current": self.current,
                "error": self.error}


async def prepare(s: Settings, status: ModelStatus, warm: Callable[[], None]) -> None:
    """Fetch every asset in a thread (progress per asset), warm the models once, then mark ready."""
    try:
        todo = required_assets(s)
    except ConfigError as e:
        status.state, status.error = "error", f"config:{e}"
        return
    status.total_mb = sum(a.size_mb for a in todo)
    for a in todo:
        status.current = a.id
        try:
            await asyncio.to_thread(a.fetch)
        except Exception as e:
            status.state, status.error = "error", f"model_download_failed:{a.id}"
            log_event("model_download_failed", asset=a.id, error=type(e).__name__)
            return
        status.done_mb += a.size_mb
    status.current = "warm"
    try:
        await asyncio.to_thread(warm)
    except ConfigError as e:
        status.state, status.error = "error", f"config:{e}"
        return
    except Exception as e:
        status.state, status.error = "error", "model_download_failed:warm"
        log_event("model_warm_failed", error=type(e).__name__)
        return
    status.current = ""
    status.state = "ready"
    log_event("models_ready", total_mb=status.total_mb)


def _providers_or_raw(s: Settings) -> dict:
    try:
        return provider_info(s)
    except ConfigError:
        return {"stt": {"name": s.stt_provider, "model": s.stt_model, "remote": False},
                "tts": {"name": s.tts_provider, "voice": s.tts_voice_resolved, "remote": False},
                "llm": {"name": s.llm_provider, "model": s.llm_model}, "transport": s.transport}


def health(s: Settings, status: ModelStatus, *, busy: bool) -> dict:
    """The Health JSON. It never carries a session id: an id is the key to that session's data (AF1)."""
    state, error = status.state, status.error
    try:
        missing = required_keys(s)
        s.ice_range
    except (ConfigError, ValueError) as e:
        missing, state, error = [], "error", f"config:{e}"
    if missing:
        state, error = "error", f"missing_key:{missing[0]}"
    progress = None
    if state == "loading_models":
        progress = {"done_mb": status.done_mb, "total_mb": status.total_mb, "current": status.current}
    return {
        "status": state, "version": __version__, "contract_version": CONTRACT_VERSION, "progress": progress,
        "error": error, "providers": _providers_or_raw(s), "busy": busy,
    }
