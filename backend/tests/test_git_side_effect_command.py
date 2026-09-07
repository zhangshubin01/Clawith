"""方向 5 git 探索命令副作用告警（执行时确定性识别）测试。

Plan: docs/technical-plans/20260907-git-side-effect-command-warning.md.
Self-contained: backend/tests is not a package, so this file keeps its own
fixtures (mirroring test_workspace_flush_revert_guard.py) and monkeypatches the
sandbox registry directly (the registry is imported lazily inside
``_execute_code_outcome``, so patching the module attribute before the call
takes effect).
"""

import uuid

import pytest

from app.services import agent_tools
from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.security import detect_git_side_effect_commands


# ── 纯函数 detect_git_side_effect_commands ────────────────────────


def test_detects_side_effect_subcommands_deduped_and_sorted():
    code = "\n".join(
        [
            "git status",
            "git checkout main",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git restore src/a.kt",
            "git checkout main",  # duplicate → dedup
        ]
    )
    assert detect_git_side_effect_commands("bash", code) == [
        "checkout",
        "clean",
        "reset",
        "restore",
    ]


def test_ignores_read_only_git_commands():
    code = "\n".join(
        ["git status --short", "git log --oneline", "git branch -a", "git diff HEAD"]
    )
    assert detect_git_side_effect_commands("bash", code) == []


def test_detects_switch_and_is_case_insensitive():
    assert detect_git_side_effect_commands("bash", "Git Checkout foo\ngit SWITCH bar") == [
        "checkout",
        "switch",
    ]


def test_does_not_scan_non_bash():
    # Python/Node subprocess arg-lists are out of scope by design (plan §4.1).
    assert (
        detect_git_side_effect_commands(
            "python", "subprocess.run(['git', 'checkout', 'x'])"
        )
        == []
    )
    assert detect_git_side_effect_commands("node", "exec('git checkout x')") == []


# ── _execute_code_outcome 集成（告警 + metadata + summary，不改执行结果） ──


class _FakeBackend:
    name = "subprocess"

    async def execute(self, code, language, timeout=30, work_dir=None, **kwargs):
        return ExecutionResult(
            success=True, stdout="", stderr="", exit_code=0, duration_ms=1, error=None
        )

    def _format_result(self, result):
        return "✅ Code executed successfully (no output)"


@pytest.fixture(autouse=True)
def _stub_sandbox_registry(monkeypatch):
    async def _noop_tool_config(agent_id, tool_name):
        return None

    monkeypatch.setattr(agent_tools, "_get_tool_config", _noop_tool_config)
    from app.services.sandbox import registry

    monkeypatch.setattr(registry, "get_sandbox_backend", lambda config: _FakeBackend())


async def _execute(tmp_path, code: str):
    outcome = await agent_tools._execute_code_outcome(
        uuid.uuid4(),
        tmp_path,
        {"language": "bash", "code": code},
        tool_name="execute_code",
        sandbox_config=SandboxConfig(type="subprocess"),
    )
    return outcome


@pytest.mark.asyncio
async def test_execute_code_warns_and_records_side_effect_commands(tmp_path):
    captured: list[str] = []

    def _sink(message):
        captured.append(str(message))

    sink_id = agent_tools.logger.add(_sink, level="WARNING")
    try:
        outcome = await _execute(
            tmp_path,
            "git checkout main\ngit reset --hard HEAD~1",
        )
    finally:
        agent_tools.logger.remove(sink_id)

    # 告警记账：loguru warning 出现一次，携带 run_id/agent_id/subcommands。
    hits = [m for m in captured if "[GitSideEffectCommand]" in m]
    assert len(hits) == 1
    assert "subcommands=checkout,reset" in hits[0]
    # 记账存活：metadata 落 subcommands（仅子命令名，不含 branch/path）。
    assert outcome.metadata["git_side_effect_commands"] == ["checkout", "reset"]
    # 事实性提示进 summary。
    assert "[git 副作用提示]" in outcome.result_summary
    assert "git checkout, reset" in outcome.result_summary
    # 零流程控制变更：仍是成功。
    assert outcome.status == "succeeded"


@pytest.mark.asyncio
async def test_execute_code_silent_for_read_only_git(tmp_path):
    captured: list[str] = []

    def _sink(message):
        captured.append(str(message))

    sink_id = agent_tools.logger.add(_sink, level="WARNING")
    try:
        outcome = await _execute(tmp_path, "git status\ngit log --oneline")
    finally:
        agent_tools.logger.remove(sink_id)

    assert not any("[GitSideEffectCommand]" in m for m in captured)
    assert "git_side_effect_commands" not in outcome.metadata
    assert "[git 副作用提示]" not in outcome.result_summary
    assert outcome.status == "succeeded"
