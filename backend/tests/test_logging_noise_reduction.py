"""Logging-noise reduction contracts.

The LangGraph retry loop logs every safe-read tool retry with a full
``exc_info`` traceback (``langgraph.pregel._retry``). In a batch heartbeat
burst those retries — which are expected control flow, not errors — printed
56 full tracebacks in two minutes. The retry outcome is already recorded in
the tool ledger and the final reply, so the INFO retry line is pure noise and
must be quieted while WARNING/ERROR from the same module stay visible.
"""

import logging

from app.core.logging_config import NOISY_CONNECTION_LOGGERS, quiet_noisy_connection_loggers


def test_langgraph_retry_logger_is_quieted_to_warning():
    assert NOISY_CONNECTION_LOGGERS.get("langgraph.pregel._retry") == logging.WARNING


def test_quiet_noisy_connection_loggers_sets_retry_logger_level():
    quiet_noisy_connection_loggers()

    assert logging.getLogger("langgraph.pregel._retry").level == logging.WARNING


def test_quiet_noisy_connection_loggers_keeps_noisy_loggers_listed():
    # Every entry must map to a stdlib logging level so setLevel never blows up.
    for name, level in NOISY_CONNECTION_LOGGERS.items():
        assert isinstance(name, str) and name
        assert level in {logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL}
