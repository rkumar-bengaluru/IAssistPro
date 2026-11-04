"""Content-free logging (§5.7, AS5).

IAssistPro's own records pass at the configured level; every other library (Pipecat, aiortc, aioice, uvicorn,
httpx, ...) passes only at WARNING or above, because their INFO/DEBUG lines can carry transcript text or SDP.
Values passed to log_event that are not plain ids, codes or numbers are redacted.
"""

import logging
import re
import sys

from loguru import logger

_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:/-]{0,64}$")
_LEVEL_NO = logging.getLevelNamesMapping()
_state: dict = {"level": 20, "sink": None, "default_removed": False}


def log_filter(record) -> bool:
    name = record["name"] or ""
    if name == "iassistpro" or name.startswith("iassistpro."):
        return record["level"].no >= _state["level"]
    return record["level"].no >= logging.WARNING


class _InterceptHandler(logging.Handler):
    """Route stdlib logging into loguru, keeping the stdlib logger's name for the filter."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not record.name.startswith("iassistpro"):
            return
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.patch(lambda r, n=record.name: r.update(name=n)).opt(exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(level: str = "INFO") -> None:
    _state["level"] = _LEVEL_NO.get(level.upper(), logging.INFO)
    if not _state["default_removed"]:
        try:
            logger.remove(0)
        except ValueError:
            pass
        _state["default_removed"] = True
    if _state["sink"] is not None:
        try:
            logger.remove(_state["sink"])
        except ValueError:
            pass
    _state["sink"] = logger.add(
        sys.stderr, level="DEBUG", filter=log_filter, diagnose=False, backtrace=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name} | {message}",
    )
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "aiortc", "aioice", "httpx"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def _safe(value) -> str:
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)
    if isinstance(value, str) and _SAFE_STR.match(value):
        return value
    return f"<redacted:len={len(str(value))}>"


def log_event(event: str, **fields) -> None:
    """Log `event k=v ...` at INFO. Free text in values is written as <redacted:len=N>."""
    parts = [_safe(event)] + [f"{k}={_safe(v)}" for k, v in fields.items()]
    logger.info(" ".join(parts))
