"""Logging as key=value lines, to the console and to <output dir>/logs/pipeline.log.

Example line:
    2026-11-08T07:30:00Z level=INFO event=run_finished run_id=run-... status=SUCCESS rows_read=12
"""

import logging
import sys
import time
from pathlib import Path

LOGGER_NAME = "inventory_pipeline"


class UtcFormatter(logging.Formatter):
    """Formats log times in UTC as 'YYYY-MM-DDTHH:MM:SSZ'."""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s level=%(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")


def setup_logging(output_dir: Path) -> logging.Logger:
    """Configure the pipeline logger to write to the console and to output_dir/logs/pipeline.log.

    Safe to call more than once: earlier handlers are closed and replaced.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_dir = Path(output_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(UtcFormatter())
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_dir / "pipeline.log", encoding="utf-8")
    file_handler.setFormatter(UtcFormatter())
    logger.addHandler(file_handler)
    return logger


def close_logging() -> None:
    """Close the pipeline logger's handlers (releases the log file, needed on Windows)."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def format_value(value: object) -> str:
    """Render one value; values containing spaces or '=' are wrapped in double quotes."""
    text = "" if value is None else str(value)
    if text == "" or any(ch in text for ch in ' ="'):
        return '"' + text.replace('"', "'") + '"'
    return text


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: object) -> None:
    """Write one key=value log line: event=<event> followed by the given fields in order."""
    parts = [f"event={event}"] + [f"{key}={format_value(value)}" for key, value in fields.items()]
    logger.log(level, " ".join(parts))


def get_logger() -> logging.Logger:
    """Return the pipeline logger (silent unless setup_logging was called)."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
    return logger
