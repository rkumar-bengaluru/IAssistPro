"""Env-driven provider factory. Adding a provider = one REGISTRY entry (+ its key env var).

Provider modules are imported inside each build function, so an unselected provider is never imported.
"""

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ConfigError, Settings

NO_SPEECH_PROB_MAX = 0.4


class _ModelCache:
    """Process-wide speech models, shared by every session."""

    def __init__(self):
        self._whisper: dict = {}
        self._kokoro: dict = {}
        self._piper: dict = {}

    def piper(self, path, **kw):
        key = str(path)
        if key not in self._piper:
            import piper

            self._piper[key] = piper.PiperVoice.load(path, **kw)
        return self._piper[key]

    def whisper(self, name: str, threads: int):
        key = (name, threads)
        if key not in self._whisper:
            from faster_whisper import WhisperModel

            self._whisper[key] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
        return self._whisper[key]

    def kokoro(self, model_path: str, voices_path: str, threads: int):
        key = (model_path, threads)
        if key not in self._kokoro:
            import onnxruntime as rt
            from kokoro_onnx import Kokoro

            so = rt.SessionOptions()
            so.intra_op_num_threads = threads
            so.inter_op_num_threads = 1
            sess = rt.InferenceSession(model_path, so, providers=["CPUExecutionProvider"])
            self._kokoro[key] = Kokoro.from_session(sess, voices_path)
        return self._kokoro[key]

    def clear(self) -> None:
        self._whisper.clear()
        self._kokoro.clear()
        self._piper.clear()


model_cache = _ModelCache()


def _whisper_class():
    from pipecat.services.whisper.stt import WhisperSTTService

    class WhisperSTT(WhisperSTTService):
        """Pipecat's Whisper service with a configurable beam, fixed English and a shared int8 CPU model."""

        def __init__(self, *, beam: int, threads: int, **kwargs):
            self._beam = beam
            self._threads = threads
            super().__init__(**kwargs)

        def _load(self):
            self._model = model_cache.whisper(self._settings.model, self._threads)

        async def run_stt(self, audio: bytes):
            import numpy as np
            from pipecat.frames.frames import TranscriptionFrame
            from pipecat.utils.time import time_now_iso8601

            await self.start_processing_metrics()
            a = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

            def go():
                segs, _ = self._model.transcribe(a, language="en", beam_size=self._beam)
                return [s for s in segs if s.no_speech_prob < NO_SPEECH_PROB_MAX]

            segs = await asyncio.to_thread(go)
            text = " ".join(s.text.strip() for s in segs).strip()
            await self.stop_processing_metrics()
            if text:
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601())

    return WhisperSTT


_whisper_cls: list = []


def _whisper_stt_class():
    if not _whisper_cls:
        _whisper_cls.append(_whisper_class())
    return _whisper_cls[0]


def __getattr__(name: str):
    # `WhisperSTT` resolves lazily so importing this module never imports Pipecat's Whisper service.
    if name == "WhisperSTT":
        return _whisper_stt_class()
    raise AttributeError(name)


@dataclass(frozen=True)
class ProviderSpec:
    kind: Literal["stt", "tts", "llm"]
    name: str
    remote: bool
    key_env: str | None
    build: Callable[..., object]


# Safety net for the turn-stop strategy: it ends the turn as soon as the finalized transcript of the last
# segment arrives, and only falls back to this many seconds after the end of speech when none does. Pipecat's
# 1.0 s default is shorter than base.en on a laptop CPU (1.2-2.2 s per segment when segments queue), which
# closed turns without their last words and let the late transcript barge in as a new turn.
WHISPER_TTFS_P99_S = 4.0


def _build_whisper(s: Settings):
    from pipecat.services.whisper.stt import WhisperSTTService

    cls = _whisper_stt_class()
    return cls(beam=s.stt_beam, threads=s.stt_threads, device="cpu", compute_type="int8",
               ttfs_p99_latency=WHISPER_TTFS_P99_S, settings=WhisperSTTService.Settings(model=s.stt_model))


class _CachedPiperVoice:
    """Stands in for piper.PiperVoice inside Pipecat's Piper service, so each session reuses one loaded voice."""

    @staticmethod
    def load(path, **kw):
        return model_cache.piper(path, **kw)


def _build_piper(s: Settings):
    import pipecat.services.piper.tts as piper_tts
    from pipecat.services.piper.tts import PiperTTSService

    piper_tts.PiperVoice = _CachedPiperVoice  # PiperTTSService.__init__ calls PiperVoice.load(path)
    return PiperTTSService(download_dir=Path(s.models_dir) / "piper", stop_frame_timeout_s=10.0,
                           settings=PiperTTSService.Settings(voice=s.tts_voice_resolved))


