import logging

from loguru import logger

from iassistpro.logs import log_event, setup_logging
from tests.conftest import CANARY


def test_log_event_redacts_free_text(captured_logs):
    log_event("turn_closed", speaker="candidate", chars=42, text=f"I led {CANARY} the team")
    out = captured_logs()
    assert "turn_closed" in out and "chars=42" in out and CANARY not in out and "<redacted:len=" in out


def test_third_party_info_and_debug_are_dropped(captured_logs):
    from loguru import logger as lg

    lg.bind().patch(lambda r: r.update(name="pipecat.services.tts_service")).info(f"Generating TTS [{CANARY}]")
    logging.getLogger("aioice.ice").info(CANARY)
    logging.getLogger("uvicorn.access").info(CANARY)
    assert CANARY not in captured_logs()


def test_third_party_warning_passes(captured_logs):
    logging.getLogger("aiortc").warning("ice failed code=7")
    assert "ice failed code=7" in captured_logs()


def test_exceptions_do_not_print_local_variables(captured_logs):
    secret = CANARY  # noqa: F841
    try:
        1 / 0
    except ZeroDivisionError:
        logger.exception("boom")
    assert CANARY not in captured_logs()


def test_setup_logging_is_idempotent(captured_logs):
    setup_logging("INFO")
    setup_logging("INFO")
    log_event("once", n=1)
    assert captured_logs().count("once n=1") == 1
