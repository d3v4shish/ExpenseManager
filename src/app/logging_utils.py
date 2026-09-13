from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.app.files import RuntimeFiles
from src.app.settings import SettingsService

_LOGGER_SENTINEL = "_expense_manager_logging_configured"


def configure_app_logging(root_dir: Path) -> Path:
    """Configure the desktop app's shared console and file logging once."""

    files = RuntimeFiles(Path(root_dir))
    log_path = files.log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    existing_path = getattr(root_logger, _LOGGER_SENTINEL, None)
    if existing_path:
        return Path(existing_path)

    settings = SettingsService(files).load().get("logging", {})
    level_name = str(os.environ.get("NOC_LOG_LEVEL", settings.get("level", "INFO")) or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    root_logger.setLevel(level)
    root_logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=int(settings.get("maxBytes", 2_000_000) or 2_000_000),
        backupCount=int(settings.get("backupCount", 5) or 5),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    sys.excepthook = _build_exception_hook(root_logger)
    setattr(root_logger, _LOGGER_SENTINEL, str(log_path))
    return log_path


def apply_logging_settings(settings: dict) -> None:
    """Apply validated GUI logging settings to the active handlers."""

    root_logger = logging.getLogger()
    level_name = str(os.environ.get("NOC_LOG_LEVEL", settings.get("level", "INFO")) or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)
        if isinstance(handler, RotatingFileHandler):
            handler.maxBytes = int(settings.get("maxBytes", handler.maxBytes) or handler.maxBytes)
            handler.backupCount = int(settings.get("backupCount", handler.backupCount) or handler.backupCount)


def _build_exception_hook(root_logger: logging.Logger):
    """Return one excepthook that logs uncaught exceptions before delegating."""

    default_hook = sys.__excepthook__

    def hook(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            default_hook(exc_type, exc_value, exc_traceback)
            return
        root_logger.critical(
            "Unhandled desktop-app exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        default_hook(exc_type, exc_value, exc_traceback)

    return hook