def kokoro_paths(s: Settings) -> tuple[Path, Path]:
    d = Path(s.models_dir) / "kokoro"
    return d / "kokoro-v1.0.int8.onnx", d / "voices-v1.0.bin"


def _build_kokoro(s: Settings):
    import pipecat.services.kokoro.tts as kokoro_mod
    from pipecat.services.kokoro.tts import KokoroTTSService

    model, voices = kokoro_paths(s)
    kokoro_mod.Kokoro = lambda m, v: model_cache.kokoro(m, v, s.tts_threads)
    return KokoroTTSService(model_path=str(model), voices_path=str(voices), stop_frame_timeout_s=10.0,
                            settings=KokoroTTSService.Settings(voice=s.tts_voice_resolved))


def _build_cartesia(s: Settings):
    if not s.tts_voice:
        raise ConfigError("TTS_VOICE is required for cartesia")
    from pipecat.services.cartesia.tts import CartesiaTTSService

    return CartesiaTTSService(api_key=os.environ.get("CARTESIA_API_KEY", ""),
                              settings=CartesiaTTSService.Settings(voice=s.tts_voice))


def _build_anthropic(s: Settings, brief, system_prompt: str):
    from pipecat.services.anthropic.llm import AnthropicLLMService

    # No thinking/effort argument: Pipecat sends each model's lowest documented thinking setting (AS2).
    return AnthropicLLMService(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        settings=AnthropicLLMService.Settings(model=brief.llm_model or s.llm_model, max_tokens=200,
                                              system_instruction=system_prompt, enable_prompt_caching=True),
    )


def _build_fake(s: Settings, brief, system_prompt: str):
    from .fake_llm import FakeInterviewerLLM

    return FakeInterviewerLLM(brief)


REGISTRY: dict[tuple[str, str], ProviderSpec] = {
    ("stt", "whisper"): ProviderSpec("stt", "whisper", False, None, _build_whisper),
    ("tts", "piper"): ProviderSpec("tts", "piper", False, None, _build_piper),
    ("tts", "kokoro"): ProviderSpec("tts", "kokoro", False, None, _build_kokoro),
    ("tts", "cartesia"): ProviderSpec("tts", "cartesia", True, "CARTESIA_API_KEY", _build_cartesia),
    ("llm", "anthropic"): ProviderSpec("llm", "anthropic", True, "ANTHROPIC_API_KEY", _build_anthropic),
    ("llm", "fake"): ProviderSpec("llm", "fake", False, None, _build_fake),
}


def spec(kind: str, name: str) -> ProviderSpec:
    found = REGISTRY.get((kind, name))
    if found is None:
        known = ", ".join(sorted(n for k, n in REGISTRY if k == kind))
        raise ConfigError(f"unknown {kind.upper()}_PROVIDER '{name}' (known: {known})")
    return found


def selected(s: Settings) -> list[ProviderSpec]:
    return [spec("stt", s.stt_provider), spec("tts", s.tts_provider), spec("llm", s.llm_provider)]


def required_keys(s: Settings) -> list[str]:
    """Names (never values) of the selected providers' keys that are missing from the environment."""
    return [p.key_env for p in selected(s) if p.key_env and not os.environ.get(p.key_env)]


def provider_info(s: Settings) -> dict:
    stt, tts, llm = selected(s)
    return {
        "stt": {"name": stt.name, "model": s.stt_model, "remote": stt.remote},
        "tts": {"name": tts.name, "voice": s.tts_voice_resolved, "remote": tts.remote},
        "llm": {"name": llm.name, "model": s.llm_model},
        "transport": s.transport,
    }


def make_stt(s: Settings):
    return spec("stt", s.stt_provider).build(s)


def make_tts(s: Settings):
    return spec("tts", s.tts_provider).build(s)


def make_llm(s: Settings, brief, system_prompt: str):
    return spec("llm", s.llm_provider).build(s, brief, system_prompt)


def make_vad(s: Settings):
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    return SileroVADAnalyzer(params=VADParams(stop_secs=s.vad_stop_s))


def make_turn_strategies(s: Settings):
    """(UserTurnStrategies, VAD analyzer): smart-turn v3 by default, silence timeout as the fallback."""
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    if s.turn_stop == "smart":
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

        stop = [TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
    else:
        stop = [SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=max(0.0, s.silence_s - s.vad_stop_s))]
    return UserTurnStrategies(stop=stop), make_vad(s)
