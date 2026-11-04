import pytest

from iassistpro.config import ConfigError, Settings
from iassistpro.providers import (
    REGISTRY, WhisperSTT, make_llm, make_stt, make_tts, make_turn_strategies, model_cache, provider_info,
    required_keys,
)


@pytest.mark.slow   # constructing Piper may fetch the voice into /models on first run
def test_default_factory_is_local_whisper_and_piper(settings):
    stt, tts = make_stt(settings), make_tts(settings)
    assert isinstance(stt, WhisperSTT) and type(tts).__name__ == "PiperTTSService" and tts._stop_frame_timeout_s == 10.0
    assert provider_info(settings)["stt"] == {"name": "whisper", "model": "base.en", "remote": False}


def test_paid_tts_needs_only_config_and_a_key(monkeypatch):   # acceptance #6
    monkeypatch.setenv("TTS_PROVIDER", "cartesia")
    monkeypatch.setenv("TTS_VOICE", "voice-123")
    monkeypatch.setenv("CARTESIA_API_KEY", "test-not-a-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    s = Settings()
    tts = make_tts(s)
    assert type(tts).__name__ == "CartesiaTTSService" and provider_info(s)["tts"]["remote"] is True and required_keys(s) == []
    assert provider_info(s)["tts"]["voice"] == "voice-123"


def test_cartesia_without_voice_is_a_config_error(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "cartesia")
    monkeypatch.setenv("CARTESIA_API_KEY", "test-not-a-key")
    with pytest.raises(ConfigError, match="TTS_VOICE is required for cartesia"):
        make_tts(Settings())


def test_missing_paid_key_is_reported_by_name_only(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "cartesia")
    monkeypatch.setenv("TTS_VOICE", "voice-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    assert required_keys(Settings()) == ["CARTESIA_API_KEY"]


def test_unknown_provider_names_the_known_ones(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "x")
    with pytest.raises(ConfigError, match=r"unknown TTS_PROVIDER 'x' \(known: cartesia, kokoro, piper\)"):
        make_tts(Settings())


def test_keys_of_unselected_providers_are_not_required(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    assert required_keys(Settings()) == []


def test_registry_has_the_mapped_providers():
    assert set(REGISTRY) == {("stt", "whisper"), ("tts", "piper"), ("tts", "kokoro"), ("tts", "cartesia"),
                             ("llm", "anthropic"), ("llm", "fake")}
    assert REGISTRY[("llm", "anthropic")].key_env == "ANTHROPIC_API_KEY" and REGISTRY[("tts", "cartesia")].remote


def test_llm_uses_brief_model_and_no_thinking_override(monkeypatch, example_brief):
    from anthropic import NOT_GIVEN as ANTHROPIC_NOT_GIVEN
    from pipecat.utils.types import NOT_GIVEN

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    llm = make_llm(Settings(), example_brief.model_copy(update={"llm_model": "claude-sonnet-5-5"}), "sys")
    assert llm._settings.model == "claude-sonnet-5-5" and llm._settings.max_tokens == 200
    # Pipecat 1.15's 'unset' is anthropic.NOT_GIVEN for thinking and None for effort.
    assert llm._settings.thinking in (NOT_GIVEN, ANTHROPIC_NOT_GIVEN) and llm._settings.effort in (None, NOT_GIVEN)
    assert llm._settings.enable_prompt_caching is True and llm._settings.system_instruction == "sys"


def test_llm_falls_back_to_settings_model(monkeypatch, example_brief):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-5-5")
    llm = make_llm(Settings(), example_brief.model_copy(update={"llm_model": None}), "sys")
    assert llm._settings.model == "claude-haiku-5-5"


def test_whisper_model_is_shared(monkeypatch):
    import faster_whisper

    made = []

    class Stub:
        def __init__(self, name, **kw):
            made.append((name, kw))

    monkeypatch.setattr(faster_whisper, "WhisperModel", Stub)
    model_cache.clear()
    s = Settings()
    a, b = make_stt(s), make_stt(s)
    assert a._model is b._model and len(made) == 1
    assert made[0] == ("base.en", {"device": "cpu", "compute_type": "int8", "cpu_threads": 4})
    model_cache.clear()


def test_whisper_stt_safety_net_outlasts_cpu_transcription(monkeypatch):
    # Turn end waits for Whisper's finalized transcript; the fallback timeout only fires when none comes.
    # Pipecat's 1.0 s default is shorter than base.en on this CPU (1.2-2.2 s per queued segment), so the
    # turn closed with the last segment's words missing and the late transcript barged in as a new turn.
    import faster_whisper

    from iassistpro.providers import WHISPER_TTFS_P99_S

    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda name, **kw: object())
    model_cache.clear()
    stt = make_stt(Settings())
    assert WHISPER_TTFS_P99_S >= 3.0 and stt._ttfs_p99_latency == WHISPER_TTFS_P99_S
    model_cache.clear()


def test_piper_voice_is_loaded_once_and_shared(monkeypatch, tmp_path):
    # Loading the voice took ~2 s on the event loop inside every offer (connect -> first question budget).
    import piper

    loads = []

    class Voice:
        config = type("C", (), {"sample_rate": 22050})()

    monkeypatch.setattr(piper.PiperVoice, "load", staticmethod(lambda path, **kw: loads.append(str(path)) or Voice()))
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    (tmp_path / "piper").mkdir()
    (tmp_path / "piper" / "en_US-lessac-medium.onnx").write_bytes(b"x")
    (tmp_path / "piper" / "en_US-lessac-medium.onnx.json").write_text("{}")
    model_cache.clear()
    a, b = make_tts(Settings()), make_tts(Settings())
    assert len(loads) == 1 and a._voice is b._voice
    model_cache.clear()


def test_turn_strategies_smart_and_silence(monkeypatch):
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy

    strategies, vad = make_turn_strategies(Settings())
    assert isinstance(strategies.stop[0], TurnAnalyzerUserTurnStopStrategy) and vad.params.stop_secs == 0.2
    monkeypatch.setenv("TURN_STOP", "silence")
    strategies, _ = make_turn_strategies(Settings())
    assert isinstance(strategies.stop[0], SpeechTimeoutUserTurnStopStrategy)
