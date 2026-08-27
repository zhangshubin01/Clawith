"""Feishu messages must be accepted by the durable Runtime before acknowledgement."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
import uuid

import pytest

from app.api import feishu
from app.services import channel_session
from app.services import agent_tools
from app.services.agent_runtime.chat_intake import ChatRuntimeIntake
from app.services.agent_runtime.contracts import RunHandle, RuntimeEventCursor


class _Scalars:
    def __init__(self, value: object) -> None:
        self.value = value

    def first(self):
        return self.value


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return _Scalars(self.value)


class _Session:
    def __init__(self, value: object) -> None:
        self.value = value
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, _statement):
        return _Result(self.value)

    async def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = iter(sessions)

    def __call__(self):
        return next(self.sessions)


class _RecordingBridge:
    """Fake CardStreamBridge that records lifecycle calls into a shared list."""

    def __init__(self, events: list[str], **kwargs):
        self.events = events

    async def start(self):
        self.events.append("card_start")

    async def withdraw(self):
        self.events.append("withdraw")


def test_feishu_callback_rejects_missing_or_mismatched_verification_token() -> None:
    config = SimpleNamespace(verification_token="expected", encrypt_key="")

    assert feishu._verify_and_decode_feishu_callback(
        b'{"header":{"token":"unexpected"}}', {}, config  # type: ignore[arg-type]
    ) is None


def test_feishu_callback_accepts_a_matching_verification_token() -> None:
    config = SimpleNamespace(verification_token="expected", encrypt_key="")
    payload = b'{"header":{"token":"expected","event_type":"im.message.receive_v1"}}'

    assert feishu._verify_and_decode_feishu_callback(payload, {}, config) == {
        "header": {"token": "expected", "event_type": "im.message.receive_v1"}
    }


def test_feishu_callback_rejects_an_invalid_signed_request() -> None:
    config = SimpleNamespace(verification_token="expected", encrypt_key="encrypt-key")
    payload = b'{"header":{"token":"expected","event_type":"im.message.receive_v1"}}'
    headers = {
        "x-lark-request-timestamp": "1",
        "x-lark-request-nonce": "2",
        "x-lark-signature": "invalid",
    }

    assert feishu._verify_and_decode_feishu_callback(payload, headers, config) is None

    headers["x-lark-signature"] = hashlib.sha256(
        b"12encrypt-key" + payload
    ).hexdigest()
    assert feishu._verify_and_decode_feishu_callback(payload, headers, config) is not None


def _runtime(tenant_id: uuid.UUID) -> ChatRuntimeIntake:
    run_id = uuid.uuid4()
    return ChatRuntimeIntake(
        handle=RunHandle(
            tenant_id=tenant_id,
            run_id=run_id,
            thread_id=str(run_id),
            command_id=uuid.uuid4(),
            runtime_type="langgraph",
            created=True,
        ),
        message_id=uuid.uuid4(),
        resumed=False,
        stream_after=RuntimeEventCursor(
            created_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            event_id=uuid.uuid4(),
        ),
    )


@pytest.mark.asyncio
async def test_feishu_group_message_uses_runtime_intake(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    event_id = f"feishu-event-{uuid.uuid4()}"
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Runtime Agent",
    )
    user = SimpleNamespace(id=user_id, display_name="Alice")
    session = SimpleNamespace(id=session_id)
    model = SimpleNamespace(id=uuid.uuid4())
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _OrderedSession(agent, None)  # 查询1=agent, 查询2=resume 检查(无进行中 run)
    intake = _runtime(tenant_id)
    calls: dict[str, object] = {}

    async def resolve_sender(_db, **_kwargs):
        await asyncio.sleep(0)  # 让出事件循环，让提前 fire 的建卡任务得以运行
        return user

    async def find_session(**kwargs):
        calls["session"] = kwargs
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        calls["intake"] = kwargs
        return intake

    from app.services.agent_runtime import card_stream_bridge as bridge_module

    bridge_events: list[str] = []

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu, "_load_agent_and_model", load_model)
    monkeypatch.setattr(feishu, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(
        bridge_module, "CardStreamBridge", partial(_RecordingBridge, bridge_events),
    )
    monkeypatch.setattr(bridge_module, "register_bridge", lambda *_args: None)
    monkeypatch.setattr(bridge_module, "unregister_bridge", lambda *_args: None)

    result = await feishu._accept_feishu_runtime_message(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="group",
        chat_id="oc_group_1",
        content="Hello Feishu",
        display_content="Hello Feishu",
        external_event_id=event_id,
    )

    assert db.commits == 1
    assert result is intake
    assert bridge_events == ["card_start"]  # 卡片先建、无撤回（非 resume）
    session_call = calls["session"]
    assert isinstance(session_call, dict)
    assert session_call["is_group"] is True
    assert session_call["created_by_user_id"] == user_id
    intake_call = calls["intake"]
    assert isinstance(intake_call, dict)
    assert intake_call["content"] == (
        "[飞书发送者: Alice | user_id: feishu-user-1 | open_id: ou_sender] "
        "Hello Feishu"
    )
    assert intake_call["display_content"] == "Hello Feishu"
    assert intake_call["runtime_instruction"] == (
        "You are passively listening in a Feishu group. A message directly addresses you if it "
        "@mentions you, names you or your Agent name, asks you a question or gives you an "
        "instruction, or explicitly asks you to reply. You must visibly answer every directly "
        "addressed message even when it is outside your usual responsibilities. For messages "
        "that do not directly address you, reply normally only when your responsibilities require "
        "a visible response; otherwise your entire final response must be exactly NO_REPLY, with "
        "no other text. Your final response is automatically delivered to the input Feishu group. "
        "Never call send_channel_message to reply to the current conversation. Use that Tool only "
        "when the user explicitly asks you to send a separate message to another person or group, "
        "and then set cross_session_confirmed=true."
    )
    assert intake_call["channel_delivery_target"] == {
        "receive_id": "oc_group_1",
        "receive_id_type": "chat_id",
        # 卡片流式: app_id/app_secret 随 delivery_target 传入 intake，
        # chat_intake 入库前会剥离 app_secret（仅保留 app_id）
        "_card_config": {"app_id": "app-1", "app_secret": "secret-1"},
        "source_message_id": event_id,
    }
    assert intake_call["message_id"] == feishu.channel_message_id(
        agent_id,
        "feishu",
        event_id,
    )


@pytest.mark.asyncio
async def test_send_channel_message_rejects_unconfirmed_cross_session_target() -> None:
    result = await agent_tools.execute_tool(
        "send_channel_message",
        {
            "channel": "feishu",
            "target_recipient_id": str(uuid.uuid4()),
            "message": "wrong group",
        },
        uuid.uuid4(),
        uuid.uuid4(),
        session_id=str(uuid.uuid4()),
    )

    assert result.startswith("❌ Cross-Session channel delivery rejected")

    typed = await agent_tools.execute_builtin_tool_outcome(
        "send_channel_message",
        {
            "channel": "feishu",
            "target_recipient_id": str(uuid.uuid4()),
            "message": "wrong group",
        },
        uuid.uuid4(),
        uuid.uuid4(),
        session_id=str(uuid.uuid4()),
    )
    assert typed.error_code == "cross_session_delivery_not_confirmed"


@pytest.mark.asyncio
async def test_feishu_event_commits_runtime_before_provider_ack(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    event_id = f"feishu-event-{uuid.uuid4()}"
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    intake = _runtime(tenant_id)
    config_db = _Session(config)
    calls: dict[str, object] = {}

    async def accept(**kwargs):
        calls["accept"] = kwargs
        return intake

    feishu._processed_events.discard(event_id)
    monkeypatch.setattr(feishu, "_async_session", _SessionFactory(config_db))
    monkeypatch.setattr(feishu, "_accept_feishu_runtime_message", accept)

    result = await feishu.process_feishu_event(
        agent_id,
        {
            "header": {
                "event_id": event_id,
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_sender",
                        "user_id": "feishu-user-1",
                    }
                },
                "message": {
                    "message_id": "om_message_1",
                    "message_type": "text",
                    "chat_type": "p2p",
                    "chat_id": "oc_chat_1",
                    "content": '{"text":"Hello Feishu"}',
                },
            },
        },
    )

    assert result == {"code": 0, "msg": "ok"}
    assert event_id in feishu._processed_events
    accepted = calls["accept"]
    assert isinstance(accepted, dict)
    assert accepted["external_event_id"] == "om_message_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("@_user_1 FYI", "@Runtime Agent FYI"),
        ("@_user_1", "@Runtime Agent"),
    ),
)
async def test_feishu_event_restores_structured_mentions_before_runtime_intake(
    monkeypatch,
    text,
    expected,
) -> None:
    agent_id = uuid.uuid4()
    event_id = f"feishu-event-{uuid.uuid4()}"
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    config_db = _Session(config)
    calls: dict[str, object] = {}

    async def accept(**kwargs):
        calls["accept"] = kwargs
        return _runtime(uuid.uuid4())

    feishu._processed_events.discard(event_id)
    monkeypatch.setattr(feishu, "_async_session", _SessionFactory(config_db))
    monkeypatch.setattr(feishu, "_accept_feishu_runtime_message", accept)

    result = await feishu.process_feishu_event(
        agent_id,
        {
            "header": {
                "event_id": event_id,
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_sender",
                        "user_id": "feishu-user-1",
                    }
                },
                "message": {
                    "message_id": "om_message_mention",
                    "message_type": "text",
                    "chat_type": "group",
                    "chat_id": "oc_group_1",
                    "content": json.dumps({"text": text}),
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "id": {"open_id": "ou_runtime_agent"},
                            "name": "Runtime Agent",
                        }
                    ],
                },
            },
        },
    )

    assert result == {"code": 0, "msg": "ok"}
    accepted = calls["accept"]
    assert isinstance(accepted, dict)
    assert accepted["content"] == expected
    assert accepted["display_content"] == expected


@pytest.mark.asyncio
async def test_feishu_post_at_tag_preserves_visible_mention_name(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    event_id = f"feishu-event-{uuid.uuid4()}"
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    calls: dict[str, object] = {}

    async def accept(**kwargs):
        calls["accept"] = kwargs
        return _runtime(uuid.uuid4())

    feishu._processed_events.discard(event_id)
    monkeypatch.setattr(feishu, "_async_session", _SessionFactory(_Session(config)))
    monkeypatch.setattr(feishu, "_accept_feishu_runtime_message", accept)

    result = await feishu.process_feishu_event(
        agent_id,
        {
            "header": {
                "event_id": event_id,
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_sender",
                        "user_id": "feishu-user-1",
                    }
                },
                "message": {
                    "message_id": "om_post_mention",
                    "message_type": "post",
                    "chat_type": "group",
                    "chat_id": "oc_group_1",
                    "content": json.dumps(
                        {
                            "content": [
                                [
                                    {
                                        "tag": "at",
                                        "user_id": "ou_runtime_agent",
                                        "user_name": "  Runtime\nAgent  ",
                                    },
                                    {"tag": "text", "text": " FYI"},
                                ]
                            ]
                        }
                    ),
                },
            },
        },
    )

    assert result == {"code": 0, "msg": "ok"}
    accepted = calls["accept"]
    assert isinstance(accepted, dict)
    assert accepted["content"] == "@Runtime Agent FYI"
    assert accepted["display_content"] == "@Runtime Agent FYI"


@pytest.mark.asyncio
async def test_feishu_image_keeps_base64_out_of_display_content(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    agent = SimpleNamespace(
        id=agent_id, tenant_id=uuid.uuid4(), creator_id=uuid.uuid4(), name="A",
    )
    calls: dict[str, object] = {}

    async def download(*_args):
        return b"image-bytes"

    async def store(*_args, **_kwargs):
        return "key", "workspace/uploads/image.jpg", SimpleNamespace()

    async def accept(**kwargs):
        calls["accept"] = kwargs
        return SimpleNamespace()

    def fire_early(*_args, **_kwargs):
        # 文件路径提前建卡：此处不关心桥，返回占位三元组即可
        return SimpleNamespace(), lambda *_a: None, lambda *_a: None

    monkeypatch.setattr(feishu, "_async_session", lambda: _Session(agent))
    monkeypatch.setattr(feishu, "_fire_early_card", fire_early)
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu, "store_agent_upload", store)
    monkeypatch.setattr(feishu, "_accept_feishu_runtime_message", accept)

    result = await feishu._accept_feishu_file_runtime(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        message={
            "message_id": "om_image_1",
            "message_type": "image",
            "content": '{"image_key":"img_12345678"}',
        },
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="p2p",
        chat_id="oc_chat_1",
        external_event_id="event-1",
    )

    assert result is not None
    accepted = calls["accept"]
    assert isinstance(accepted, dict)
    assert accepted["display_content"] == "[file:image_12345678.jpg]"
    assert "base64," in accepted["content"]
    assert "base64," not in accepted["display_content"]


@pytest.mark.asyncio
async def test_feishu_file_card_fires_before_download_completes(monkeypatch) -> None:
    """文件消息：卡片在下载开始前 fire，accept 复用同一 bridge/run_id 不重建。"""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=uuid.uuid4(), creator_id=uuid.uuid4(), name="A",
    )
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    events: list[str] = []
    registered: list = []
    fire_kwargs: dict = {}
    accepted: dict = {}

    async def download(*_args):
        await asyncio.sleep(0)  # 模拟下载 RTT：让已 fire 的卡片任务先跑
        events.append("download")
        return b"file-bytes"

    async def store(*_args, **_kwargs):
        return "key", "workspace/uploads/report.txt", SimpleNamespace()

    async def accept(**kwargs):
        events.append("accept")
        accepted.update(kwargs)
        return SimpleNamespace()

    def fire_early(**kwargs):
        events.append("card_fired")
        fire_kwargs.update(kwargs)
        bridge = _RecordingBridge(events)
        task = asyncio.create_task(bridge.start())
        task.add_done_callback(lambda t: t.exception())
        return bridge, lambda *a: registered.append(a), lambda *_a: None

    monkeypatch.setattr(feishu, "_async_session", lambda: _Session(agent))
    monkeypatch.setattr(feishu, "_fire_early_card", fire_early)
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu, "store_agent_upload", store)
    monkeypatch.setattr(feishu, "_accept_feishu_runtime_message", accept)

    result = await feishu._accept_feishu_file_runtime(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        message={
            "message_id": "om_file_1",
            "message_type": "file",
            "content": json.dumps({"file_key": "file_abc", "file_name": "report.txt"}),
        },
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="p2p",
        chat_id="oc_chat_1",
        external_event_id="event-1",
    )

    assert result is not None
    # 建卡 fire 严格早于下载完成；卡片任务先于 accept 运行
    assert events.index("card_fired") < events.index("download") < events.index("accept")
    assert events.index("card_start") < events.index("accept")
    # accept 复用提前生成的 run_id 与 bridge，不重复建卡
    assert isinstance(accepted["pregenerated_run_id"], uuid.UUID)
    assert accepted["pregenerated_run_id"] == fire_kwargs["pregenerated_run_id"]
    assert accepted["early_card"] is not None
    assert fire_kwargs["agent"] is agent


@pytest.mark.asyncio
async def test_feishu_file_download_failure_withdraws_early_card(monkeypatch) -> None:
    """文件下载失败：提前建的卡片被撤回（含注册表清理），提示已发送，不入队。"""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=uuid.uuid4(), creator_id=uuid.uuid4(), name="A",
    )
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    events: list[str] = []
    unregistered: list = []
    sent: list = []

    async def download(*_args):
        raise RuntimeError("download exploded")

    async def send_message(app_id, app_secret, target, msg_type, content, **kwargs):
        sent.append((target, msg_type, json.loads(content), kwargs))

    async def accept(**_kwargs):
        raise AssertionError("download failure must not reach intake")

    def fire_early(**_kwargs):
        bridge = _RecordingBridge(events)
        task = asyncio.create_task(bridge.start())
        task.add_done_callback(lambda t: t.exception())
        return bridge, lambda *_a: None, lambda *a: unregistered.append(a)

    monkeypatch.setattr(feishu, "_async_session", lambda: _Session(agent))
    monkeypatch.setattr(feishu, "_fire_early_card", fire_early)
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu.feishu_service, "send_message", send_message)
    monkeypatch.setattr(feishu, "_accept_feishu_runtime_message", accept)

    result = await feishu._accept_feishu_file_runtime(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        message={
            "message_id": "om_file_2",
            "message_type": "file",
            "content": json.dumps({"file_key": "file_def", "file_name": "broken.bin"}),
        },
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="p2p",
        chat_id="oc_chat_1",
        external_event_id="event-2",
    )

    assert result is None
    assert "withdraw" in events  # 提前建的卡片被撤回
    assert len(unregistered) == 1  # 注册表条目无论成败都清理
    assert len(sent) == 1
    assert "下载失败" in sent[0][2]["text"]


@pytest.mark.asyncio
async def test_feishu_runtime_message_reuses_early_card_without_refiring(monkeypatch) -> None:
    """early_card 传入时复用调用方桥，绝不触发第二次建卡。"""
    from app.services.agent_runtime import card_stream_bridge as bridge_module

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    pregenerated = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4(), name="A",
    )
    user = SimpleNamespace(id=uuid.uuid4(), display_name="Alice")
    session = SimpleNamespace(id=uuid.uuid4())
    model = SimpleNamespace(id=uuid.uuid4())
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _OrderedSession(agent, None)  # 查询1=agent, 查询2=resume 检查(无进行中 run)
    intake = _runtime(tenant_id)
    events: list[str] = []
    registered: list = []

    async def resolve_sender(_db, **_kwargs):
        await asyncio.sleep(0)
        events.append("resolve")
        return user

    async def find_session(**kwargs):
        events.append("session")
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        events.append("enqueue")
        return intake

    def forbid_fire(**_kwargs):
        raise AssertionError("early_card provided — must not fire a second card")

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_fire_early_card", forbid_fire)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu, "_load_agent_and_model", load_model)
    monkeypatch.setattr(feishu, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(bridge_module, "register_bridge", lambda *a: registered.append(a))
    monkeypatch.setattr(bridge_module, "unregister_bridge", lambda *_a: None)

    bridge = _RecordingBridge(events)
    result = await feishu._accept_feishu_runtime_message(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="group",
        chat_id="oc_group_1",
        content="Hello",
        display_content="Hello",
        external_event_id="evt-1",
        pregenerated_run_id=pregenerated,
        early_card=(bridge, lambda *a: registered.append(a), lambda *_a: None),
    )

    assert result is intake
    # 复用桥：没有第二次 card_start；intake 流程照常
    assert events == ["resolve", "session", "enqueue"]
    # run_id 与预生成不一致时按实际 run_id 补注册（幂等重试保护）
    assert len(registered) == 1


class _OrderedSession:
    """Returns the queued values for successive execute() calls, in order."""

    def __init__(self, *values: object) -> None:
        self._values = list(values)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, _statement):
        if not self._values:
            raise AssertionError("unexpected extra query in interrupt flow")
        return _Result(self._values.pop(0))

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_feishu_interrupt_command_cancels_active_run_without_new_run(monkeypatch) -> None:
    from app.services.agent_runtime import adapter as runtime_adapter
    from app.services.agent_runtime import card_stream_bridge as bridge_module
    from app.services.feishu_service import feishu_service as fs

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    active_run_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4(), name="A")
    user = SimpleNamespace(id=user_id, display_name="Alice")
    session = SimpleNamespace(id=session_id)
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _OrderedSession(agent, SimpleNamespace(id=active_run_id))
    cancels: list = []
    sent: list = []
    intake_calls: list = []

    class _FakeIntake:
        def __init__(self, _db) -> None:
            pass

        async def cancel_run(self, command):
            cancels.append(command)
            return SimpleNamespace(run_id=command.run_id)

    async def resolve_sender(_db, **_kwargs):
        return user

    async def find_session(**kwargs):
        return session

    async def send_message(app_id, app_secret, target, msg_type, content, **kwargs):
        sent.append((target, msg_type, content, kwargs))

    async def enqueue(_db, **kwargs):
        intake_calls.append(kwargs)
        raise AssertionError("interrupt must not enqueue a new run")

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(runtime_adapter, "RuntimeCommandIntake", _FakeIntake)
    monkeypatch.setattr(bridge_module, "get_bridge", lambda _key: None)
    monkeypatch.setattr(fs, "send_message", send_message)

    class _NoCard:
        def __init__(self, **kwargs):
            raise AssertionError("interrupt command must not create a card")

    monkeypatch.setattr(bridge_module, "CardStreamBridge", _NoCard)

    result = await feishu._accept_feishu_runtime_message(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="p2p",
        chat_id="",
        content="中断",
        display_content="中断",
        external_event_id=f"evt-{uuid.uuid4()}",
    )

    assert result == "interrupted"
    assert db.commits == 1
    assert len(cancels) == 1
    command = cancels[0]
    assert command.run_id == active_run_id
    assert command.tenant_id == tenant_id
    assert command.actor_user_id == user_id
    assert command.reason == "cancelled_by_user"
    assert command.idempotency_key == f"cancel:feishu:{active_run_id}"
    assert len(intake_calls) == 0
    assert len(sent) == 1
    target, msg_type, content, kwargs = sent[0]
    assert target == "ou_sender"
    assert msg_type == "text"
    assert "已中断" in json.loads(content)["text"]
    assert kwargs["receive_id_type"] == "open_id"


@pytest.mark.asyncio
async def test_feishu_interrupt_with_no_active_run_confirms_and_stays_idle(monkeypatch) -> None:
    from app.services.agent_runtime import adapter as runtime_adapter
    from app.services.feishu_service import feishu_service as fs

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4(), name="A")
    user = SimpleNamespace(id=user_id, display_name="Alice")
    session = SimpleNamespace(id=session_id)
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _OrderedSession(agent, None)
    cancels: list = []
    sent: list = []
    intake_calls: list = []

    class _FakeIntake:
        def __init__(self, _db) -> None:
            pass

        async def cancel_run(self, command):
            cancels.append(command)
            return SimpleNamespace(run_id=command.run_id)

    async def resolve_sender(_db, **_kwargs):
        return user

    async def find_session(**kwargs):
        return session

    async def send_message(app_id, app_secret, target, msg_type, content, **kwargs):
        sent.append((target, content))

    async def enqueue(_db, **kwargs):
        intake_calls.append(kwargs)
        raise AssertionError("interrupt must not enqueue a new run")

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(runtime_adapter, "RuntimeCommandIntake", _FakeIntake)
    monkeypatch.setattr(fs, "send_message", send_message)

    result = await feishu._accept_feishu_runtime_message(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="p2p",
        chat_id="",
        content="停止",
        display_content="停止",
        external_event_id=f"evt-{uuid.uuid4()}",
    )

    assert result == "no_active_run"
    assert len(cancels) == 0
    assert len(intake_calls) == 0
    assert len(sent) == 1
    assert "没有正在执行" in json.loads(sent[0][1])["text"]


@pytest.mark.asyncio
async def test_feishu_card_creation_starts_before_sender_resolution(monkeypatch) -> None:
    """卡片任务在发送者解析/会话创建之前就 fire —— 压缩卡片首现延迟。"""
    from app.services.agent_runtime import card_stream_bridge as bridge_module

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4(), name="A",
    )
    user = SimpleNamespace(id=uuid.uuid4(), display_name="Alice")
    session = SimpleNamespace(id=uuid.uuid4())
    model = SimpleNamespace(id=uuid.uuid4())
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _OrderedSession(agent, None)  # 查询1=agent, 查询2=resume 检查(无进行中 run)
    intake = _runtime(tenant_id)
    events: list[str] = []
    registered: list = []

    async def resolve_sender(_db, **_kwargs):
        await asyncio.sleep(0)  # 模拟真实 Contact API RTT：让出事件循环
        events.append("resolve")
        return user

    async def find_session(**kwargs):
        events.append("session")
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        events.append("enqueue")
        return intake

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu, "_load_agent_and_model", load_model)
    monkeypatch.setattr(feishu, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(
        bridge_module, "CardStreamBridge", partial(_RecordingBridge, events),
    )
    monkeypatch.setattr(bridge_module, "register_bridge", lambda *a: registered.append(a))
    monkeypatch.setattr(bridge_module, "unregister_bridge", lambda *_a: None)

    result = await feishu._accept_feishu_runtime_message(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="group",
        chat_id="oc_group_1",
        content="Hello",
        display_content="Hello",
        external_event_id="evt-1",
    )

    assert result is intake
    # 建卡任务先于发送者解析与会话创建启动；非 resume 不撤回
    assert events == ["card_start", "resolve", "session", "enqueue"]
    assert len(registered) >= 1  # 初始注册 (rebound 注册取决于 fake intake 的 run_id)


@pytest.mark.asyncio
async def test_feishu_card_withdrawn_when_resuming_existing_run(monkeypatch) -> None:
    """消息命中有 lane 的既有 run 时，提前建的卡片被撤回且不参与注册。"""
    from app.services.agent_runtime import card_stream_bridge as bridge_module

    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    active_run_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=tenant_id, creator_id=uuid.uuid4(), name="A",
    )
    user = SimpleNamespace(id=uuid.uuid4(), display_name="Alice")
    session = SimpleNamespace(id=uuid.uuid4())
    model = SimpleNamespace(id=uuid.uuid4())
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _OrderedSession(agent, SimpleNamespace(id=active_run_id))
    intake = _runtime(tenant_id)
    events: list[str] = []
    registered: list = []
    unregistered: list = []

    async def resolve_sender(_db, **_kwargs):
        await asyncio.sleep(0)
        events.append("resolve")
        return user

    async def find_session(**kwargs):
        events.append("session")
        return session

    async def load_model(_db, _agent_id):
        return agent, model, None

    async def enqueue(_db, **kwargs):
        events.append("enqueue")
        return intake

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(channel_session, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(feishu, "_load_agent_and_model", load_model)
    monkeypatch.setattr(feishu, "enqueue_channel_chat_runtime", enqueue)
    monkeypatch.setattr(
        bridge_module, "CardStreamBridge", partial(_RecordingBridge, events),
    )
    monkeypatch.setattr(bridge_module, "register_bridge", lambda *a: registered.append(a))
    monkeypatch.setattr(bridge_module, "unregister_bridge", lambda *a: unregistered.append(a))

    result = await feishu._accept_feishu_runtime_message(
        agent_id=agent_id,
        config=config,  # type: ignore[arg-type]
        sender_open_id="ou_sender",
        sender_user_id="feishu-user-1",
        chat_type="p2p",
        chat_id="",
        content="继续",
        display_content="继续",
        external_event_id="evt-1",
    )

    assert result is intake
    # 提前建的卡片被撤回、从注册表移除；run 仍照常入队（resume）
    assert events == ["card_start", "resolve", "session", "withdraw", "enqueue"]
    assert len(registered) == 1  # 仅初始注册；撤回后不再按 rebound run_id 注册
    assert len(unregistered) == 1


@pytest.mark.asyncio
async def test_feishu_card_withdrawn_on_sender_resolution_failure(monkeypatch) -> None:
    """发送者解析失败时，提前建的卡片被撤回，错误照常传播。"""
    from app.services.agent_runtime import card_stream_bridge as bridge_module
    from app.services.channel_user_service import ChannelUserResolutionError

    agent_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=uuid.uuid4(), creator_id=uuid.uuid4(), name="A",
    )
    config = SimpleNamespace(app_id="app-1", app_secret="secret-1")
    db = _Session(agent)  # 只需要 agent 查询；解析失败发生在会话创建之前
    events: list[str] = []
    unregistered: list = []

    async def resolve_sender(_db, **_kwargs):
        await asyncio.sleep(0)
        raise ChannelUserResolutionError("unresolvable sender")

    monkeypatch.setattr(feishu, "_async_session", lambda: db)
    monkeypatch.setattr(feishu, "_resolve_feishu_sender", resolve_sender)
    monkeypatch.setattr(
        bridge_module, "CardStreamBridge", partial(_RecordingBridge, events),
    )
    monkeypatch.setattr(bridge_module, "register_bridge", lambda *_a: None)
    monkeypatch.setattr(bridge_module, "unregister_bridge", lambda *a: unregistered.append(a))

    with pytest.raises(ChannelUserResolutionError):
        await feishu._accept_feishu_runtime_message(
            agent_id=agent_id,
            config=config,  # type: ignore[arg-type]
            sender_open_id="ou_sender",
            sender_user_id="feishu-user-1",
            chat_type="p2p",
            chat_id="",
            content="Hello",
            display_content="Hello",
            external_event_id="evt-1",
        )

    # 卡片任务已 fire，解析失败时被撤回并从注册表移除
    assert events == ["card_start", "withdraw"]
    assert len(unregistered) == 1
