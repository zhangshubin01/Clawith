"""Regression tests for Feishu user-facing domain fallback (Lark international).

Guards the fix that derives the fallback site domain from the API gateway
domain (FEISHU_DOMAIN) instead of hardcoding feishu.cn, which produced
unusable links for international Lark tenants when tenant domain resolution
fails.
"""
import httpx
import pytest

from app.services import agent_tools
from app.services.agent_tools import (
    _feishu_site_domain,
    _get_feishu_bitable_url,
    _get_feishu_tenant_doc_url,
)


def test_site_domain_derivation_from_gateway_domain():
    assert _feishu_site_domain("https://open.larksuite.com") == "larksuite.com"
    assert _feishu_site_domain("https://open.feishu.cn") == "feishu.cn"
    assert _feishu_site_domain("https://open.example.com/") == "example.com"
    assert _feishu_site_domain("not-a-url") == "feishu.cn"


class _FailingAsyncClient:
    """httpx.AsyncClient stand-in that raises on construction."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("network unavailable")


@pytest.mark.asyncio
async def test_doc_url_fallback_uses_lark_site_domain(monkeypatch):
    monkeypatch.setattr(agent_tools, "_FEISHU_BASE", "https://open.larksuite.com")
    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)
    url = await _get_feishu_tenant_doc_url("token", "doc1", doc_type="docx")
    assert url == "https://larksuite.com/docx/doc1"


@pytest.mark.asyncio
async def test_doc_url_fallback_uses_feishu_site_domain(monkeypatch):
    monkeypatch.setattr(agent_tools, "_FEISHU_BASE", "https://open.feishu.cn")
    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)
    url = await _get_feishu_tenant_doc_url("token", "wiki1", doc_type="wiki")
    assert url == "https://feishu.cn/wiki/wiki1"


@pytest.mark.asyncio
async def test_bitable_url_fallback_uses_lark_site_domain(monkeypatch):
    monkeypatch.setattr(agent_tools, "_FEISHU_BASE", "https://open.larksuite.com")
    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)
    url = await _get_feishu_bitable_url("token", "app1", table_id="t1")
    assert url == "https://larksuite.com/base/app1?table=t1"
