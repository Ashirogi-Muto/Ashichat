"""Logging setup for AshiChat.

**File-only logging.**  ``stdout`` / ``stderr`` are reserved for the TUI.
No handler must ever write to the console.

Debug log: ``~/.ashichat/debug.log``
Rotation:  10 MB max, 5 kept (debug.log, debug.log.1, …)
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ashichat.config import AshiChatConfig

# 10 MB
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(config: AshiChatConfig) -> None:
    """Configure the root logger to write **only** to the debug log file.

    Must be called once at startup, before any ``get_logger`` usage.
    """
    log_path = config.base_dir / "debug.log"

    handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FORMAT))

    root = logging.getLogger()
    # Remove any existing handlers (e.g. default StreamHandler)
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.debug.log_level, logging.INFO))


def get_logger(name: str) -> logging.Logger:
    """Return a child logger.  Use as ``log = get_logger(__name__)``."""
    return logging.getLogger(name)
