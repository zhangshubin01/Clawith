"""Regression tests for ``_lazy_reset_token_counters``.

Guards the cache_miss counter reset bug: ``cache_miss_tokens_today`` and
``cache_miss_tokens_month`` were added later than the other counters and were
missing from the reset function, so they accumulated across days/months
forever while the sibling columns reset correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.agents import _lazy_reset_token_counters

_COUNTER_COLUMNS = (
    "tokens_used",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_miss_tokens",
)


def _agent(
    last_daily: datetime | None,
    last_monthly: datetime | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        last_daily_reset=last_daily,
        last_monthly_reset=last_monthly,
        tokens_used_today=111,
        tokens_used_month=222,
        cache_read_tokens_today=333,
        cache_read_tokens_month=444,
        cache_creation_tokens_today=555,
        cache_creation_tokens_month=666,
        cache_miss_tokens_today=777,
        cache_miss_tokens_month=888,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _yesterday() -> datetime:
    return _now() - timedelta(days=1)


def _last_month() -> datetime:
    return _now() - timedelta(days=32)


def _assert_today_counters_zeroed(agent: SimpleNamespace) -> None:
    for column in _COUNTER_COLUMNS:
        assert getattr(agent, f"{column}_today") == 0, f"{column}_today not reset"


def _assert_month_counters_zeroed(agent: SimpleNamespace) -> None:
    for column in _COUNTER_COLUMNS:
        assert getattr(agent, f"{column}_month") == 0, f"{column}_month not reset"


@pytest.mark.asyncio
async def test_daily_reset_zeroes_all_today_counters_including_cache_miss() -> None:
    """The bug: daily reset left cache_miss_tokens_today accumulating forever."""
    agent = _agent(last_daily=_yesterday(), last_monthly=_now())

    changed = await _lazy_reset_token_counters(agent, SimpleNamespace())

    assert changed is True
    _assert_today_counters_zeroed(agent)
    # Month counters are untouched on a daily-only reset.
    assert agent.tokens_used_month == 222
    assert agent.cache_miss_tokens_month == 888
    assert agent.last_daily_reset.date() == _now().date()


@pytest.mark.asyncio
async def test_daily_reset_skips_when_same_day() -> None:
    agent = _agent(last_daily=_now(), last_monthly=_now())

    changed = await _lazy_reset_token_counters(agent, SimpleNamespace())

    assert changed is False
    assert agent.tokens_used_today == 111
    assert agent.cache_miss_tokens_today == 777
    assert agent.cache_miss_tokens_month == 888


@pytest.mark.asyncio
async def test_monthly_reset_zeroes_all_month_counters_including_cache_miss() -> None:
    """The bug: monthly reset left cache_miss_tokens_month accumulating forever."""
    agent = _agent(last_daily=_now(), last_monthly=_last_month())

    changed = await _lazy_reset_token_counters(agent, SimpleNamespace())

    assert changed is True
    _assert_month_counters_zeroed(agent)
    # Today counters are untouched on a monthly-only reset.
    assert agent.tokens_used_today == 111
    assert agent.cache_miss_tokens_today == 777
    assert agent.last_monthly_reset.date() == _now().date()


@pytest.mark.asyncio
async def test_monthly_reset_skips_when_same_month() -> None:
    agent = _agent(last_daily=_now(), last_monthly=_now())

    changed = await _lazy_reset_token_counters(agent, SimpleNamespace())

    assert changed is False
    assert agent.tokens_used_month == 222
    assert agent.cache_miss_tokens_month == 888


@pytest.mark.asyncio
async def test_reset_zeroes_both_today_and_month_when_both_expired() -> None:
    agent = _agent(last_daily=_yesterday(), last_monthly=_last_month())

    changed = await _lazy_reset_token_counters(agent, SimpleNamespace())

    assert changed is True
    _assert_today_counters_zeroed(agent)
    _assert_month_counters_zeroed(agent)
