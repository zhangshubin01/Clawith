"""Tests for the startup PostgreSQL connection budget check."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from loguru import logger
import pytest

from app.config import Settings
from app.database import warn_on_connection_budget


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        DB_POOL_SIZE=8,
        DB_MAX_OVERFLOW=4,
        DB_RESERVED_CONNECTIONS=20,
        CHECKPOINT_POOL_MAX_SIZE=4,
        **overrides,
    )


class _StubEngine:
    """Minimal async engine double returning a fixed max_connections value."""

    def __init__(self, max_connections: str | None, fail: bool = False) -> None:
        self._max_connections = max_connections
        self._fail = fail

    def connect(self) -> _StubEngine:
        return self

    async def __aenter__(self) -> _StubEngine:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, stmt: object) -> MagicMock:
        if self._fail:
            raise OSError("database down")
        result = MagicMock()
        result.scalar_one.return_value = self._max_connections
        return result


@pytest.fixture
def _captured_logs():
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    yield captured
    logger.remove(sink_id)


@pytest.mark.asyncio
async def test_budget_exceeded_logs_error(_captured_logs: list[str]) -> None:
    with patch("app.database.engine", _StubEngine("30")), patch("app.database.settings", _settings()):
        await warn_on_connection_budget()

    assert any("connection budget EXCEEDED" in line for line in _captured_logs)
    assert any("max_connections=30" in line for line in _captured_logs)


@pytest.mark.asyncio
async def test_tight_budget_logs_warning(_captured_logs: list[str]) -> None:
    with patch("app.database.engine", _StubEngine("40")), patch("app.database.settings", _settings()):
        # demand = 8 + 4 + 4 + 20 = 36 > 80% of 40
        await warn_on_connection_budget()

    assert any("connection budget is tight" in line for line in _captured_logs)


@pytest.mark.asyncio
async def test_healthy_budget_logs_ok(_captured_logs: list[str]) -> None:
    with patch("app.database.engine", _StubEngine("100")), patch("app.database.settings", _settings()):
        await warn_on_connection_budget()

    assert any("connection budget OK" in line for line in _captured_logs)


@pytest.mark.asyncio
async def test_unreachable_database_skips_quietly(_captured_logs: list[str]) -> None:
    with patch("app.database.engine", _StubEngine(None, fail=True)), patch("app.database.settings", _settings()):
        await warn_on_connection_budget()

    assert any("connection budget check skipped" in line for line in _captured_logs)
    assert not any("EXCEEDED" in line for line in _captured_logs)
