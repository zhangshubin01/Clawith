"""验证 agent_context 与 Web 会话逻辑严格对齐：无 IDE 特殊化分支。

对齐目标（用户原则 13：IDE 复用 Web 会话逻辑，仅增加 IDE 插件触手）：
1. `_is_ide_session` ContextVar 已彻底从 agent_context 模块代码中移除
2. skills 规则使用 Web 风格的「先 read_file 加载 full instructions」
3. 不再注入 IDE 特殊 prompt（`⚡ IDE 连接模式` / `🪨 IDE 极简回复规则`）

测试不实际调用 build_agent_context（避免触达数据库与文件系统），仅做模块源码静态检查。
"""

from pathlib import Path

_AGENT_CONTEXT_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "agent_context.py"


def _read_source() -> str:
    return _AGENT_CONTEXT_PATH.read_text(encoding="utf-8")


def test_agent_context_no_longer_imports_contextvar():
    """ContextVar 已从模块顶部 import 移除（不再需要）。"""
    src = _read_source()
    assert "from contextvars import ContextVar" not in src, (
        "agent_context.py 仍 import ContextVar，应已与 _is_ide_session 一并移除"
    )


def test_agent_context_no_is_ide_session_definition():
    """_is_ide_session ContextVar 定义已删除。"""
    src = _read_source()
    assert '_is_ide_session: ContextVar[bool] = ContextVar(' not in src, (
        "agent_context.py 仍包含 _is_ide_session ContextVar 定义"
    )


def test_agent_context_no_is_ide_session_read():
    """模块内代码不再读取 _is_ide_session（即不再有 IDE / Web 分支）。

    允许在注释中作为变更说明出现，但不允许出现在可执行代码中。
    """
    src = _read_source()
    code_lines = [line for line in src.splitlines() if not line.lstrip().startswith("#")]
    code_text = "\n".join(code_lines)
    assert "_is_ide_session.get()" not in code_text, (
        "agent_context.py 仍读取 _is_ide_session，IDE 与 Web 仍未对齐"
    )


def test_skills_rules_use_web_style():
    """skills 规则使用 Web 风格：先 read_file 加载 full instructions。"""
    src = _read_source()
    assert "FIRST call `read_file` with the File path above to load the full instructions" in src, (
        "skills 规则未保留 Web 风格的「先 read_file 加载」"
    )
    assert "Skills are reference material — work on the user's project directly first" not in src, (
        "IDE 分支的 skills 规则未被删除"
    )


def test_no_ide_connection_mode_block():
    """`## ⚡ IDE 连接模式` 已删除。"""
    src = _read_source()
    assert "⚡ IDE 连接模式" not in src, "IDE 连接模式 prompt 未被删除"


def test_no_ide_concise_reply_block():
    """`## 🪨 IDE 极简回复规则` 已删除。"""
    src = _read_source()
    assert "🪨 IDE 极简回复规则" not in src, "IDE 极简回复规则 prompt 未被删除"


def test_no_im_channel_gate():
    """`_inject_im_channel_tools` 闸门已删除，IM 通道按 agent 配置注入而非按会话模式。"""
    src = _read_source()
    # 仅允许在注释中作为变更说明出现，不允许作为代码赋值
    code_lines = [line for line in src.splitlines() if not line.strip().startswith("#")]
    code_text = "\n".join(code_lines)
    assert "_inject_im_channel_tools = " not in code_text, (
        "_inject_im_channel_tools 赋值语句未删除"
    )
