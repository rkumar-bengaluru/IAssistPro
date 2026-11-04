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
            "DEBUG_STEREO_WAV", "LOG_LEVEL", "TURN_GRACE_S", "RESUME_WINDOW_S", "BARGE_IN_MIN_S",
        }:
            monkeypatch.delenv(var, raising=False)


CANARY = "ZEBRA-CANARY-7731"


@pytest.fixture
def captured_logs():
    """A list sink at DEBUG, installed after setup_logging with the same filter. Yields a function returning the text."""
    from loguru import logger

    from iassistpro import logs

    logs.setup_logging("INFO")
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="DEBUG", filter=logs.log_filter, diagnose=False,
                         backtrace=False, format="{name} | {level} | {message}{exception}")
    yield lambda: "".join(lines)
    logger.remove(sink_id)


@pytest.fixture
def settings():
    from iassistpro.config import Settings

    return Settings()


@pytest.fixture
def example_brief():
    import json
    from pathlib import Path

    from iassistpro.contract import Brief

    p = Path(__file__).resolve().parents[1] / "contracts" / "examples" / "brief.example.json"
    return Brief.model_validate(json.loads(p.read_text()))
