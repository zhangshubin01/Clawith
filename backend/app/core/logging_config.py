"""Centralized logging configuration using loguru."""

import sys
import logging
from contextvars import ContextVar

from loguru import logger

# Context variable for trace ID
from uuid import uuid4

trace_id_var: ContextVar[str] = ContextVar("trace_id", default=None)


NOISY_CONNECTION_LOGGERS = {
    # WebSocket accepted / HTTP access lines from uvicorn.
    "uvicorn.access": logging.WARNING,
    # "connection open" / "connection closed" emitted by websockets.
    "websockets": logging.WARNING,
    "websockets.server": logging.WARNING,
    "websockets.client": logging.WARNING,
    "uvicorn.protocols.websockets.websockets_impl": logging.WARNING,
    # Supress "Failed to parse headers" warning from urllib3 when interacting with MinIO.
    "urllib3.connection": logging.ERROR,
    # "HTTP Request: POST https://api.deepseek.com/chat/completions 200 OK"
    # emitted by httpx/httpcore for every model call and tool HTTP request.
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    # LangGraph logs every safe-read tool retry with a full exc_info traceback.
    # Retries are expected control flow (not errors); their outcome is already
    # recorded in the tool ledger, so the INFO retry line is noise — quiet it
    # while keeping any WARNING/ERROR from the same module visible.
    "langgraph.pregel._retry": logging.WARNING,
}


def get_trace_id() -> str:
    """Get current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set trace ID in context."""
    trace_id_var.set(trace_id)


def new_trace_id() -> str:
    """Generate a new 12-char trace ID and bind it to the current context.

    Intended for background tasks that run outside HTTP/WebSocket request
    scopes so that all log lines produced by one task execution share the
    same trace_id.
    """
    tid = uuid4().hex[:12]
    set_trace_id(tid)
    return tid


def _disable_agentbay_logger_override():
    """Disable AgentBay SDK's logging override to prevent it from resetting loguru."""
    if "agentbay._common.logger" in sys.modules:
        try:
            from agentbay._common.logger import AgentBayLogger
            AgentBayLogger._initialized = True
            AgentBayLogger.setup = classmethod(lambda cls, *args, **kwargs: None)
        except Exception:
            pass


def configure_logging():
    """Configure loguru with custom format including trace ID."""
    # Remove default handler
    logger.remove()

    # Add stdout handler with custom format and filter to ensure trace_id exists
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{extra[trace_id]:-<12}</cyan> | <cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>{exception}",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: (record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None)
    )

    _disable_agentbay_logger_override()

    return logger


def quiet_noisy_connection_loggers() -> None:
    """Reduce chatty transport-level logs while keeping warnings/errors visible."""
    for logger_name, level in NOISY_CONNECTION_LOGGERS.items():
        target = logging.getLogger(logger_name)
        target.setLevel(level)


def intercept_standard_logging():
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

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, message
            )

    # Replace all standard logger handlers
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).handlers = [InterceptHandler()]
        logging.getLogger(name).propagate = False
    quiet_noisy_connection_loggers()


# Configure on import.
configured_logger = configure_logging()
