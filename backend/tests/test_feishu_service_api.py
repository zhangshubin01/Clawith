import pytest

from app.services import feishu_service as feishu_service_module


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, send_payload: dict | None = None, patch_payload: dict | None = None, get_payload: dict | None = None):
        self._send_payload = send_payload or {"code": 0, "msg": "ok", "data": {"message_id": "m_1"}}
        self._patch_payload = patch_payload or {"code": 0, "msg": "ok"}
        self._get_payload = get_payload or {"code": 0, "msg": "ok", "data": {"items": []}}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **_kwargs):
        if "app_access_token/internal" in url:
            return _FakeResponse(200, {"app_access_token": "token_x"})
        return _FakeResponse(200, self._send_payload)

    async def patch(self, _url, **_kwargs):
        return _FakeResponse(200, self._patch_payload)

    async def get(self, _url, **_kwargs):
        return _FakeResponse(200, self._get_payload)


@pytest.mark.asyncio
async def test_send_message_raises_when_business_code_nonzero(monkeypatch):
    feishu_service_module._shared_client = None  # reset shared client
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(send_payload={"code": 99991663, "msg": "rate limited"}),
    )

    with pytest.raises(RuntimeError, match="code=99991663"):
        await feishu_service_module.feishu_service.send_message(
            "app_id",
            "app_secret",
            "ou_xxx",
            "text",
            "{\"text\":\"hello\"}",
            stage="unit_test_send",
        )


@pytest.mark.asyncio
async def test_patch_message_raises_when_business_code_nonzero(monkeypatch):
    feishu_service_module._shared_client = None  # reset shared client
    monkeypatch.setattr(
        feishu_service_module.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(patch_payload={"code": 10019, "msg": "invalid card content"}),
    )

    with pytest.raises(RuntimeError, match="code=10019"):
        await feishu_service_module.feishu_service.patch_message(
            "app_id",
            "app_secret",
            "om_xxx",
            "{\"content\":\"test\"}",
            stage="unit_test_patch",
        )


@pytest.mark.asyncio
async def test_add_message_reaction_uses_glance_emoji(monkeypatch):
    client = _FakeAsyncClient()
    calls: dict[str, object] = {}

    async def post(url, **kwargs):
        if "app_access_token/internal" in url:
            return _FakeResponse(200, {"app_access_token": "token_x"})
        calls["url"] = url
        calls["kwargs"] = kwargs
        return _FakeResponse(200, {"code": 0, "msg": "ok", "data": {}})

    client.post = post
    monkeypatch.setattr(feishu_service_module.httpx, "AsyncClient", lambda: client)

    await feishu_service_module.feishu_service.add_message_reaction(
        "app_id",
        "app_secret",
        "om_source",
        "GLANCE",
        stage="unit_test_reaction",
    )

    assert calls["url"] == (
        f"{feishu_service_module._FEISHU_BASE}"
        "/open-apis/im/v1/messages/om_source/reactions"
    )
    assert calls["kwargs"]["json"] == {  # type: ignore[index]
        "reaction_type": {"emoji_type": "GLANCE"}
    }


@pytest.mark.asyncio
async def test_list_bot_chats_uses_app_identity_and_parses_groups(monkeypatch):
    client = _FakeAsyncClient(
        get_payload={
            "code": 0,
            "msg": "ok",
            "data": {"items": [{"chat_id": "oc_1", "name": "项目群"}], "has_more": False},
        }
    )
    monkeypatch.setattr(feishu_service_module.httpx, "AsyncClient", lambda **_kwargs: client)

    result = await feishu_service_module.feishu_service.list_bot_chats("app", "secret")

    assert result["data"]["items"][0]["chat_id"] == "oc_1"


class _CountingClient:
    """计数型 httpx.AsyncClient 假件：token 端点 + contact user 端点，可注入前导失败。"""

    def __init__(self, *, fail_token_count: int = 0, fail_user_count: int = 0):
        self.post_count = 0
        self.get_count = 0
        self._fail_token_count = fail_token_count
        self._fail_user_count = fail_user_count

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, _url, json=None, **_kwargs):
        self.post_count += 1
        if self._fail_token_count > 0:
            self._fail_token_count -= 1
            return _FakeResponse(200, {"code": 99991663, "msg": "rate limited"})
        return _FakeResponse(200, {"tenant_access_token": f"tok_{self.post_count}"})

    async def get(self, _url, **_kwargs):
        self.get_count += 1
        if self._fail_user_count > 0:
            self._fail_user_count -= 1
            return _FakeResponse(200, {"code": 99991663, "msg": "contact failed"})
        return _FakeResponse(
            200,
            {
                "code": 0,
                "data": {"user": {"user_id": "uid_1", "name": "Alice"}},
            },
        )


@pytest.mark.asyncio
async def test_tenant_access_token_cached_within_ttl(monkeypatch):
    """同一 (app_id, app_secret) 在 TTL 内复用 token，不再重复打 token 端点。"""
    client = _CountingClient()
    monkeypatch.setattr(feishu_service_module.httpx, "AsyncClient", lambda *a, **k: client)
    service = feishu_service_module.FeishuService()

    first = await service.get_tenant_access_token("app-1", "secret-1")
    second = await service.get_tenant_access_token("app-1", "secret-1")

    assert first == second == "tok_1"
    assert client.post_count == 1


@pytest.mark.asyncio
async def test_tenant_access_token_cache_partitioned_per_app_credentials(monkeypatch):
    """不同应用凭据各自独立缓存，互不串用 token。"""
    client = _CountingClient()
    monkeypatch.setattr(feishu_service_module.httpx, "AsyncClient", lambda *a, **k: client)
    service = feishu_service_module.FeishuService()

    t1a = await service.get_tenant_access_token("app-1", "secret-1")
    t2 = await service.get_tenant_access_token("app-2", "secret-2")
    t1b = await service.get_tenant_access_token("app-1", "secret-1")

    assert client.post_count == 2
    assert t1a == t1b == "tok_1"
    assert t2 == "tok_2"


@pytest.mark.asyncio
async def test_contact_user_cached_hit_skips_second_roundtrip(monkeypatch):
    """同用户连续消息：token 与用户信息均命中缓存，不再打 Contact API。"""
    client = _CountingClient()
    monkeypatch.setattr(feishu_service_module.httpx, "AsyncClient", lambda *a, **k: client)
    service = feishu_service_module.FeishuService()

    first = await service.get_contact_user_cached("app-1", "secret-1", "ou_1")
    second = await service.get_contact_user_cached("app-1", "secret-1", "ou_1")

    assert first == second
    assert first["user_id"] == "uid_1"
    assert client.post_count == 1  # token 复用
    assert client.get_count == 1  # 用户信息复用


@pytest.mark.asyncio
async def test_contact_user_failure_not_cached_and_returns_none(monkeypatch):
    """用户信息拉取失败返回 None 且不缓存（负结果不缓存），下次重试重新拉取。"""
    client = _CountingClient(fail_user_count=1)
    monkeypatch.setattr(feishu_service_module.httpx, "AsyncClient", lambda *a, **k: client)
    service = feishu_service_module.FeishuService()

    assert await service.get_contact_user_cached("app-1", "secret-1", "ou_1") is None
    result = await service.get_contact_user_cached("app-1", "secret-1", "ou_1")

    assert result is not None
    assert client.get_count == 2  # 失败未缓存 → 第二次重新发起 GET
    assert client.post_count == 1  # token 已缓存，不重复获取
