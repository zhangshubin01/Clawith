"""ACP 插件单元测试 — document store、特性开关、providers 映射。"""

from __future__ import annotations

import pytest

from app.plugins.clawith_acp.acp_document import (
    document_store,
    handle_document_notification,
)
from app.plugins.clawith_acp.acp_features import acp_feature_enabled
from app.plugins.clawith_acp.acp_nes import handle_nes_suggest, nes_enabled


@pytest.fixture(autouse=True)
def _clear_document_store():
    document_store.clear_session("sess-test")
    yield
    document_store.clear_session("sess-test")


def test_document_did_open_and_format(monkeypatch):
    monkeypatch.setenv("ACP_FEATURES", "document")
    handle_document_notification("sess-test", "document/didOpen", {
        "uri": "file:///proj/Foo.kt", "languageId": "kotlin", "version": 1,
    })
    handle_document_notification("sess-test", "document/didFocus", {
        "uri": "file:///proj/Foo.kt",
    })
    ctx = document_store.format_for_prompt("sess-test")
    assert "Foo.kt" in ctx
    assert "focused" in ctx


def test_document_snapshot_apply():
    document_store.apply_snapshot("sess-test", {
        "openUris": ["file:///a.py", "file:///b.py"],
        "focusedUri": "file:///b.py",
        "languageIds": {"file:///a.py": "python", "file:///b.py": "python"},
    })
    ctx = document_store.format_for_prompt("sess-test")
    assert "file:///b.py" in ctx
    assert "Open Documents" in ctx


def test_acp_feature_toggle(monkeypatch):
    monkeypatch.setenv("ACP_FEATURES", "document")
    assert acp_feature_enabled("document") is True
    monkeypatch.setenv("ACP_FEATURES", "")
    assert acp_feature_enabled("document") is False


def test_nes_suggest_empty_when_disabled(monkeypatch):
    monkeypatch.setenv("ACP_NES_ENABLED", "0")
    assert nes_enabled() is False


@pytest.mark.asyncio
async def test_nes_suggest_returns_empty():
    result = await handle_nes_suggest("sess-test", {})
    assert result == {"suggestions": []}
