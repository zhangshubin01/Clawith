"""HEARTBEAT 模板一致性：单规范源 + reflections 维护指令。"""
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
AGENT_TEMPLATE_HB = BACKEND_DIR / "agent_template" / "HEARTBEAT.md"
APP_TEMPLATE_HB = BACKEND_DIR / "app" / "templates" / "HEARTBEAT.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_agent_template_heartbeat_drives_reflections_and_keeps_idle_guard():
    content = _read(AGENT_TEMPLATE_HB)

    # reflections 维护指令（读旧反思 / 写发现 / Next Cycle Seeds）
    assert "memory/reflections.md" in content
    assert "Next Cycle Seeds" in content
    # 好奇日志指令保留
    assert "memory/curiosity_journal.md" in content
    # 防空转护栏保留（无兴趣点直接收尾）
    assert "HEARTBEAT_OK" in content
    # 探索预算护栏保留
    assert "5 searches" in content


def test_agent_template_heartbeat_converges_curiosity_followups_into_seeds():
    content = _read(AGENT_TEMPLATE_HB)

    # Phase 2 定位句：curiosity 是纯探索日志，durable findings 归 Phase 3。
    assert "raw exploration log" in content
    # Phase 3 收敛步：读 Follow-up 与 Active Questions，promote ≤3 进 Next Cycle Seeds。
    assert "Read the **Follow-up** entries" in content
    assert "Active Questions" in content
    assert "at most 3" in content
    # 原条目标记 promoted 不删除；优先未 promote 条目防重复收敛。
    assert "→promoted YYYY-MM-DD" in content
    assert "do not delete journal entries" in content
    assert "preferring entries not yet marked" in content


def test_both_heartbeat_templates_are_identical():
    """app/templates 与 agent_template 必须一致，杜绝再分叉。"""
    assert AGENT_TEMPLATE_HB.read_bytes() == APP_TEMPLATE_HB.read_bytes()


def test_heartbeat_convergence_is_unconditional_even_when_idle():
    """收敛步必须无条件执行：08-28 生产观察——模型把收敛步读成
    "Otherwise, update reflections" 的条件分支，无新内容时整步跳过，
    存量 follow-up/Active Questions 永不 promote。"""
    content = _read(AGENT_TEMPLATE_HB)

    converge_pos = content.index("Converge your exploration log")
    # 明确无条件标记
    assert "always" in content[converge_pos : converge_pos + 400]
    # HEARTBEAT_OK 必须在收敛步之后——禁止早退跳过收敛
    assert content.index("HEARTBEAT_OK") > converge_pos
    # 无条目值得 promote 时的显式处置路径（不默认丢弃，也不静默跳过）
    assert "If no journal entry is worth promoting" in content
