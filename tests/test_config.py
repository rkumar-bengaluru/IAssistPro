import pytest

from iassistpro.config import Settings


def test_defaults_match_the_spike_rulings(monkeypatch):
    s = Settings(_env_file=None)
    assert (s.transport, s.stt_provider, s.stt_model, s.stt_beam, s.stt_threads) == ("smallwebrtc", "whisper", "base.en", 1, 4)
    assert (s.tts_provider, s.tts_voice_resolved, s.turn_stop, s.llm_model) == ("piper", "en_US-lessac-medium", "smart", "claude-haiku-5-5")
    assert (s.result_ttl_h, s.connect_token_ttl_s, s.ice_range, s.vad_stop_s) == (24, 120, (40010, 40012), 0.2)


def test_kokoro_default_voice(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "kokoro")
    assert Settings().tts_voice_resolved == "af_heart"


@pytest.mark.parametrize("bad", ["40012-40010", "abc", "40000-40020"])
def test_bad_ice_range_is_refused(monkeypatch, bad):
    monkeypatch.setenv("ICE_PORTS", bad)
    with pytest.raises(ValueError):
        Settings().ice_range
