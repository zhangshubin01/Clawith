"""L3 path-grounding 存储侧 basename 建议测试（ADR-0013 改动一）。

覆盖：事故 run 6a1c0eab 回归（mydome1→calculator）、零命中无建议行、
多命中最近优先且上限 3、隐藏条目跳过、深度/节点双上限、目录型 miss、
空 rel_path、企业路径限定与跨根隔离、pattern base 排除 storage 建议、
storage 异常窄化降级、_read_file_outcome 端到端。
"""

import uuid

import pytest

from app.services import agent_tools
from app.services.storage_runtime.base import StorageBackend, StorageEntry


class MemoryStorageBackend(StorageBackend):
    """自包含内存后端（与 test_agent_tools_storage_workspace.py 同构，避免跨测试文件导入）。"""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.files)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries: dict[str, StorageEntry] = {}
        for existing, data in self.files.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries.values(), key=lambda entry: (not entry.is_dir, entry.name))

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]


def _install(monkeypatch, files: dict[str, bytes]) -> MemoryStorageBackend:
    backend = MemoryStorageBackend(files)
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: backend)
    return backend


@pytest.mark.asyncio
async def test_accident_regression_suggests_real_package(monkeypatch):
    """事故回归：猜 mydome1（实际 calculator）→ 建议真实路径。"""
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {f"{agent_id}/workspace/com/example/calculator/Calculator.kt": b""},
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/com/example/mydome1/Calculator.kt",
        tenant_id=None,
    )

    assert "verified in workspace storage" in text
    assert "'workspace/com/example/calculator/Calculator.kt'" in text


@pytest.mark.asyncio
async def test_zero_match_has_no_verified_line(monkeypatch):
    agent_id = uuid.uuid4()
    _install(monkeypatch, {f"{agent_id}/workspace/notes.md": b""})

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/Calculator.kt",
        tenant_id=None,
    )

    assert "Not found:" in text  # L2 诊断仍在
    assert "verified in workspace storage" not in text


@pytest.mark.asyncio
async def test_multiple_matches_nearest_first_and_capped(monkeypatch):
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {
            f"{agent_id}/workspace/a/Calculator.kt": b"",
            f"{agent_id}/workspace/a/b/Calculator.kt": b"",
            f"{agent_id}/workspace/c/Calculator.kt": b"",
            f"{agent_id}/workspace/d/Calculator.kt": b"",
        },
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/missing/Calculator.kt",
        tenant_id=None,
    )

    assert "'workspace/a/Calculator.kt'" in text
    assert "'workspace/c/Calculator.kt'" in text
    assert "'workspace/d/Calculator.kt'" in text
    # 深度 3 的命中排第 4，被 max_suggestions=3 截断
    assert "'workspace/a/b/Calculator.kt'" not in text


@pytest.mark.asyncio
async def test_hidden_entries_are_skipped(monkeypatch):
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {f"{agent_id}/workspace/.hidden/Calculator.kt": b""},
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/x/Calculator.kt",
        tenant_id=None,
    )

    assert "verified in workspace storage" not in text


@pytest.mark.asyncio
async def test_depth_limit_includes_boundary_and_excludes_deeper(monkeypatch):
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {
            # 相对祖先 depth=6：命中（等于 max_depth）
            f"{agent_id}/workspace/d1/d2/d3/d4/d5/Calculator.kt": b"",
            # depth=7：超出 max_depth=6，不返回
            f"{agent_id}/workspace/d1/d2/d3/d4/d5/d6/Calculator.kt": b"",
        },
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/x/Calculator.kt",
        tenant_id=None,
    )

    assert "'workspace/d1/d2/d3/d4/d5/Calculator.kt'" in text
    assert "'workspace/d1/d2/d3/d4/d5/d6/Calculator.kt'" not in text


@pytest.mark.asyncio
async def test_node_budget_caps_scan(monkeypatch):
    """160 个同级条目：排序后第 6 个命中（预算内），第 160 个（预算外）不返回。"""
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {f"{agent_id}/workspace/a{i:03d}": b"" for i in range(160)},
    )

    hit = await agent_tools._path_failure_details(
        agent_id,
        "workspace/x/a005",
        tenant_id=None,
    )
    assert "'workspace/a005'" in hit

    miss = await agent_tools._path_failure_details(
        agent_id,
        "workspace/x/a159",
        tenant_id=None,
    )
    assert "verified in workspace storage" not in miss


@pytest.mark.asyncio
async def test_directory_type_miss_suggests_matching_directory(monkeypatch):
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {f"{agent_id}/workspace/app/module-x/build.gradle": b""},
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/module-x",
        tenant_id=None,
    )

    assert "'workspace/app/module-x'" in text


