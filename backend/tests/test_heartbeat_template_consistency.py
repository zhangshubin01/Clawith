"""HEARTBEAT 模板一致性：单规范源 + reflections 维护指令。"""
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
AGENT_TEMPLATE_HB = BACKEND_DIR / "agent_template" / "HEARTBEAT.md"
APP_TEMPLATE_HB = BACKEND_DIR / "app" / "templates" / "HEARTBEAT.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_agent_template_heartbeat_drives_reflections_and_keeps_idle_guard():
    content = _read(AGENT_TEMPLATE_HB)

    # reflections 维护指令（读旧反思 / 写发现 / next cycle seed）
    assert "memory/reflections.md" in content
    assert "next cycle seed" in content
    # 好奇日志指令保留
    assert "memory/curiosity_journal.md" in content
    # 防空转护栏保留（无兴趣点直接收尾）
    assert "HEARTBEAT_OK" in content
    # 探索预算护栏保留
    assert "5 searches" in content


def test_both_heartbeat_templates_are_identical():
    """app/templates 与 agent_template 必须一致，杜绝再分叉。"""
    assert AGENT_TEMPLATE_HB.read_bytes() == APP_TEMPLATE_HB.read_bytes()
