"""工具调用结果的上下文智能截断器（仅 LSP4J 路径使用）。

防止 read_file 返回的大文件和过多历史工具记录导致 context 膨胀，
从而避免 LLM 推理延迟急剧增加。

智能压缩策略：
- 最近 RECENT_DETAIL_ROUNDS 轮：逐条保留详情
- 更早的轮次：按工具类型分组摘要，去重合并
"""

from collections import defaultdict

# 单次工具结果最大字符数（超过则截断）
MAX_TOOL_RESULT_CHARS = 4000
# 终端输出最大字符数（比普通工具更激进，终端日志重复性高）
MAX_TERMINAL_RESULT_CHARS = 2000
# 工具调用历史最大保留轮数
MAX_TOOL_HISTORY_ROUNDS = 10
# 最近 N 轮保留详情，更早的按分组摘要
RECENT_DETAIL_ROUNDS = 5
# 截断标记
TRUNCATION_MARKER = "\n\n[结果已截断，完整内容可通过 read_file 获取]"


def trim_tool_result(result_content: str, tool_name: str = "", max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """截断过长的工具结果到 max_chars，差异化策略保留最有价值的部分。"""
    if len(result_content) <= max_chars:
        return result_content

    if tool_name in ("read_file",):
        half = max_chars // 2
        head = result_content[:half]
        tail = result_content[-half:]
        return head + f"\n... [中间 {len(result_content) - max_chars} 字符已省略] ...\n" + tail

    if tool_name in ("search_file", "search_codebase", "search_symbol", "list_dir"):
        return result_content[:max_chars] + TRUNCATION_MARKER

    if tool_name in ("run_in_terminal",):
        return _trim_terminal_output_intelligent(result_content, max_chars)

    return result_content[:max_chars] + TRUNCATION_MARKER


def _trim_terminal_output_intelligent(result: str, max_chars: int = 2000) -> str:
    """终端输出智能截断：保留前 N 行 + 错误行 + 尾部，比简单头尾截断更有效。"""
    if len(result) <= max_chars:
        return result

    lines = result.split("\n")
    head_lines = 30
    head = "\n".join(lines[:head_lines])

    error_keywords = ("error", "fail", "exception", "traceback", "cannot", "unable", "denied", "ERROR", "FAIL")
    error_lines = [l for l in lines if any(kw in l for kw in error_keywords)]
    error_block = "\n".join(error_lines[:10]) if error_lines else ""

    tail_lines = []
    for l in reversed(lines):
        tail_lines.insert(0, l)
        if len("\n".join(tail_lines)) > 200:
            break
    tail = "\n".join(tail_lines)

    omitted = len(lines) - head_lines - len(error_lines[:10]) - len(tail_lines)
    parts = [head]
    if omitted > 0:
        parts.append(f"\n... [省略 {omitted} 行] ...")
    if error_block and error_block not in head:
        parts.append(error_block)
    if tail:
        parts.append(f"\n... [尾部 {len(tail_lines)} 行] ...\n{tail}")

    return "\n".join(parts)


def trim_tool_context_history(tool_records: list[dict], max_rounds: int = MAX_TOOL_HISTORY_ROUNDS) -> list[dict]:
    """只保留最近 max_rounds 轮的工具调用记录。"""
    if len(tool_records) <= max_rounds:
        return tool_records
    return tool_records[-max_rounds:]


def _extract_file_paths(results) -> list[str]:
    """从结果列表中提取文件路径，用于去重展示。"""
    paths = []
    for r in (results or [])[:20]:
        if isinstance(r, dict):
            fp = r.get("filePath", "") or r.get("file_path", "") or r.get("path", "") or r.get("fileName", "")
            if fp and fp not in paths:
                paths.append(fp)
    return paths


def compress_tool_context_summary(
    tool_records: list[dict],
    recent_detail: int = RECENT_DETAIL_ROUNDS,
) -> list[str]:
    """智能压缩工具调用上下文：最近 N 轮详情 + 早期分组摘要。

    Args:
        tool_records: 工具调用记录列表
        recent_detail: 最近 N 轮保留完整详情

    Returns:
        格式化后的行列表，可直接拼接为 system message
    """
    total = len(tool_records)
    if total <= recent_detail:
        return []  # 不需要压缩，走正常详情流程

    recent = tool_records[-recent_detail:]
    older = tool_records[:-recent_detail]

    lines = [
        f"[会话上下文] 已执行 {total} 个工具调用。最近 {recent_detail} 轮详情 + 早期 {len(older)} 次调用摘要如下：",
        "",
        "## 最近操作",
    ]

    # 最近 N 轮: 逐条详情（和原有逻辑一致）
    for i, tc in enumerate(recent, 1):
        name = tc.get("name") or tc.get("tool_name", "")
        params = tc.get("parameters") or tc.get("arguments") or tc.get("args") or {}
        results = tc.get("results") or tc.get("result") or []

        if name in ("search_file", "search_codebase", "grep_code", "search_symbol", "list_dir"):
            param_info = ""
            if name == "search_file":
                param_info = params.get("file_pattern", "") or params.get("query", "")
            elif name in ("search_codebase", "grep_code"):
                param_info = params.get("query", "") or params.get("regex", "")
            elif name == "search_symbol":
                param_info = params.get("query", "")
            elif name == "list_dir":
                param_info = params.get("relative_workspace_path", "")
            line = f"{i}. {name}({param_info[:120]})" if param_info else f"{i}. {name}"
            paths = _extract_file_paths(results)
            if paths:
                line += f": 找到 {len(results)} 个结果"
                lines.append(line)
                for fp in paths[:5]:
                    lines.append(f"   - {fp}")
                if len(paths) > 5:
                    lines.append(f"   ... 还有 {len(paths) - 5} 个")
            else:
                lines.append(line)

        elif name == "read_file":
            fp = (params.get("file_path", "") or params.get("filePath", "") or params.get("path", ""))
            lines.append(f"{i}. read_file: 已读取 {fp}" if fp else f"{i}. read_file: 已执行")

        elif name == "run_in_terminal":
            cmd = params.get("command", "")
            lines.append(f"{i}. run_in_terminal: {cmd[:150]}")

        elif name in ("replace_text_by_path", "search_replace", "create_file_with_text",
                      "delete_file_by_path", "edit_file", "write_file"):
            fp = params.get("filePath", "") or params.get("file_path", "")
            lines.append(f"{i}. {name}: {fp}" if fp else f"{i}. {name}: 已执行")
        else:
            param_keys = list(params.keys())[:3] if params else []
            lines.append(
                f"{i}. {name}: 已执行 (params={','.join(param_keys)})" if param_keys else f"{i}. {name}: 已执行"
            )

    # 早期记录: 按工具类型分组摘要
    lines.append("")
    lines.append(f"## 早期操作摘要（前 {len(older)} 次调用）")

    # 分组统计
    search_tools: list[str] = []       # search_file/search_codebase/grep_code
    search_params: set[str] = set()    # 去重的搜索关键词
    read_files: set[str] = set()       # 去重的文件路径
    list_dirs: set[str] = set()        # 去重的目录路径
    write_ops: list[str] = []          # 修改操作
    terminal_ops: list[str] = []       # 终端命令
    other_count = 0

    for tc in older:
        name = tc.get("name") or tc.get("tool_name", "")
        params = tc.get("parameters") or tc.get("arguments") or tc.get("args") or {}

        if name in ("search_file", "search_codebase", "grep_code", "search_symbol"):
            search_tools.append(name)
            query = params.get("query", "") or params.get("regex", "") or params.get("file_pattern", "")
            if query:
                search_params.add(query[:80])
        elif name == "read_file":
            fp = params.get("file_path", "") or params.get("filePath", "") or ""
            if fp:
                # 只保留文件名部分
                short = fp.split("/")[-1] if "/" in fp else fp
                read_files.add(short)
        elif name == "list_dir":
            p = params.get("relative_workspace_path", "")
            if p:
                list_dirs.add(p[:80])
        elif name in ("replace_text_by_path", "search_replace", "create_file_with_text",
                      "delete_file_by_path", "edit_file", "write_file"):
            fp = params.get("filePath", "") or params.get("file_path", "")
            short = fp.split("/")[-1] if "/" in fp else fp[:60]
            if short and short not in write_ops:
                write_ops.append(short)
        elif name == "run_in_terminal":
            cmd = params.get("command", "")[:80]
            if cmd:
                terminal_ops.append(cmd)
        else:
            other_count += 1

    if search_tools:
        kw_list = ", ".join(sorted(search_params)[:8]) if search_params else "(无关键词)"
        lines.append(f"- 搜索: {len(search_tools)} 次，涉及: {kw_list}")
    if read_files:
        files_list = ", ".join(sorted(read_files)[:10])
        lines.append(f"- 文件读取: {len(read_files)} 个文件 ({files_list})")
    if list_dirs:
        dirs_list = ", ".join(sorted(list_dirs)[:5])
        lines.append(f"- 目录浏览: {len(list_dirs)} 个目录 ({dirs_list})")
    if write_ops:
        lines.append(f"- 代码修改: {len(write_ops)} 次 ({', '.join(write_ops[:8])})")
    if terminal_ops:
        lines.append(f"- 终端命令: {len(terminal_ops)} 次 ({', '.join(terminal_ops[:3])})")
    if other_count:
        lines.append(f"- 其他: {other_count} 次")
    lines.append("")
    lines.append("如需要上述早期操作的详细结果，可调用 read_file 或搜索工具重新获取。")

    return lines