@pytest.mark.asyncio
async def test_empty_rel_path_returns_empty(monkeypatch):
    agent_id = uuid.uuid4()
    _install(monkeypatch, {f"{agent_id}/workspace/notes.md": b""})

    assert await agent_tools._path_failure_details(agent_id, "", tenant_id=None) == ""
    assert await agent_tools._storage_nearest_candidates(agent_id, "", tenant_id=None) == []


@pytest.mark.asyncio
async def test_enterprise_suggestions_stay_within_enterprise_root(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = "tenant-a"
    _install(
        monkeypatch,
        {
            f"enterprise_info_{tenant_id}/docs/guide.md": b"",
            f"{agent_id}/workspace/guide.md": b"",
        },
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "enterprise_info/xx/guide.md",
        tenant_id=tenant_id,
    )

    assert "'enterprise_info/docs/guide.md'" in text
    assert "'workspace/guide.md'" not in text  # 不跨根


@pytest.mark.asyncio
async def test_workspace_suggestions_do_not_cross_into_enterprise(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = "tenant-a"
    _install(
        monkeypatch,
        {
            f"enterprise_info_{tenant_id}/docs/guide.md": b"",
            f"{agent_id}/workspace/guide.md": b"",
        },
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/xx/guide.md",
        tenant_id=tenant_id,
    )

    assert "'workspace/guide.md'" in text
    assert "enterprise_info" not in text


@pytest.mark.asyncio
async def test_pattern_base_excludes_storage_suggestions(monkeypatch):
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {f"{agent_id}/workspace/Calculator.kt": b""},
    )

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/x/Calculator.kt",
        label="pattern base",
        include_storage=False,
    )

    assert "Not found:" in text
    assert "verified in workspace storage" not in text


@pytest.mark.asyncio
async def test_storage_exceptions_degrade_without_raising(monkeypatch):
    """storage is_dir 抛错 → 窄化降级：无建议行、不抛异常、L2 诊断保留。"""
    agent_id = uuid.uuid4()
    backend = _install(monkeypatch, {f"{agent_id}/workspace/notes.md": b""})

    async def boom(_key: str) -> bool:
        raise RuntimeError("s3 down")

    backend.is_dir = boom  # type: ignore[method-assign]

    text = await agent_tools._path_failure_details(
        agent_id,
        "workspace/x/Calculator.kt",
        tenant_id=None,
    )

    assert "Not found:" in text
    assert "verified in workspace storage" not in text


@pytest.mark.asyncio
async def test_local_fs_diagnosis_and_storage_suggestion_coexist(monkeypatch, tmp_path):
    """L2 本地 FS 诊断与 L3 storage 建议并存（方案测试清单 #8）。"""
    agent_id = uuid.uuid4()
    workspace_root = tmp_path / str(agent_id)
    # FS 上存在 workspace/com/example/mydome1/Calculator.kt → L2 前缀修正建议真实
    fs_target = workspace_root / "workspace" / "com" / "example" / "mydome1" / "Calculator.kt"
    fs_target.parent.mkdir(parents=True)
    fs_target.write_text("")
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _aid: workspace_root)
    _install(
        monkeypatch,
        {
            f"{agent_id}/workspace/com/example/calculator/Calculator.kt": b"",
        },
    )

    # guess 相对 root 缺 workspace/ 前缀 → L2 建议补前缀，L3 建议真实包路径，二者共存
    text = await agent_tools._path_failure_details(
        agent_id,
        "com/example/mydome1/Calculator.kt",
        tenant_id=None,
    )

    assert "Did you mean: 'workspace/com/example/mydome1/Calculator.kt'" in text  # L2
    assert "verified in workspace storage" in text  # L3
    assert "'workspace/com/example/calculator/Calculator.kt'" in text


@pytest.mark.asyncio
async def test_read_file_outcome_carries_verified_suggestion(monkeypatch):
    """端到端：错误码不变（workspace_file_not_found），消息含 verified 建议。"""
    agent_id = uuid.uuid4()
    _install(
        monkeypatch,
        {
            f"{agent_id}/workspace/com/example/calculator/Calculator.kt": b"package com.example.calculator\n",
        },
    )

    outcome = await agent_tools._read_file_outcome(
        agent_id,
        {"path": "workspace/com/example/mydome1/Calculator.kt"},
        tenant_id=None,
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "workspace_file_not_found"
    summary = outcome.result_summary or ""
    assert "verified in workspace storage" in summary
    assert "workspace/com/example/calculator/Calculator.kt" in summary
