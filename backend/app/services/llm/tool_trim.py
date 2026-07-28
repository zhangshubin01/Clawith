"""工具结果注入期压缩 — 共享单一真源（Layer 0）。

设计（评审共识）：
- 触发按 **token 预算**（`_est_tokens_str`），CJK/代码不因字符数失真。
- 压缩走 **类型感知 `_dispatch`**（复用 context_compressor 的 _detect/_json/_search/...），
  而非哑字符截断 —— 与 Layer 1 `_multi_role_compress` 共用同一套类型逻辑。
- `never_worse`：压缩后 token 不小于原始则回退原文（rtk 模式）。
- 硬天花板 `_TOOL_HARD_CEIL_CHARS`：仅防单条巨型结果 OOM，正常路径不触及。
- `_COMPRESS_MARKER`：插入压缩结果首行，供 Layer 1 检测跳过，避免双压。

调用方：
- caller._process_tool_call（注入期，所有经 call_llm 的路径）
"""

from __future__ import annotations

from loguru import logger

from .compression_config import is_tool_excluded
from .context_compressor import _est_tokens_str

# 程序可读压缩标记（统一真源）。
_COMPRESS_MARKER = "<!-- ctx:trimmed -->"

# 按工具的 token 预算（相对 100K 窗口基线，运行时随窗口缩放）。
# read_file 需要更多上下文；grep/search 有类型摘要可更激进；terminal 日志重复性高。
_TOOL_TOKEN_BUDGET: dict[str, int] = {
    "read_file": 6000,
    "read_document": 6000,
    "search_file": 3000,
    "search_files": 3000,
    "search_codebase": 3000,
    "search_symbol": 3000,
    "search_clawhub": 3000,
    "list_dir": 3000,
    "list_files": 3000,  # ACP/agent_tools 实际传 list_files；缺失会落 _default 并走 _text（有损根因）
    "list_focus_items": 2000,
    "execute_command": 2000,
    "run_in_terminal": 2000,
    "_default": 2000,
}

# 工具名 → 压缩类型路由。优先于 _detect(content) 内容猜测，修 list_files 被判 text 等误判。
# 两套命名空间：agent_tools(list_files/read_file/search_files) + ACP IDE(list_dir/read_document/search_*)。
_TOOL_TYPE_ROUTE: dict[str, str] = {
    "list_files": "list",
    "list_dir": "list",
    "list_focus_items": "list",
    "read_file": "code",
    "read_document": "code",
    "search_files": "search",
    "search_file": "search",
    "search_codebase": "search",
    "search_symbol": "search",
    "search_clawhub": "search",
    "run_in_terminal": "log",
    "execute_command": "log",
}


def _list_head_tail(content: str, head: int = 40, tail: int = 20) -> str:
    """目录/文件列表专用：保留有序首尾 + 省略行数统计。绝不打分删行（保持顺序无损语义）。

    完整列表由调用方 CCR 归档，Agent 可 retrieve_context 取回。
    """
    lines = content.split("\n")
    n = len(lines)
    if n <= head + tail:
        return content
    omitted = n - head - tail
    body = lines[:head] + [f"... [{omitted} 行已省略，共 {n} 行；完整列表见 CCR retrieve] ..."] + lines[-tail:]
    return "\n".join(body)

# 硬天花板：任何工具结果超过此字符数一律 head+tail 截断（防 OOM）。
_TOOL_HARD_CEIL_CHARS = 16384  # 批次 A 2.3：16K chars ≈ 4K tok


def _tool_token_budget(tool_name: str, ctx_window: int) -> int:
    """按工具派生 token 预算，随模型窗口相对 100K 缩放（0.5x~2x 夹逼）。"""
    base = _TOOL_TOKEN_BUDGET.get(tool_name, _TOOL_TOKEN_BUDGET["_default"])
    if ctx_window <= 0:
        return base
    scale = ctx_window / 100000.0
    scale = max(0.5, min(2.0, scale))
    return int(base * scale)


def _deep_round_budget_factor(round_i: int, session_pressure: float = 0.0) -> float:
    """F6：深 loop 预算乘数 — 避免单条 under_budget 在多轮累积。"""
    from app.config import get_settings

    s = get_settings()
    start = int(getattr(s, "CTX_DEEP_ROUND_START", 8) or 8)
    step = float(getattr(s, "CTX_DEEP_ROUND_STEP", 0.05) or 0.05)
    floor = float(getattr(s, "CTX_DEEP_ROUND_BUDGET_FLOOR", 0.25) or 0.25)
    factor = 1.0
    if round_i >= start:
        factor = max(floor, 1.0 - (round_i - start + 1) * step)
    if session_pressure >= 0.30:
        factor = min(factor, max(floor, 1.0 - session_pressure * 0.6))
    return factor


def _effective_tool_budget(
    tool_name: str,
    ctx_window: int,
    *,
    round_i: int = 0,
    session_pressure: float = 0.0,
) -> int:
    """F6：深 loop 动态 per-tool token 预算。"""
    base = _tool_token_budget(tool_name, ctx_window)
    factor = _deep_round_budget_factor(round_i, session_pressure)
    return max(64, int(base * factor))


def _hard_head_tail(s: str, max_chars: int = _TOOL_HARD_CEIL_CHARS) -> str:
    """OOM 兜底：保留首尾各半，中间省略。仅极端超大结果触发。"""
    if len(s) <= max_chars:
        return s
    half = max_chars // 2
    omitted = len(s) - max_chars
    body = s[:half] + f"\n... [中间 {omitted} 字符已省略（超硬天花板）] ...\n" + s[-half:]
    if _COMPRESS_MARKER in body:
        return body
    return _COMPRESS_MARKER + "\n" + body


def _dispatch_guarded_result(
    result: str,
    tool_name: str = "",
    budget_tokens: int = 0,
    model_name: str = "",
    path: str = "",
    ctx_window: int = 100000,
    session_pressure: float = 0.0,
    user_query: str = "",
    tool_args_text: str = "",
):
    """委托 content_router.compress_one_result（结构化 Layer0 真源）。"""
    from .content_router import compress_one_result

    pressure = budget_tokens / max(ctx_window, 1)
    return compress_one_result(
        result,
        tool_name=tool_name,
        budget_tokens=budget_tokens,
        model_name=model_name,
        path=path,
        ctx_window=ctx_window,
        pressure=pressure,
        session_pressure=session_pressure,
        user_query=user_query,
        tool_args_text=tool_args_text,
    )


def _dispatch_guarded(
    result: str,
    tool_name: str = "",
    budget_tokens: int = 0,
    model_name: str = "",
    path: str = "",
    ctx_window: int = 100000,
    session_pressure: float = 0.0,
    user_query: str = "",
    tool_args_text: str = "",
) -> str:
    """委托 content_router.compress_one（Stage3 统一 Layer0 真源）。"""
    from .content_router import compress_one
    pressure = budget_tokens / max(ctx_window, 1)
    return compress_one(
        result,
        tool_name=tool_name,
        budget_tokens=budget_tokens,
        model_name=model_name,
        path=path,
        ctx_window=ctx_window,
        pressure=pressure,
        session_pressure=session_pressure,
        user_query=user_query,
        tool_args_text=tool_args_text,
    )
