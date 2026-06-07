"""ACP 工具桥 — 文件操作 + 终端代理。

通过当前活跃的 AcpHandler 将工具调用代理到 IDE 插件执行。

ContextVar:
- current_acp_handler: AcpHandler | None — 活跃的 ACP 会话 handler
"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar
from typing import Any

from loguru import logger

current_acp_handler: ContextVar[Any | None] = ContextVar("current_acp_handler", default=None)

# ACP 协议方法映射
_ACP_METHOD_MAP = {
    "read_file": "fs/read_text_file",
    "write_file": "fs/write_text_file",
    "edit_file": "fs/write_text_file",
    "delete_file": "fs/write_text_file",
}


def _is_project_file(path: str) -> bool:
    """判断路径是否是 IDE 项目文件（应走 ACP）还是 agent 自身文件（走本地）。"""
    if not path:
        return False
    if path.startswith("/"):
        return True
    # agent 自身文件前缀
    agent_prefixes = ("memory/", "skills/", "enterprise_info/", "workspace/")
    if any(path.startswith(p) for p in agent_prefixes):
        return False
    agent_files = ("soul.md", "focus.md", "memory.md", "tasks.json")
    if path.split("/")[-1] in agent_files:
        return False
    return True  # 其余相对路径 → IDE 项目根


async def _try_acp_execute(tool_name: str, args: dict, handler) -> str | None:
    """通过 ACP 协议执行文件操作。

    返回 None 表示不应由 ACP 处理（如 path 为空或 agent 自身文件）。
    返回字符串表示 ACP 执行结果。
    """
    path = args.get("path") or args.get("file_path") or args.get("filePath", "")
    if not _is_project_file(path):
        return None

    method = _ACP_METHOD_MAP.get(tool_name)
    if not method:
        return None

    session_id = getattr(handler, "session_id", "")
    conn_id = getattr(handler, "conn_id", "?")

    if method == "fs/read_text_file":
        params = {"sessionId": session_id, "path": path}
        line = args.get("line")
        if line is not None:
            params["line"] = int(line)
        limit = args.get("limit")
        if limit is not None:
            params["limit"] = int(limit)
    elif method == "fs/write_text_file":
        content = args.get("content", "")
        params = {
            "sessionId": session_id,
            "path": path,
            "content": content if tool_name != "delete_file" else "",
        }
    else:
        return None

    t0 = time.perf_counter()
    logger.info(f"[ACP-PERF] fs START tool={tool_name} path={path} session={session_id}")
    try:
        result = await handler.send_request(method, params, timeout=float(os.getenv("ACP_FS_TIMEOUT", "15")))
        logger.info(
            f"[ACP-PERF] fs DONE tool={tool_name} path={path} session={session_id} "
            f"elapsed={time.perf_counter() - t0:.3f}s"
        )
        if method == "fs/read_text_file":
            if isinstance(result, dict):
                return result.get("content", "")
            return str(result)
        return "文件操作成功"
    except TimeoutError:
        logger.warning(f"[ACP-bridge] timeout: {tool_name} {path}, 尝试本地读取")
        if method == "fs/read_text_file" and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                logger.info(f"[ACP-bridge] 本地读取成功: {path} ({len(content)} bytes)")
                return content
            except Exception as e:
                logger.error(f"[ACP-bridge] 本地读取失败: {path}: {e}")
                return f"⚠️ 文件读取失败: {e}"
        return f"⚠️ IDE 文件操作超时: {path}"
    except Exception as e:
        logger.error(f"[ACP-bridge conn={conn_id}] error: {tool_name} path={path} session={session_id}: {e}")
        return f"⚠️ IDE 文件操作失败: {e}"


async def _try_acp_terminal(args: dict, handler) -> str | None:
    """通过 ACP terminal 协议执行命令。

    流程: terminal/create → terminal/wait_for_exit → terminal/output

    返回 None 表示 command 为空。
    返回字符串表示执行结果。
    """
    command = args.get("command", "")
    if not command:
        return None

    session_id = getattr(handler, "session_id", "")
    conn_id = getattr(handler, "conn_id", "?")

    t0 = time.perf_counter()
    logger.info(f"[ACP-PERF] terminal START session={session_id} cmd={command[:120]}")
    try:
        # 1. 创建终端并执行命令
        create_result = await handler.send_request("terminal/create", {
            "sessionId": session_id,
            "command": command,
        }, timeout=float(os.getenv("ACP_TERMINAL_CREATE_TIMEOUT", "30")))

        terminal_id = ""
        if isinstance(create_result, dict):
            terminal_id = create_result.get("terminalId", "")

        if not terminal_id:
            return "终端创建失败"

        # 2. 等待命令执行完毕
        await handler.send_request("terminal/wait_for_exit", {
            "sessionId": session_id,
            "terminalId": terminal_id,
        }, timeout=float(os.getenv("ACP_TERMINAL_WAIT_TIMEOUT", "300")))

        # 3. 读取终端输出
        output = await handler.send_request("terminal/output", {
            "sessionId": session_id,
            "terminalId": terminal_id,
        }, timeout=float(os.getenv("ACP_TERMINAL_OUTPUT_TIMEOUT", "30")))

        if isinstance(output, dict):
            out_text = output.get("output", "")
            logger.info(
                f"[ACP-PERF] terminal DONE session={session_id} terminalId={terminal_id} "
                f"elapsed={time.perf_counter() - t0:.3f}s outputLen={len(out_text)}"
            )
            return out_text

        out_text = str(output) if output else "命令执行完毕，无输出"
        logger.info(
            f"[ACP-PERF] terminal DONE session={session_id} terminalId={terminal_id} "
            f"elapsed={time.perf_counter() - t0:.3f}s outputLen={len(out_text)}"
        )
        return out_text

    except TimeoutError:
        logger.error(
            f"[ACP-PERF] terminal TIMEOUT session={session_id} "
            f"elapsed={time.perf_counter() - t0:.3f}s cmd={command[:80]}"
        )
        return f"⚠️ 终端命令超时: {command[:80]}"
    except Exception as e:
        logger.error(f"[ACP-terminal conn={conn_id}] error: {command[:80]} session={session_id}: {e}")
        return f"⚠️ 终端执行失败: {e}"
