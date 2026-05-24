"""验证 LSP4J IDE 原生文件工具的描述包含 Agent 私有路径用法说明。

背景：`_should_route_to_ide` 与 `_is_agent_internal_path` 已经能正确把 Agent 私有路径
（memory.md、skills/、workspace/ 等）路由到后端兜底执行，不会被误写到 IDE 项目里。
但 LLM 看到的工具描述若未声明这一能力，就不会主动用 IDE 工具去更新 memory.md 或维护
skills/。这组测试守住描述的关键字契约。
"""

from app.plugins.clawith_lsp4j.tool_hooks import _LSP4J_IDE_TOOLS


def _find_tool(name: str) -> dict:
    """_LSP4J_IDE_TOOLS 是 OpenAI function-calling 结构：{type, function: {name, description, ...}}。"""
    for tool in _LSP4J_IDE_TOOLS:
        fn = tool.get("function") or {}
        if fn.get("name") == name:
            return fn
    raise AssertionError(f"tool {name} 未在 _LSP4J_IDE_TOOLS 中定义")


def test_write_file_mentions_agent_private_prefixes():
    """write_file 描述明确 Agent 私有路径前缀（memory/skills/enterprise_info）。

    workspace/ 不在 Agent 私有路径白名单，路由到 IDE 项目根目录。
    """
    desc = _find_tool("write_file")["description"]
    assert "绝对路径" in desc
    assert "memory/" in desc
    assert "skills/" in desc
    assert "enterprise_info/" in desc
    # workspace/ 必须出现在描述里、但要明确路由到 IDE 项目而非 Agent 内部
    assert "workspace/" in desc, "需要描述 workspace/ 的真实路由行为"
    assert "IDE 项目" in desc


def test_edit_file_mentions_agent_files():
    """edit_file 描述明确 Agent 私有路径前缀及根级文件名白名单。"""
    desc = _find_tool("edit_file")["description"]
    assert "绝对路径" in desc
    assert "memory/" in desc
    assert "skills/" in desc
    assert "soul.md" in desc


def test_delete_file_mentions_protected_files():
    """delete_file 描述明确禁止删除受保护文件。"""
    desc = _find_tool("delete_file")["description"]
    assert "禁止删除" in desc
    assert "soul.md" in desc
    assert "tasks.json" in desc
