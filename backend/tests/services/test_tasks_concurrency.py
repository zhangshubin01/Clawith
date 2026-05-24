"""tasks.json 并发写入测试（P1-1 修复验证）

验证 _handle_add_tasks / _handle_todo_write 在并发调用下的数据完整性：
- per-agent asyncio.Lock 串行化读-合并-写
- 原子写（先 .tmp 再 os.replace）避免中断损坏
- add_tasks 与 todo_write 共享同一把锁

历史背景：评审发现 LLM 同会话并发调用 add_tasks 会触发 lost-update
（读取相同 existing → 各自合并 → 互相覆盖写回），50 并发会丢失约 50% 任务。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import uuid

import pytest

from app.services import agent_tools as at
from app.services.agent_tools import _handle_add_tasks, _handle_todo_write


@pytest.fixture
def tmp_workspace(monkeypatch):
    """临时工作空间 + mock ensure_workspace / _get_agent_tenant_id，避免 DB 依赖。"""
    tmpdir = tempfile.mkdtemp()
    _ws_root = pathlib.Path(tmpdir) / "ws"
    _ws_root.mkdir(parents=True, exist_ok=True)

    async def fake_tenant(agent_id):
        return uuid.UUID("00000000-0000-0000-0000-000000000002")

    monkeypatch.setattr(at, "_agent_workspace_root", lambda aid: _ws_root)
    monkeypatch.setattr(at, "_get_agent_tenant_id", fake_tenant)
    return _ws_root


class TestTasksConcurrency:
    """tasks.json 并发安全测试（P1-1）"""

    async def test_add_tasks_no_lost_update_under_50_concurrency(self, tmp_workspace):
        """50 个并发 add_tasks 应全部落盘，零丢失。"""
        aid = uuid.UUID("00000000-0000-0000-0000-000000000099")
        coros = [
            _handle_add_tasks(aid, {"tasks": [{"id": f"t{i:03d}", "content": f"task {i}"}]})
            for i in range(50)
        ]
        await asyncio.gather(*coros)
        final = json.loads((tmp_workspace / "tasks.json").read_text(encoding="utf-8"))
        assert len(final) == 50, f"lost-update: 实际 {len(final)} / 预期 50"
        # ID 完整性
        assert sorted(t["id"] for t in final) == [f"t{i:03d}" for i in range(50)]

    async def test_add_tasks_dedup_same_id(self, tmp_workspace):
        """重复 id 的任务后写覆盖前写，最终列表无重复。"""
        aid = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        # 顺序写入相同 id 的任务
        await _handle_add_tasks(aid, {"tasks": [{"id": "x1", "content": "old"}]})
        await _handle_add_tasks(aid, {"tasks": [{"id": "x1", "content": "new"}]})
        final = json.loads((tmp_workspace / "tasks.json").read_text(encoding="utf-8"))
        assert len(final) == 1
        assert final[0]["content"] == "new"

    async def test_todo_write_overwrite_semantics(self, tmp_workspace):
        """todo_write 覆盖语义：清空已有任务，写入新列表。"""
        aid = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
        await _handle_add_tasks(aid, {"tasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
        await _handle_todo_write(aid, {"tasks": [{"content": "only one"}]})
        final = json.loads((tmp_workspace / "tasks.json").read_text(encoding="utf-8"))
        assert len(final) == 1
        assert final[0]["content"] == "only one"

    async def test_mixed_add_and_overwrite_concurrent_no_corruption(self, tmp_workspace):
        """add_tasks 与 todo_write 混合并发：结果可能各异，但 JSON 必须完整。"""
        aid = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
        mixed = [_handle_add_tasks(aid, {"tasks": [{"id": f"y{i}"}]}) for i in range(20)]
        mixed.append(_handle_todo_write(aid, {"tasks": [{"content": "reset"}]}))
        await asyncio.gather(*mixed)
        # 仅断言 JSON 解析成功 + 是 list（不能保证最终条数，因为执行顺序不定）
        data = json.loads((tmp_workspace / "tasks.json").read_text(encoding="utf-8"))
        assert isinstance(data, list)
        # 每条都符合 schema（有 content 或 id）
        for entry in data:
            assert isinstance(entry, dict)
            assert "content" in entry and "status" in entry

    async def test_cross_agent_locks_independent(self, tmp_workspace, monkeypatch):
        """不同 agent 的锁互不阻塞（per-agent 分片）。"""
        agents = [uuid.UUID(f"00000000-0000-0000-0000-{i:012d}") for i in range(1, 4)]

        # 每个 agent 用独立 workspace 子目录，避免共用 tasks.json
        monkeypatch.setattr(at, "_agent_workspace_root", lambda aid: tmp_workspace / str(aid))

        # 三 agent 并发写入，互不干扰
        await asyncio.gather(*(
            _handle_add_tasks(a, {"tasks": [{"id": "t1", "content": f"agent {a}"}]})
            for a in agents
        ))
        for a in agents:
            data = json.loads((tmp_workspace / str(a) / "tasks.json").read_text(encoding="utf-8"))
            assert len(data) == 1
            assert str(a) in data[0]["content"]
