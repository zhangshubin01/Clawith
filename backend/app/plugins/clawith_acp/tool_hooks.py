"""ACP 工具桥钩子 — IDE 工具路由到 ACP WebSocket。

链式 Hook 注册：
- 注册到 _bridge_handlers 列表，由 _chained_execute_tool 统一分发。
- 任一 handler 返回非 None 即短路；全部返回 None 时回落 base handler。

通过 ContextVar(current_acp_handler) 在 ACP 会话中将工具路由到 IDE 插件执行。

三层钩子：
1. 执行路径：_acp_aware_execute_tool — 文件/终端工具走 ACP WebSocket
2. 注册路径：_acp_aware_get_tools — LLM 看到 ACP 代理工具定义
3. caller 模块修补 — 防止 from import 本地引用绕过钩子
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from loguru import logger
from app.plugins.clawith_acp.acp_routes import ACP_OVERLAP_BASE_TOOL_NAMES, ACP_TOOL_MAP

from app.services import agent_tools
from .tool_bridge import current_acp_handler

# ──────────────────────────────────────────────
# 链式 Handler 注册
# ──────────────────────────────────────────────
# _bridge_handlers 存放 ACP 工具路由 handler。
# _chained_execute_tool 遍历调用，首个返回非 None 的 handler 结果即工具终态。
# 所有 handler 都返回 None 时，回落 base handler。
_bridge_handlers: list[Callable[..., Any]] = []
_base_execute_tool_snapshot: Callable[..., Any] | None = None


async def _chained_execute_tool(
    tool_name: str,
    args: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str = "",
    on_output=None,
) -> str:
    """链式工具执行分发器。

    遍历 _bridge_handlers，任一 handler 返回非 None 即短路返回。
    所有 handler 都返回 None 时，回退到被捕获的 base handler。
    """
    for handler in _bridge_handlers:
        try:
            result = await handler(
                tool_name, args, agent_id, user_id, session_id=session_id, on_output=on_output
            )
            if result is not None:
                return result
        except Exception:
            logger.exception("[ACP] handler {} 抛出异常，继续下一个 handler", handler.__name__)

    # 所有 bridge handler 未处理，回退基础入口
    base = _base_execute_tool_snapshot
    if base is not None:
        return await base(tool_name, args, agent_id, user_id, session_id=session_id, on_output=on_output)
    # 回退到 agent_tools 模块属性（兜底）
    return await agent_tools.execute_tool(
        tool_name, args, agent_id, user_id, session_id=session_id, on_output=on_output
    )

# ──────────────────────────────────────────────
# ACP 代理工具定义（替换基础工具中同名者）
# ──────────────────────────────────────────────

_ACP_IDE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "acp_read_file",
            "description": (
                "读取 IDE 项目文件内容。\n"
                "路径: 绝对路径（如 /Users/xxx/project/src/Main.kt）\n"
                "      相对路径（如 src/Main.kt）相对于项目根目录。\n"
                "支持 line/limit 参数分页读取。\n"
                "⚠️ Agent 自身文件（memory/、skills/ 前缀、soul.md/focus.md）不使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对项目根）"},
                    "line": {"type": "integer", "description": "起始行（1-indexed）"},
                    "limit": {"type": "integer", "description": "最大行数（默认 2000）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_write_file",
            "description": (
                "创建或覆盖 IDE 项目文件。\n"
                "局部修改优先用 edit_file（参数完全相同）。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。\n"
                "⚠️ Agent 自身文件（memory/、skills/ 前缀）不使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_edit_file",
            "description": (
                "编辑 IDE 项目文件（局部替换）。\n"
                "对文件做局部文本替换: 用 old_string 定位文本 → 替换为 new_string。\n"
                "多个匹配时需设置 replace_all=true 或提供唯一 old_string。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。\n"
                "⚠️ 不是全量写入，参数不是 content 而是 old_string+new_string。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "old_string": {"type": "string", "description": "要替换的原文（精确匹配，含空白和换行）"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {"type": "boolean", "description": "是否替换所有匹配（默认 false，仅替换第一处）"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_delete_file",
            "description": (
                "删除 IDE 项目文件。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_list_files",
            "description": (
                "列出 IDE 项目目录内容（仅目录结构，不含文件内容）。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。\n"
                "默认列出项目根目录，支持 depth 控制递归深度、limit 控制最大条目数。\n"
                "⚠️ 禁止使用 .. 路径穿越。\n"
                "⚠️ depth 上限 3，limit 上限 200。\n"
                "⚠️ Agent 自身文件（memory/、skills/ 前缀）不支持此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径（绝对或相对项目根，默认项目根）"},
                    "depth": {"type": "integer", "description": "递归深度（默认 1，上限 3）"},
                    "limit": {"type": "integer", "description": "最大返回条目数（默认 50，上限 200）"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_execute_command",
            "description": (
                "在 IDE 项目根目录执行终端命令。\n"
                "✅ 构建: ./gradlew build / assembleDebug\n"
                "✅ 测试: ./gradlew test\n"
                "✅ Git: git log / diff / status / show\n"
                "✅ 搜索: grep -rl PATTERN src/  # 只搜文件名, 减少输出\n"
                "❌ 禁止 cat/head/tail/less → 请用 read_file\n"
                "❌ 禁止 find/ls 逐目录探索 → 用 grep -rl 一次性定位\n"
                "⚡ 同一轮内可并行调用多个命令, 但不要重复跑同一个 gradlew 编译"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "requires_approval": {"type": "boolean", "description": "是否需要用户审批（默认 false）"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_find_file",
            "description": (
                "在 IDE 项目中按文件名搜索文件。\n"
                "使用 IDE 文件索引，速度极快。\n"
                "支持 camelCase 匹配（USJ → UserService.java）、子串匹配、通配符匹配。\n"
                "替代 grep -rl 或 find 命令进行文件查找。\n"
                "scope: project_files(默认)/project_and_libraries/project_production_files/project_test_files"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "文件名搜索模式（必填，支持子串/camelCase/通配符）"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "description": "搜索范围（默认 project_files）"
                    },
                    "pageSize": {"type": "integer", "description": "每页结果数（默认 25，最大 500）"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "acp_search_text",
            "description": (
                "在 IDE 项目文件中搜索文本内容。\n"
                "精确搜索使用 IDE 词索引（O(1) 查找），正则搜索使用 Find in Files。\n"
                "替代 grep 命令进行代码内文本搜索。\n"
                "支持上下文过滤（code/comments/strings）和文件掩码过滤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索内容（必填，精确匹配或正则表达式）"},
                    "regex": {"type": "boolean", "description": "是否作为正则表达式（默认 false）"},
                    "context": {
                        "type": "string",
                        "enum": ["all", "code", "comments", "strings"],
                        "description": "搜索上下文过滤：all(默认)/code/comments/strings"
                    },
                    "caseSensitive": {"type": "boolean", "description": "是否大小写敏感（默认 true）"},
                    "filePattern": {"type": "string", "description": "文件掩码过滤，如 *.kt、*.java,!*Test.java"},
                    "pageSize": {"type": "integer", "description": "每页结果数（默认 100，最大 500）"},
                },
                "required": ["query"],
            },
        },
    },
    # ── find_class: 类搜索 (IDE 索引) ──
    {
        "type": "function",
        "function": {
            "name": "acp_find_class",
            "description": (
                "按类名搜索项目中的类/接口/枚举。基于 IntelliJ 类索引 (Ctrl+N 同款)，"
                "支持 camelCase 匹配 (USvc -> UserService)、substring 匹配 (Service -> UserService)、"
                "通配符匹配 (User*Impl -> UserServiceImpl)。\n"
                "参数: query(必填), scope(project_files/project_and_libraries/project_production_files/project_test_files), "
                "language(按语言过滤), matchMode(substring/prefix/exact), pageSize(默认25, 最大500), cursor(分页游标)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索模式。支持 camelCase/substring/wildcard 匹配"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "default": "project_files"
                    },
                    "language": {"type": "string", "description": "按语言过滤结果（如 'Java', 'Kotlin', 'Python'），大小写不敏感"},
                    "matchMode": {"type": "string", "enum": ["substring", "prefix", "exact"], "default": "substring"},
                    "pageSize": {"type": "integer", "default": 25, "maximum": 500},
                    "cursor": {"type": "string", "description": "分页游标，从上一次响应获取"}
                },
                "required": ["query"]
            }
        }
    },
    # ── find_symbol: 符号搜索 (IDE 索引) ──
    {
        "type": "function",
        "function": {
            "name": "acp_find_symbol",
            "description": (
                "🚀 IDE 索引符号搜索 — 比 grep -r 快 100-1000 倍，支持语义匹配（多态继承、接口实现）。\n"
                "用途: 按名称查找类/方法/字段/函数。基于 IntelliJ 符号索引 (Ctrl+Alt+Shift+N 同款)，\n"
                "能搜到 grep 找不到的 Kotlin data class/Java 接口实现/继承链中的方法。\n"
                "参数: query(必填), scope, language(按语言过滤), pageSize(默认25, 最大500), cursor"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "符号名搜索模式"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "default": "project_files"
                    },
                    "language": {"type": "string", "description": "按语言过滤"},
                    "pageSize": {"type": "integer", "default": 25, "maximum": 500},
                    "cursor": {"type": "string", "description": "分页游标"}
                },
                "required": ["query"]
            }
        }
    },
    # ── index_status: IDE 索引状态查询 ──
    {
        "type": "function",
        "function": {
            "name": "acp_index_status",
            "description": "查询 IDE 索引状态。返回 Dumb Mode 状态，帮助 LLM 判断代码智能操作（搜索、导航、重构）是否可用。索引构建期间返回 isDumbMode=true。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    # ── find_references: 查找符号所有引用 (IDE 索引) ──
    {
        "type": "function",
        "function": {
            "name": "acp_find_references",
            "description": (
                "查找符号在项目中的所有引用。基于 IntelliJ Find Usages (Alt+F7)。\n"
                "支持 file+line+column 定位或 language+symbol FQN 引用。\n"
                "参数: file(可选), line(可选), column(可选), language(可选), symbol(可选), "
                "scope, includeGenerated(默认true), pageSize(默认100, 最大500), cursor(分页游标)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "源文件路径（相对或绝对路径）"},
                    "line": {"type": "integer", "description": "1-based 行号，与 file+column 共同定位符号"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "language": {"type": "string", "description": "编程语言 (Java/Kotlin/Python 等)，与 symbol 配合使用"},
                    "symbol": {"type": "string", "description": "FQN 符号引用，如 com.example.MyClass#method(String)"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "default": "project_files"
                    },
                    "includeGenerated": {"type": "boolean", "default": True, "description": "是否包含生成代码中的引用"},
                    "pageSize": {"type": "integer", "default": 100, "maximum": 500},
                    "cursor": {"type": "string", "description": "分页游标，从上一次响应获取"}
                }
            }
        }
    },
    # ── find_definition: 导航到符号声明 ──
    {
        "type": "function",
        "function": {
            "name": "acp_find_definition",
            "description": (
                "导航到符号的定义位置（Go to Definition）。\n"
                "支持 file+line+column 定位或 language+symbol FQN 引用。\n"
                "返回文件路径、行号/列号、代码预览和符号名称。\n"
                "参数: file(可选), line(可选), column(可选), language(可选), symbol(可选)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "源文件路径"},
                    "line": {"type": "integer", "description": "1-based 行号"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "language": {"type": "string", "description": "编程语言 (Java/Kotlin/Python 等)"},
                    "symbol": {"type": "string", "description": "FQN 符号引用，如 com.example.MyClass#method(String)"}
                }
            }
        }
    },
    # ── find_implementations: 查找接口/抽象方法的所有实现 ──
    {
        "type": "function",
        "function": {
            "name": "acp_find_implementations",
            "description": (
                "查找接口、抽象类或抽象方法的所有具体实现。\n"
                "支持 file+line+column 定位或 language+symbol FQN 引用。\n"
                "参数: file(可选), line(可选), column(可选), language(可选), symbol(可选), "
                "scope, pageSize(默认100, 最大500), cursor(分页游标)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "源文件路径"},
                    "line": {"type": "integer", "description": "1-based 行号"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "language": {"type": "string", "description": "编程语言 (Java/Kotlin/Python 等)"},
                    "symbol": {"type": "string", "description": "FQN 符号引用"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "default": "project_files"
                    },
                    "pageSize": {"type": "integer", "default": 100, "maximum": 500},
                    "cursor": {"type": "string", "description": "分页游标"}
                }
            }
        }
    },
    # ── find_super_methods: 查找方法重写链 ──
    {
        "type": "function",
        "function": {
            "name": "acp_find_super_methods",
            "description": (
                "查找方法在继承链中的父类/接口声明。\n"
                "从子类实现追溯到接口或父类声明。\n"
                "支持 file+line+column 定位或 language+symbol FQN 引用。\n"
                "参数: file(可选), line(可选), column(可选), language(可选), symbol(可选)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "源文件路径"},
                    "line": {"type": "integer", "description": "1-based 行号"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "language": {"type": "string", "description": "编程语言 (Java/Kotlin/Python 等)"},
                    "symbol": {"type": "string", "description": "FQN 符号引用"}
                }
            }
        }
    },
    # ── call_hierarchy: 分析调用层次 ──
    {
        "type": "function",
        "function": {
            "name": "acp_call_hierarchy",
            "description": (
                "分析方法的调用层次结构。支持向上查找调用者或向下查找被调用者。\n"
                "支持 file+line+column 定位或 language+symbol FQN 引用。\n"
                "参数: file(可选), line(可选), column(可选), language(可选), symbol(可选), "
                "direction(callers/callees, 必填), depth(1-5, 默认3), scope"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "源文件路径"},
                    "line": {"type": "integer", "description": "1-based 行号"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "language": {"type": "string", "description": "编程语言 (Java/Kotlin/Python 等)"},
                    "symbol": {"type": "string", "description": "FQN 符号引用"},
                    "direction": {
                        "type": "string",
                        "enum": ["callers", "callees"],
                        "description": "方向：callers(谁调用此方法)/callees(此方法调用了谁)"
                    },
                    "depth": {"type": "integer", "default": 3, "maximum": 5, "description": "递归深度（默认 3，最大 5）"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "default": "project_files"
                    }
                }
            }
        }
    },
    # ── type_hierarchy: 获取类型继承层次 ──
    {
        "type": "function",
        "function": {
            "name": "acp_type_hierarchy",
            "description": (
                "获取类/接口的完整继承层次结构。显示父类链和所有子类。\n"
                "支持 className FQN 或 file+line+column 定位。\n"
                "参数: className(可选FQN), file(可选), line(可选), column(可选), scope"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "className": {"type": "string", "description": "FQN 类名，如 com.example.UserService"},
                    "file": {"type": "string", "description": "源文件路径（用于无 className 时的定位）"},
                    "line": {"type": "integer", "description": "1-based 行号"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "scope": {
                        "type": "string",
                        "enum": ["project_files", "project_and_libraries", "project_production_files", "project_test_files"],
                        "default": "project_files"
                    }
                }
            }
        }
    },
    # ── diagnostics: 获取 IDE 诊断 ──
    {
        "type": "function",
        "function": {
            "name": "acp_diagnostics",
            "description": (
                "获取 IDE 代码诊断信息，包括错误、警告、快速修复、构建错误和测试结果。\n"
                "可按文件过滤（可选）、设置严重级别、包含构建错误和测试结果。\n"
                "参数: file(可选), severity(errors/warnings/all), includeBuildErrors(默认false), "
                "includeTestResults(默认false), startLine(可选), endLine(可选), "
                "maxBuildErrors(默认100), maxTestResults(默认100)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（可选，用于按文件过滤诊断）"},
                    "severity": {
                        "type": "string",
                        "enum": ["all", "errors", "warnings"],
                        "default": "all",
                        "description": "严重级别过滤：all(默认)/errors/warnings"
                    },
                    "includeBuildErrors": {"type": "boolean", "default": False, "description": "是否包含构建错误"},
                    "includeTestResults": {"type": "boolean", "default": False, "description": "是否包含测试结果"},
                    "startLine": {"type": "integer", "description": "起始行号（用于按行范围过滤）"},
                    "endLine": {"type": "integer", "description": "结束行号"},
                    "maxBuildErrors": {"type": "integer", "default": 100, "maximum": 500, "description": "最多返回构建错误数"},
                    "maxTestResults": {"type": "integer", "default": 100, "maximum": 500, "description": "最多返回测试结果数"}
                }
            }
        }
    },
    # ── refactor_rename: 安全重命名符号 ──
    {
        "type": "function",
        "function": {
            "name": "acp_refactor_rename",
            "description": (
                "安全重命名符号（类/方法/变量/文件），自动更新所有引用。\n"
                "支持符号重命名(file+line+column+newName)和文件重命名(file+newName, 无line/column)。\n"
                "newName 为必填。自动处理 getter/setter、重写方法、测试类等关联元素。\n"
                "参数: file(必填), line(可选, 符号重命名), column(可选), "
                "newName(必填), overrideStrategy(rename_base/rename_only_current/ask), "
                "relatedRenamingStrategy(all/none/accessors_and_tests/ask)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填）"},
                    "line": {"type": "integer", "description": "1-based 行号（符号重命名时必填，文件重命名时忽略）"},
                    "column": {"type": "integer", "description": "1-based 列号"},
                    "newName": {"type": "string", "description": "新名称（必填，文件重命名需包含扩展名）"},
                    "overrideStrategy": {
                        "type": "string",
                        "enum": ["rename_base", "rename_only_current", "ask"],
                        "default": "rename_base",
                        "description": "重写方法策略：rename_base(重命名基类和所有重写)/rename_only_current(仅当前)/ask(交互)"
                    },
                    "relatedRenamingStrategy": {
                        "type": "string",
                        "enum": ["all", "none", "accessors_and_tests", "ask"],
                        "default": "all",
                        "description": "关联元素重命名策略：all/none/accessors_and_tests/ask"
                    }
                },
                "required": ["file", "newName"]
            }
        }
    },
    # ── move_file: 移动文件 ──
    {
        "type": "function",
        "function": {
            "name": "acp_move_file",
            "description": (
                "移动文件到新目录，自动更新所有引用和包/命名空间声明。\n"
                "使用 IDE 重构引擎，保留项目内所有导入和引用的一致性。\n"
                "参数: file(源文件路径, 必填), destination(目标目录路径, 必填)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "源文件路径（必填）"},
                    "destination": {"type": "string", "description": "目标目录路径（必填，不存在则自动创建）"}
                },
                "required": ["file", "destination"]
            }
        }
    },
    # ── reformat_code: 格式化代码 ──
    {
        "type": "function",
        "function": {
            "name": "acp_reformat_code",
            "description": (
                "按项目代码风格格式化代码文件。\n"
                "支持按行范围部分格式化(需同时提供 startLine+endLine)。\n"
                "默认同时优化导入和重排成员，可通过参数关闭。\n"
                "参数: file(必填), startLine(可选), endLine(可选), "
                "optimizeImports(默认true), rearrangeCode(默认true)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填）"},
                    "startLine": {"type": "integer", "description": "格式化起始行（可选，需与 endLine 同时提供）"},
                    "endLine": {"type": "integer", "description": "格式化结束行"},
                    "optimizeImports": {"type": "boolean", "default": True, "description": "是否优化导入（默认 true）"},
                    "rearrangeCode": {"type": "boolean", "default": True, "description": "是否重排代码成员（默认 true）"}
                },
                "required": ["file"]
            }
        }
    },
    # ── optimize_imports: 仅优化导入 ──
    {
        "type": "function",
        "function": {
            "name": "acp_optimize_imports",
            "description": (
                "优化文件中的导入语句：删除未使用的导入，按项目风格整理导入顺序。\n"
                "等同于 IDE 的 Optimize Imports 操作 (Ctrl+Alt+O / Cmd+Opt+O)。\n"
                "参数: file(必填)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填）"}
                },
                "required": ["file"]
            }
        }
    },
    # ── safe_delete: 安全删除 ──
    {
        "type": "function",
        "function": {
            "name": "acp_safe_delete",
            "description": (
                "安全删除符号或文件。先检查引用，如有引用则需确认是否强制删除。\n"
                "支持删除符号(file+line+column, 默认)和删除文件(target_type='file')。\n"
                "参数: file(必填), line(符号删除时必填), column(符号删除时必填), "
                "target_type(symbol/file, 默认symbol), force(是否强制删除, 默认false)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填）"},
                    "line": {"type": "integer", "description": "1-based 行号（符号删除时必填）"},
                    "column": {"type": "integer", "description": "1-based 列号（符号删除时必填）"},
                    "target_type": {
                        "type": "string",
                        "enum": ["symbol", "file"],
                        "default": "symbol",
                        "description": "删除类型：symbol(删除符号，默认)/file(删除文件)"
                    },
                    "force": {"type": "boolean", "default": False, "description": "存在引用时是否强制删除（默认 false）"}
                },
                "required": ["file"]
            }
        }
    },
    # ── convert_java_to_kotlin: Java→Kotlin 转换 ──
    {
        "type": "function",
        "function": {
            "name": "acp_convert_java_to_kotlin",
            "description": (
                "将 Java 文件转换为 Kotlin 文件。使用 IDE 内置转换器。\n"
                "自动处理类、接口、枚举、注解、方法、字段等的转换。\n"
                "转换后原 Java 文件被删除。\n"
                "参数: files(Java文件路径数组, 必填)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要转换的 Java 文件路径数组（必填，相对项目根）"
                    }
                },
                "required": ["files"]
            }
        }
    },
    # ── sync_files: 同步文件系统 ──
    {
        "type": "function",
        "function": {
            "name": "acp_sync_files",
            "description": (
                "强制 IDE 同步其虚拟文件系统和 PSI 缓存与外部文件变更。\n"
                "当文件在 IDE 外部创建/修改/删除（如通过 git pull）后，"
                "代码智能操作（搜索/导航/重构）可能不可靠时使用。\n"
                "参数: paths(可选文件路径数组，不传则同步整个项目)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要同步的文件/目录路径数组（可选，不传则同步整个项目）"
                    }
                }
            }
        }
    },
    # ── active_file: 获取当前活动文件 ──
    {
        "type": "function",
        "function": {
            "name": "acp_active_file",
            "description": (
                "获取 IDE 编辑器中当前打开的活动文件信息。\n"
                "返回文件路径、光标位置(行号/列号)、选中文本(如有)和编程语言。\n"
                "支持分屏视图，可返回多个活动编辑器信息。\n"
                "无参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    # ── open_file: 打开文件 ──
    {
        "type": "function",
        "function": {
            "name": "acp_open_file",
            "description": (
                "在 IDE 编辑器中打开指定文件，可选定位到特定行/列。\n"
                "参数: file(文件路径, 必填), line(可选, 1-based), column(可选)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填，相对或绝对路径）"},
                    "line": {"type": "integer", "description": "1-based 行号（可选，打开后定位到此行）"},
                    "column": {"type": "integer", "description": "1-based 列号（可选，需与 line 同时提供）"}
                },
                "required": ["file"]
            }
        }
    },
    # ── file_structure: 获取文件结构 ──
    {
        "type": "function",
        "function": {
            "name": "acp_file_structure",
            "description": (
                "获取源文件的结构层次（类似 IDE 的 Structure 视图）。\n"
                "显示类、方法、字段、函数、枚举、Markdown 标题及其嵌套关系。\n"
                "支持 Java/Kotlin/Python/JavaScript/TypeScript/PHP/Markdown。\n"
                "参数: file(必填)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填，相对项目根）"}
                },
                "required": ["file"]
            }
        }
    },
    # ── build_project: 编译项目返回结构化错误 ──
    {
        "type": "function",
        "function": {
            "name": "acp_build_project",
            "description": (
                "使用 IDE 编译系统编译项目，返回结构化编译错误（含文件路径、行号、列号、错误代码、严重级别）。\n"
                "比 execute_command + gradle 更适合 LLM 自助修复。\n"
                "参数: rebuild(是否clean build), includeRawOutput, timeoutSeconds(默认120)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rebuild": {"type": "boolean", "default": False, "description": "是否完整重建（clean build），默认 false（增量编译）"},
                    "includeRawOutput": {"type": "boolean", "default": False, "description": "是否包含原始编译器输出，默认 false"},
                    "timeoutSeconds": {"type": "integer", "default": 120, "description": "编译超时秒数，默认 120"}
                }
            }
        }
    },
    # ── get_documentation: 获取文档/API 参考 ──
    {
        "type": "function",
        "function": {
            "name": "acp_get_documentation",
            "description": "获取 IDE 中指定符号（类/方法）的文档和 API 参考信息。支持 className 和可选 memberName 参数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "className": {"type": "string", "description": "类名（必填，支持 FQN）"},
                    "memberName": {"type": "string", "description": "成员名（可选，如方法名、字段名）"}
                },
                "required": ["className"]
            }
        }
    },
    # ── apply_quickfix: 应用快速修复（需审批）──
    {
        "type": "function",
        "function": {
            "name": "acp_apply_quickfix",
            "description": "应用 IDE 快速修复。使用 diagnostics 获取可用修复后调用此工具应用指定修复。需要用户审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "文件路径（必填）"},
                    "line": {"type": "integer", "description": "1-based 行号（必填）"},
                    "column": {"type": "integer", "description": "1-based 列号（必填）"},
                    "fixName": {"type": "string", "description": "修复名称（必填）"}
                },
                "required": ["file", "line", "column", "fixName"]
            }
        }
    },
    # ── git_status: Git 状态 ──
    {
        "type": "function",
        "function": {
            "name": "acp_git_status",
            "description": "查看 Git 仓库状态（modified/staged/untracked 文件列表）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "verbose": {"type": "boolean", "default": False, "description": "是否显示详细变更内容（默认 false）"}
                }
            }
        }
    },
    # ── git_diff: Git 差异 ──
    {
        "type": "function",
        "function": {
            "name": "acp_git_diff",
            "description": "查看 Git 差异。支持 staged/working tree 差异，可选 stat_only 仅显示统计信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean", "default": False, "description": "是否显示已暂存文件的差异（默认 false）"},
                    "stat_only": {"type": "boolean", "default": False, "description": "是否仅显示统计信息（默认 false）"},
                    "commit": {"type": "string", "description": "比较的 commit hash（可选）"},
                    "path": {"type": "string", "description": "限定路径（可选）"}
                }
            }
        }
    },
    # ── git_stage: 暂存文件（需审批）──
    {
        "type": "function",
        "function": {
            "name": "acp_git_stage",
            "description": "暂存文件变更（git add）。默认暂存所有变更，也可指定文件列表。需要用户审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要暂存的文件路径列表（可选，不传则暂存所有变更）"
                    },
                    "all": {"type": "boolean", "default": False, "description": "是否暂存所有变更（包括新文件）"}
                }
            }
        }
    },
    # ── git_commit: 提交变更（需审批）──
    {
        "type": "function",
        "function": {
            "name": "acp_git_commit",
            "description": "创建 Git 提交（git commit）。默认暂存所有变更后提交。需要用户审批。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "提交信息（必填）"},
                    "all": {"type": "boolean", "default": True, "description": "是否暂存所有变更后提交（默认 true）"},
                    "amend": {"type": "boolean", "default": False, "description": "是否修正上一次提交（默认 false）"}
                },
                "required": ["message"]
            }
        }
    },
]

# ACP 活跃时过滤基础工具中与 ACP 代理工具重名的（见 acp_routes.ACP_OVERLAP_BASE_TOOL_NAMES）
_ACP_OVERLAP_BASE_TOOL_NAMES = ACP_OVERLAP_BASE_TOOL_NAMES

# 无关工具：IDE 编辑场景不需要的工具
_ACP_IRRELEVANT_TOOL_NAMES = frozenset({
    "convert_csv_to_xlsx",
    "convert_html_to_pdf",
    "convert_html_to_pptx",
    "convert_markdown_to_docx",
    "convert_markdown_to_pdf",
    "plaza_add_comment",
    "plaza_create_post",
    "plaza_create_reply",
    "generate_image",
    "generate_audio",
    "generate_video",
    "convert_pdf_to_text",
    "convert_docx_to_markdown",
})


# ──────────────────────────────────────────────
# 钩子安装
# ──────────────────────────────────────────────

_installed = False


def install_acp_tool_hooks() -> None:
    """安装 ACP 工具钩子（idempotent）。

    使用链式模式注册到 _bridge_handlers。

    同时包裹 agent_tools.get_agent_tools_for_llm，
    在 ACP 活跃时路由到 IDE 插件。
    """
    global _installed, _base_execute_tool_snapshot
    if _installed:
        logger.debug("[ACP] 工具钩子已安装，跳过")
        return

    # ── 在首次安装时捕获基础 tool 入口并设置链式分发器 ──
    # 如果 _chained_execute_tool 尚未设置（首次安装），
    # 捕获当前 agent_tools.execute_tool（可能是原始实现或其他插件的 wrapper）。
    # 之后用 _chained_execute_tool 替换，后续安装者只需 append handler。
    if not _bridge_handlers:
        _base_execute_tool_snapshot = agent_tools.execute_tool
        agent_tools.execute_tool = _chained_execute_tool
        logger.info("[ACP] 初始化链式分发器 _chained_execute_tool")

    _base_get_tools = agent_tools.get_agent_tools_for_llm

    # ── 工具名映射（见 acp_routes.ACP_TOOL_MAP）──
    _ACP_TOOL_MAP = ACP_TOOL_MAP

    async def _acp_aware_execute_tool(
        tool_name: str,
        args: dict,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str = "",
        on_output=None,
    ) -> str | None:
        """ACP 感知的工具执行路由。
        返回 None 表示未处理（由链中后续 handler 或 base handler 处理）。

        优先级：
        1. ACP handler 活跃 + 文件工具 → ACP WebSocket
        2. ACP handler 活跃 + terminal 工具 → ACP terminal
        3. 其余 → 返回 None 让链继续
        """
        handler = current_acp_handler.get()
        if handler is None:
            return None  # ACP 不活跃，让链中其他 handler 尝试

        route_t0 = time.perf_counter()

        # 文件工具 → ACP
        if tool_name in _ACP_TOOL_MAP:
            from .tool_bridge import _try_acp_execute as _acp_file_exec
            result = await _acp_file_exec(tool_name, args, handler)
            if result is not None:
                logger.info(
                    "[ACP] route=acp tool={} path={} elapsed={:.3f}s",
                    tool_name,
                    args.get("path", ""),
                    time.perf_counter() - route_t0,
                )
                return result

        # terminal 工具 → ACP（policy 路由 blocking / streaming）
        if tool_name in ("acp_execute_command", "acp_bash"):
            from .terminal_policy import TerminalMode, effective_terminal_mode, resolve_terminal_policy
            from .tool_bridge import _try_acp_terminal, _try_acp_terminal_streaming

            command = str(args.get("command", ""))
            policy = resolve_terminal_policy(command)
            mode = effective_terminal_mode(policy)
            _cancel = getattr(handler, "_cancel_event", None)

            if mode == TerminalMode.BLOCKING:
                result = await _try_acp_terminal(args, handler, policy=policy)
                if result is not None:
                    logger.info(
                        "[ACP] route=acp-blocking tool={} bucket={} cmd={} elapsed={:.3f}s",
                        tool_name,
                        policy.bucket,
                        command[:80],
                        time.perf_counter() - route_t0,
                    )
                    return result
                return "⚠️ 终端 blocking 执行未返回结果"

            result = await _try_acp_terminal_streaming(
                args, handler, cancel_event=_cancel, policy=policy,
            )
            if result is not None:
                logger.info(
                    "[ACP] route=acp-streaming tool={} cmd={} elapsed={:.3f}s",
                    tool_name,
                    command[:80],
                    time.perf_counter() - route_t0,
                )
                return result
            logger.info(
                "[ACP] route=acp-streaming tool={} cmd={} streaming...",
                tool_name,
                command[:80],
            )
            return "(terminal 流式输出中，请等待终端面板更新...)"

        # 未命中任何 ACP 路由，返回 None 让链继续
        logger.debug(
            "[ACP] unhandled={} elapsed={:.3f}s", tool_name, time.perf_counter() - route_t0,
        )
        return None

    async def _acp_aware_get_tools(agent_id: uuid.UUID) -> list[dict]:
        """ACP 感知的工具注册路由。"""
        handler = current_acp_handler.get()
        if handler is not None:
            tools = await _base_get_tools(agent_id)
            # 过滤重名基础工具
            tools = [t for t in tools
                     if t.get("function", {}).get("name", "") not in _ACP_OVERLAP_BASE_TOOL_NAMES]
            # 过滤无关工具
            tools = [t for t in tools
                     if t.get("function", {}).get("name", "") not in _ACP_IRRELEVANT_TOOL_NAMES]
            ide_names = [t["function"]["name"] for t in _ACP_IDE_TOOLS]
            logger.info("[ACP] 注册工具: base_count={} acp_tools={}", len(tools), ide_names)
            return tools + _ACP_IDE_TOOLS
        return await _base_get_tools(agent_id)

    # 注册到链式 handler 列表
    _bridge_handlers.append(_acp_aware_execute_tool)
    logger.info("[ACP] ACP handler 注册到链, 当前 handler 数: {}", len(_bridge_handlers))

    # get_agent_tools_for_llm 仍然直接覆盖（注册路径不参与链式调用）
    agent_tools.get_agent_tools_for_llm = _acp_aware_get_tools

    # caller.py 在函数体内做延迟导入，每次调用拿到最新 agent_tools.XXX，无需猴子补丁

    _installed = True
    logger.info("[ACP] tool hooks installed (chain mode, handlers={})", len(_bridge_handlers))
