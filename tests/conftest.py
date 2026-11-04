import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Tests never see the host's provider settings or keys unless they set them."""
    import os

    for var in list(os.environ):
        if var in {
            "PORT", "TRANSPORT", "ICE_PORTS", "ICE_HOST", "STT_PROVIDER", "STT_MODEL", "STT_BEAM", "STT_THREADS",
            "TTS_PROVIDER", "TTS_VOICE", "TTS_THREADS", "TURN_STOP", "SILENCE_S", "LLM_PROVIDER", "LLM_MODEL",
            "ANTHROPIC_API_KEY", "CARTESIA_API_KEY", "RESULT_TTL_H", "CONNECT_TOKEN_TTL_S", "DEV_CLIENT",
            "DEBUG_STEREO_WAV", "LOG_LEVEL",
        }:
            monkeypatch.delenv(var, raising=False)
