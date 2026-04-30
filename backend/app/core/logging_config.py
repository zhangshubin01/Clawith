"""Centralized logging configuration using loguru."""

from __future__ import annotations

import sys
import threading
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

if TYPE_CHECKING:
    from app.config import Settings

# Context variable for trace ID
trace_id_var: ContextVar[str] = ContextVar("trace_id", default=None)

# Idempotency guard
_config_lock = threading.Lock()
_logging_configured: bool = False
_file_logging_configured: bool = False

# Format constants
_LOG_FORMAT_COLOR = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[trace_id]:-<12}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_LOG_FORMAT_PLAIN = (
    "{time:YYYY-MM-DD HH:mm:ss} | "
    "{level: <8} | "
    "{extra[trace_id]:-<12} | "
    "{name}:{function}:{line} - {message}"
)


NOISY_CONNECTION_LOGGERS = {
    # WebSocket accepted / HTTP access lines from uvicorn.
    "uvicorn.access": logging.WARNING,
    # "connection open" / "connection closed" emitted by websockets.
    "websockets": logging.WARNING,
    "websockets.server": logging.WARNING,
    "websockets.client": logging.WARNING,
    "uvicorn.protocols.websockets.websockets_impl": logging.WARNING,
}


def get_trace_id() -> str:
    """Get current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set trace ID in context."""
    trace_id_var.set(trace_id)


def _ensure_trace_id(record) -> bool:
    """Filter that ensures trace_id is always set and silences noisy request logs."""
    record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4()))
    return _request_noise_filter(record)


# Path prefixes for high-frequency low-value requests to silence
_SILENT_PATH_PREFIXES = ("/api/health", "/api/version", "/")


def _request_noise_filter(record) -> bool:
    """Silence normal 2xx responses for high-frequency paths.

    Non-request logs (without ``request_info``) always pass through.
    Abnormal requests (4xx/5xx, slow ≥ 1 s) always pass through.
    For request logs without status_code (entry logs), silence by path only.
    """
    req = record["extra"].get("request_info")
    if req is None:
        return True  # Non-request log, always pass

    status = req.get("status_code", 0)
    duration = req.get("duration", 0)
    path = req.get("path", "")

    # Always log errors and slow requests
    if status >= 400 or duration >= 1.0:
        return True

    # Silence health checks and root path
    if any(path.startswith(p) for p in _SILENT_PATH_PREFIXES):
        return False

    return True


def configure_logging():
    """Configure loguru stdout handler.

    Called at module import time — MUST NOT call get_settings() to avoid
    circular imports.  Uses hardcoded defaults; call configure_file_logging()
    from lifespan to apply settings-based configuration.
    """
    global _logging_configured
    with _config_lock:
        if _logging_configured:
            return
        _logging_configured = True

        # Remove default handler
        logger.remove()

        # Add stdout handler with hardcoded defaults (safe for early import)
        logger.add(
            sys.stdout,
            level="INFO",
            format=_LOG_FORMAT_COLOR,
            enqueue=True,
            backtrace=True,
            diagnose=False,
            filter=_ensure_trace_id,
        )

        # Intercept stdlib logging immediately
        _intercept_standard_logging()


def configure_file_logging(settings: Settings) -> None:
    """Add file handler based on settings. Called from lifespan after settings are available.

    Idempotent: safe to call multiple times — only configures once.
    Brief no-handler window during reconfiguration — acceptable at startup only.
    """
    global _file_logging_configured
    with _config_lock:
        if _file_logging_configured:
            return
        _file_logging_configured = True

        if not settings.LOG_DIR:
            # Docker mode: stdout only (Docker json-file driver handles persistence)
            # Re-intercept to capture loggers loaded after the initial configure_logging()
            # call (e.g., lark-oapi "Lark" logger which retains its own StreamHandler)
            _intercept_standard_logging()
            return

        log_path = Path(settings.LOG_DIR)
        try:
            log_path.mkdir(parents=True, exist_ok=True, mode=0o750)
        except OSError:
            logger.warning(
                f"[logging] Cannot create log dir {log_path}, file logging disabled"
            )
            return

        # Remove all handlers and re-add with settings-based configuration
        logger.remove()
        _add_stdout_handler(settings)
        _add_file_handler(settings, log_path)
        _intercept_standard_logging()  # Re-register after remove()


def _add_stdout_handler(settings: Settings) -> None:
    """Add stdout handler with settings-based configuration."""
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=_LOG_FORMAT_COLOR,
        enqueue=True,
        backtrace=True,
        diagnose=settings.LOG_DIAGNOSE,
        filter=_ensure_trace_id,
    )


def _add_file_handler(settings: Settings, log_path: Path) -> None:
    """Add file handler with rotation/retention/compression.

    enqueue=True provides thread-safety only (not multi-process safe).
    Current deployment is single-process, so this is sufficient.
    """
    logger.add(
        str(log_path / "clawith_{time:YYYY-MM-DD}.log"),
        level=settings.LOG_LEVEL,
        format=_LOG_FORMAT_PLAIN,
        rotation=settings.LOG_ROTATION,
        retention=settings.LOG_RETENTION,
        compression=settings.LOG_COMPRESSION,
        enqueue=True,
        backtrace=True,
        diagnose=settings.LOG_DIAGNOSE,
        filter=_ensure_trace_id,
        encoding="utf-8",
    )


def quiet_noisy_connection_loggers() -> None:
    """Reduce chatty transport-level logs while keeping warnings/errors visible."""
    for logger_name, level in NOISY_CONNECTION_LOGGERS.items():
        target = logging.getLogger(logger_name)
        target.setLevel(level)


def _intercept_standard_logging():
    """Redirect standard library logging to loguru."""

    class InterceptHandler(logging.Handler):
        def emit(self, record):
            # Get corresponding loguru level
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find the caller's frame
            frame, depth = logging.currentframe(), 2
            while frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            # Capture the message safely
            try:
                message = record.getMessage()
            except (TypeError, ValueError):
                # Fallback if formatting fails (e.g. third party lib bug)
                if record.args:
                    message = f"{record.msg} [args={record.args}]"
                else:
                    message = record.msg

            logger.opt(depth=depth, exception=record.exc_info).log(level, message)

    # Use a single handler instance for all loggers
    handler = InterceptHandler()
    # Replace all standard logger handlers (snapshot dict keys to avoid RuntimeError)
    logging.basicConfig(handlers=[handler], level=0, force=True)
    for name in list(logging.root.manager.loggerDict):
        logging.getLogger(name).handlers = [handler]
        logging.getLogger(name).propagate = False
    quiet_noisy_connection_loggers()

    # Suppress noisy third-party loggers
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Uvicorn access log is redundant with TraceIdMiddleware
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# Keep backward-compatible public name
intercept_standard_logging = _intercept_standard_logging

# Configure on import (no assignment — logger is already imported from loguru)
configure_logging()
