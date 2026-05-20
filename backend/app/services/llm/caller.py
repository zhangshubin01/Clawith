"""Unified LLM calling service with failover support for all execution paths.

This module provides a shared entry point for all LLM calls across:
- WebSocket chat
- IM channels (Feishu, Slack, Teams, Discord, WeCom, DingTalk)
- Background services (task executor, scheduler, heartbeat, etc.)

All paths now support:
1. Config-level fallback: if primary missing, use fallback directly
2. Runtime failover: if primary fails with retryable error, try fallback once
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

# #region agent log
from app.debug_trace import dbg as _dbg_llm


def _audit_tool_pairing(api_messages: list) -> dict:
    """统计 assistant tool_calls 与后续 tool 消息的配对缺口。"""
    gaps: list[dict] = []
    for i, msg in enumerate(api_messages):
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        expected = [tc.get("id") for tc in msg.tool_calls if tc.get("id")]
        found: set[str] = set()
        j = i + 1
        while j < len(api_messages) and api_messages[j].role == "tool":
            tid = api_messages[j].tool_call_id
            if tid:
                found.add(tid)
            j += 1
        missing = [x for x in expected if x not in found]
        if missing:
            gaps.append({"idx": i, "expected": len(expected), "missing": missing[:5]})
    return {"msg_count": len(api_messages), "gap_count": len(gaps), "gaps": gaps[:3]}


# #endregion

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
# 延迟导入 agent_tools 以避免循环依赖（agent_tools → llm.finish → llm.caller → agent_tools）
# 实际导入在函数体内按需执行
from app.services.token_tracker import (
    TokenUsage,
    record_token_usage,
    extract_token_usage,
    estimate_token_usage_from_chars,
)

from .client import LLMError
from .failover import classify_error, FailoverErrorType
from .finish import FINISH_PROTOCOL_REMINDER, FINISH_TOOL_DEFINITION, find_finish_call, parse_tool_arguments
from .utils import LLMMessage, create_llm_client, get_max_tokens, get_model_api_key

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.llm import LLMModel


TOOLS_REQUIRING_ARGS = frozenset(
    {
        "write_file",
        "read_file",
        "move_file",
        "delete_file",
        "read_document",
        "send_message_to_agent",
        "send_feishu_message",
        "send_email",
    }
)


def _sanitize_tool_calls_for_context(tool_calls: list[dict]) -> tuple[list[dict] | None, str | None]:
    """Return OpenAI-compatible tool calls, or a retry instruction if args are invalid."""
    sanitized: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        tool_name = fn.get("name") or ""
        raw_args = fn.get("arguments", "{}")

        if raw_args is None or raw_args == "":
            args_str = "{}"
        elif isinstance(raw_args, str):
            try:
                json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[LLM] Invalid tool arguments JSON for {}: {} at pos {}",
                    tool_name or "<unknown>",
                    exc.msg,
                    exc.pos,
                )
                return None, (
                    "Your previous tool call arguments were not valid JSON. "
                    f"The affected tool was `{tool_name or 'unknown'}`. "
                    "Retry the tool call now with `function.arguments` as one valid JSON object string. "
                    "Escape all quotes and newlines inside long HTML, CSS, JavaScript, or markdown content. "
                    "Do not explain; only retry with a valid tool call."
                )
            args_str = raw_args
        elif isinstance(raw_args, (dict, list)):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            return None, (
                "Your previous tool call arguments had an unsupported type. "
                f"The affected tool was `{tool_name or 'unknown'}`. "
                "Retry the tool call with `function.arguments` as one valid JSON object string."
            )

        sanitized.append(
            {
                "id": tc.get("id", ""),
                "type": tc.get("type") or "function",
                "function": {
                    "name": tool_name,
                    "arguments": args_str,
                },
            }
        )

    return sanitized, None


# ═══════════════════════════════════════════════════════════════════════════════
# Failover Guard
# ═══════════════════════════════════════════════════════════════════════════════


class FailoverGuard:
    """Guard state for failover decisions."""

    def __init__(self):
        self.tool_executed = False
        self.streaming_started = False
        self.failover_done = False

    def mark_tool_executed(self):
        """Mark that a side-effecting tool has been executed."""
        self.tool_executed = True

    def mark_streaming_started(self):
        """Mark that streaming output has started."""
        self.streaming_started = True

    def mark_failover_done(self):
        """Mark that failover has already happened once."""
        self.failover_done = True

    def can_failover(self) -> bool:
        """Check if failover is allowed based on guard rules."""
        if self.failover_done:
            return False  # Only failover once
        if self.tool_executed:
            return False  # Don't failover after side effects
        if self.streaming_started:
            return False  # Don't failover after streaming started
        return True


def _is_llm_error(response: dict) -> bool:
    """结构化检测 LLM 错误——不再依赖字符串前缀匹配 (#69 修复)。

    检查 response 的结构化字段而非 content 字符串，更可靠地判断 LLM 调用是否异常。
    """
    if not response:
        return True
    if response.get("finish_reason") == "error":
        return True
    if response.get("tool_calls") is not None and not isinstance(response["tool_calls"], list):
        return True
    return False


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.

    Uses unified classification from failover.py.

    核心原则：先判断是否为错误，再判断是否可重试。
    非错误响应（正常 LLM 回复）绝不应被分类为可重试。
    """
    result_lower = result.lower()
    is_error = result.startswith("[LLM Error]") or result.startswith("[LLM call error]") or result.startswith("[Error]")

    # 第一步：如果不是错误，直接返回 False
    if not is_error:
        return False

    # 第二步：已确认为错误，检查限流关键词
    if any(kw in result_lower for kw in ["rate limit", "too many requests"]):
        return True

    # 第三步：已确认为错误，检查 HTTP 状态码
    http_context_keywords = ["status", "code", "http", "error"]
    for code in ["429", "500", "502", "503", "504"]:
        idx = result_lower.find(code)
        while idx != -1:
            window = result_lower[max(0, idx - 30) : idx + len(code) + 30]
            if any(kw in window for kw in http_context_keywords):
                return True
            idx = result_lower.find(code, idx + 1)

    # 第四步：使用分类器判断
    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _get_model_timeout(model: "LLMModel") -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _get_thinking_kwargs(model: "LLMModel", round_i: int | None = None) -> dict:
    """DeepSeek V4 思考模式参数检测。

    DeepSeek V4（deepseek-v4-pro / deepseek-v4-flash）需要显式传入
    thinking={"type": "enabled"} 开启思考模式，否则 API 返回 400 错误。
    参考：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode

    全程保持 thinking=enabled。中途切换 disabled→enabled 会导致 API 400：
    历史 assistant 消息中部分有 reasoning_content、部分无，API 拒绝不一致的对话历史。

    Returns:
        DeepSeek V4: {"thinking": {"type": "enabled"}}
        其他模型: {}
    """
    model_name = getattr(model, "model", "") or ""
    if "deepseek-v4" in model_name or "deepseek_v4" in model_name:
        return {"thinking": {"type": "enabled"}}
    return {}


def _usage_from_response_or_estimate(response, api_messages: list[LLMMessage]) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    round_chars = sum(len(m.content or "") if isinstance(m.content, str) else 0 for m in api_messages)
    round_chars += len(response.content or "")
    return estimate_token_usage_from_chars(round_chars)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def _get_agent_config(agent_id) -> tuple[int, str | None]:
    """Get agent config: max_tool_rounds and token limit status."""
    if not agent_id:
        return 50, None

    try:
        from app.models.agent import Agent as AgentModel

        async with async_session() as _db:
            _ar = await _db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _ar.scalar_one_or_none()
            if _agent:
                max_rounds = _agent.max_tool_rounds or 50
                if _agent.max_tokens_per_day and _agent.tokens_used_today >= _agent.max_tokens_per_day:
                    return (
                        max_rounds,
                        f"⚠️ Daily token usage has reached the limit ({_agent.tokens_used_today:,}/{_agent.max_tokens_per_day:,}). Please try again tomorrow or ask admin to increase the limit.",
                    )
                if _agent.max_tokens_per_month and _agent.tokens_used_month >= _agent.max_tokens_per_month:
                    return (
                        max_rounds,
                        f"⚠️ Monthly token usage has reached the limit ({_agent.tokens_used_month:,}/{_agent.max_tokens_per_month:,}). Please ask admin to increase the limit.",
                    )
                return max_rounds, None
    except Exception:
        pass
    return 50, None


def format_tool_rounds_limit_reply(max_rounds: int) -> str:
    """工具循环触顶时返回给用户的双语说明（不依赖 IDE 是否传 locale）。"""
    return (
        f"已达到本智能体的工具调用上限（{max_rounds} 轮）。"
        "建议将任务拆成更小的步骤，或开启新对话后继续；"
        "也可在 Clawith 智能体设置中调整「最大工具调用轮次」。\n\n"
        f"The tool call limit for this agent has been reached ({max_rounds} rounds). "
        "Try splitting the task into smaller steps or starting a new chat; "
        'you can also raise "Max tool call rounds" in the agent settings.'
    )


async def _get_user_name(user_id) -> str | None:
    """Get user's display name for personalized context."""
    if not user_id:
        return None
    try:
        from app.models.user import User as _UserModel

        async with async_session() as _udb:
            _ur = await _udb.execute(select(_UserModel).where(_UserModel.id == user_id))
            _u = _ur.scalar_one_or_none()
            if _u:
                return _u.display_name or _u.username
    except Exception:
        pass
    return None


def _convert_messages_for_vision(api_messages: list, supports_vision: bool) -> list:
    """Convert image markers to vision format if supported, or strip them."""
    import re as _re_v
    import copy

    # Deep copy to avoid modifying the original list in place
    new_messages = copy.deepcopy(api_messages)

    if supports_vision:
        # Vision format: convert image markers in strings to OpenAI Vision API list format
        for i, msg in enumerate(new_messages):
            if msg.role != "user" or not msg.content or not isinstance(msg.content, str):
                continue

            content_str = msg.content
            pattern = r"\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]"
            images = _re_v.findall(pattern, content_str)

            if not images:
                continue

            text = _re_v.sub(pattern, "", content_str).strip()
            parts = [{"type": "image_url", "image_url": {"url": img}} for img in images]
            if text:
                # Per OpenAI spec, text part should come after image parts
                parts.append({"type": "text", "text": text})

            new_messages[i] = type(msg)(
                role=msg.role, content=parts, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
            )
    else:
        # Non-vision format: ensure content is a string for all roles, stripping image data.
        _img_marker_pattern = r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]"
        for i, msg in enumerate(new_messages):
            if isinstance(msg.content, list):
                # It's a list, join all text parts. This handles user messages
                # with vision content and tool messages from vision_inject.
                text_parts = [part.get("text", "") for part in msg.content if part.get("type") == "text"]
                content_str = "\n".join(text_parts).strip()
                new_messages[i] = type(msg)(
                    role=msg.role, content=content_str, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

            elif isinstance(msg.content, str) and "[image_data:" in msg.content:
                # It's a string with image markers, strip them
                _n_imgs = len(_re_v.findall(_img_marker_pattern, msg.content))
                cleaned = _re_v.sub(_img_marker_pattern, "", msg.content).strip()
                if _n_imgs > 0:
                    cleaned += f"\n[用户发送了 {_n_imgs} 张图片，但当前模型不支持视觉，无法查看图片内容]"
                new_messages[i] = type(msg)(
                    role=msg.role, content=cleaned, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

    return new_messages


def _check_tool_requires_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """Check if tool requires arguments and return (should_execute, result_or_error)."""
    if not args and tool_name in TOOLS_REQUIRING_ARGS:
        return (
            False,
            f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments.",
        )
    return True, ""


def _allowed_tool_names(tools_for_llm: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools_for_llm or []:
        name = ((tool.get("function") or {}).get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _tool_not_enabled_message(tool_name: str) -> str:
    return (
        f"Tool `{tool_name}` is not enabled for this agent. "
        "Do not call it again. Use only the tools currently available to you, "
        "or explain that the required capability is not enabled."
    )


async def _process_tool_call(
    tc: dict,
    api_messages: list,
    agent_id,
    user_id,
    session_id: str,
    supports_vision: bool,
    on_tool_call,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
    tool_result_cache: dict[str, str | list] | None = None,
    on_code_output=None,
) -> str:
    from app.services.agent_tools import execute_tool  # 延迟导入，避免循环依赖
    """Process a single tool call and return result."""
    fn = tc["function"]
    tool_name = fn["name"]
    raw_args = fn.get("arguments", "{}")
    logger.info(f"[LLM] Calling tool: {tool_name}({json.dumps(raw_args, ensure_ascii=False)[:100]})")

    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        args = {}

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        api_messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=tc.get("id", ""),
                content=error_msg or "[工具参数无效，已跳过]",
            )
        )
        return ""

    if tool_name not in allowed_tool_names:
        result = _tool_not_enabled_message(tool_name)
        logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
        if on_tool_call:
            try:
                await on_tool_call(
                    {
                        "name": tool_name,
                        "call_id": tc.get("id", ""),
                        "args": args,
                        "status": "done",
                        "result": result,
                        "reasoning_content": full_reasoning_content,
                    }
                )
            except Exception:
                pass
        api_messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=tc["id"],
                content=result,
            )
        )
        return ""

    cache_key = None
    if tool_result_cache is not None:
        # Canonicalize args for stable dedupe key.
        try:
            args_key = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except TypeError:
            args_key = str(args)
        cache_key = f"{tool_name}:{args_key}"
        if cache_key in tool_result_cache:
            cached_result = tool_result_cache[cache_key]
            logger.info("[LLM-OPT] Skip duplicate tool call: {}", tool_name)
            if on_tool_call:
                try:
                    await on_tool_call(
                        {
                            "name": tool_name,
                            "call_id": tc.get("id", ""),
                            "args": args,
                            "status": "done",
                            "result": cached_result if isinstance(cached_result, str) else str(cached_result),
                            "reasoning_content": full_reasoning_content,
                        }
                    )
                except Exception as _e:
                    logger.warning(f"[LLM] on_tool_call dedupe error: {_e}")
            api_messages.append(
                LLMMessage(
                    role="tool",
                    tool_call_id=tc["id"],
                    # #119 修复：注入重复调用反馈，让 LLM 感知跳过原因
                    content="⛔ 该工具调用与之前重复，已跳过执行。请基于已有结果直接推进任务，不要重复相同调用。\n\n"
                    + (cached_result if isinstance(cached_result, str) else str(cached_result)),
                )
            )
            return ""

    # Notify client about tool call (in-progress)
    if on_tool_call:
        try:
            await on_tool_call(
                {
                    "name": tool_name,
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "running",
                    "reasoning_content": full_reasoning_content,
                }
            )
        except Exception as _e:
            logger.warning(f"[LLM] on_tool_call running error: {_e}")

    # Execute tool — pass on_output for execute_code streaming
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    # #region agent log
    _dbg_llm(
        "B",
        "caller.py:_process_tool_call:before_execute",
        "execute_tool",
        {"tool": tool_name, "call_id": tc.get("id"), "has_on_output_kw": _on_output is not None},
    )
    # #endregion
    try:
        result = await execute_tool(
            tool_name,
            args,
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_output=_on_output,
        )
    except Exception as exc:
        # #region agent log
        _dbg_llm(
            "B",
            "caller.py:_process_tool_call:execute_failed",
            type(exc).__name__,
            {"tool": tool_name, "call_id": tc.get("id"), "err": str(exc)[:200]},
        )
        # #endregion
        api_messages.append(
            LLMMessage(
                role="tool",
                tool_call_id=tc.get("id", ""),
                content=f"[工具执行失败] {type(exc).__name__}: {str(exc)[:200]}",
            )
        )
        return ""
    # #region agent log
    _dbg_llm(
        "B",
        "caller.py:_process_tool_call:after_execute",
        "ok",
        {"tool": tool_name, "call_id": tc.get("id"), "result_len": len(str(result)) if result else 0},
    )
    # #endregion
    logger.debug(f"[LLM] Tool result: {result[:100]}")

    # ── Vision injection for screenshot tools ──
    tool_content: str | list = str(result)
    if supports_vision and agent_id:
        try:
            from app.services.vision_inject import try_inject_screenshot_vision
            from app.config import get_settings

            settings = get_settings()
            ws_path = Path(settings.AGENT_DATA_DIR) / str(agent_id)
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = vision_content
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client about tool call result
    if on_tool_call:
        try:
            await on_tool_call(
                {
                    "name": tool_name,
                    "call_id": tc.get("id", ""),
                    "args": args,
                    "status": "done",
                    "result": result,
                    "reasoning_content": full_reasoning_content,
                }
            )
        except Exception as _e:
            logger.warning(f"[LLM] on_tool_call done error: {_e}")

    api_messages.append(
        LLMMessage(
            role="tool",
            tool_call_id=tc["id"],
            content=tool_content,
        )
    )
    if tool_result_cache is not None and cache_key:
        tool_result_cache[cache_key] = tool_content
    return ""


def _assistant_tool_call_ids(msg: LLMMessage) -> set[str]:
    """从 assistant 消息提取 tool_call id 集合。"""
    if msg.role != "assistant" or not msg.tool_calls:
        return set()
    return {tc.get("id") for tc in msg.tool_calls if tc.get("id")}


def _find_tool_calls_owner(api_messages: list, tool_index: int) -> tuple[LLMMessage, int] | None:
    """定位 tool 消息对应的 assistant（允许连续多条 tool 跟在同一个 assistant 后）。"""
    j = tool_index - 1
    while j >= 0 and api_messages[j].role == "tool":
        j -= 1
    if j >= 0 and api_messages[j].role == "assistant" and api_messages[j].tool_calls:
        return api_messages[j], j
    return None


def _sanitize_orphan_tool_messages(api_messages: list) -> int:
    """移除没有前置 assistant tool_calls 的孤立 tool 消息，避免 OpenAI 400。"""
    removed = 0
    i = 0
    while i < len(api_messages):
        msg = api_messages[i]
        if msg.role != "tool":
            i += 1
            continue
        owner_info = _find_tool_calls_owner(api_messages, i)
        tool_call_id = msg.tool_call_id or ""
        owner = owner_info[0] if owner_info else None
        if owner and tool_call_id in _assistant_tool_call_ids(owner):
            i += 1
            continue
        logger.warning(
            "[LLM] removed orphan tool message: tool_call_id={} owner={}",
            tool_call_id,
            owner is not None,
        )
        api_messages.pop(i)
        removed += 1
    return removed


def _repair_openai_tool_call_pairing(api_messages: list) -> None:
    """规范化 tool 消息序列：先删孤立 tool，再补齐缺失的 tool 结果。"""
    removed = _sanitize_orphan_tool_messages(api_messages)
    if removed:
        logger.warning("[LLM] sanitized {} orphan tool message(s)", removed)
    i = 0
    while i < len(api_messages):
        msg = api_messages[i]
        if msg.role != "assistant" or not msg.tool_calls:
            i += 1
            continue
        expected_ids = [tc.get("id") for tc in msg.tool_calls if tc.get("id")]
        if not expected_ids:
            i += 1
            continue
        found_ids: set[str] = set()
        j = i + 1
        while j < len(api_messages) and api_messages[j].role == "tool":
            tool_call_id = api_messages[j].tool_call_id
            if tool_call_id:
                found_ids.add(tool_call_id)
            j += 1
        missing_ids = [call_id for call_id in expected_ids if call_id not in found_ids]
        if missing_ids:
            # #region agent log
            _dbg_llm(
                "C",
                "caller.py:_repair_openai_tool_call_pairing",
                "repair_insert",
                {"missing_ids": missing_ids[:5], "expected_count": len(expected_ids)},
            )
            # #endregion
            insert_at = j
            for call_id in missing_ids:
                api_messages.insert(
                    insert_at,
                    LLMMessage(
                        role="tool",
                        content="[工具未返回结果，已跳过]",
                        tool_call_id=call_id,
                    ),
                )
                insert_at += 1
            logger.warning("[LLM] repaired missing tool results: {}", missing_ids)
        i = j if j > i else i + 1


# ═══════════════════════════════════════════════════════════════════════════════
# Core LLM Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_thinking=None,
    supports_vision=False,
    cancel_event: asyncio.Event | None = None,
    skip_tools: bool = False,
    parallel_tools_extra_readonly: set[str] | None = None,
    tool_warning_mode: str = "default",
    on_code_output=None,
) -> str:
    from app.services.agent_tools import AGENT_TOOLS, get_agent_tools_for_llm  # 延迟导入，避免循环依赖
    """Call LLM via unified client with function-calling tool loop.

    Args:
        cancel_event: 可选的取消事件，设置后中断工具循环和流式输出（用于 chat/stop）。
        parallel_tools_extra_readonly: 扩展只读工具名集合，合并到 _READONLY_TOOLS（LSP4J 路径使用）
        tool_warning_mode: "default" 保持现有行为，"lsp4j" 使用更主动的收敛引导
    """
    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id)
    if _token_limit_msg:
        return _token_limit_msg
    logger.info("[LLM] max_tool_rounds={} agent_id={}", _max_tool_rounds, agent_id)

    # Get user's name for personalized context
    _user_name = await _get_user_name(user_id)

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context

    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(
        agent_id, agent_name, role_description, current_user_name=_user_name
    )

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn; keep the runtime-level `finish`
    # tool available so every turn still has an explicit stop signal.
    if skip_tools:
        tools_for_llm = [FINISH_TOOL_DEFINITION]
    else:
        tools_for_llm = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    # Convert messages to LLMMessage format
    api_messages = [LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt)]
    for msg in messages:
        api_messages.append(
            LLMMessage(
                role=msg.get("role", "user"),
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
                reasoning_content=msg.get("reasoning_content"),
            )
        )
    # #region agent log
    _dbg_llm(
        "D",
        "caller.py:call_llm:history_loaded",
        "initial_messages",
        _audit_tool_pairing(api_messages),
    )
    # #endregion

    # Vision format conversion
    api_messages = _convert_messages_for_vision(api_messages, supports_vision)

    # Create the unified LLM client
    try:
        client = create_llm_client(
            provider=model.provider,
            api_key=get_model_api_key(model),
            model=model.model,
            base_url=model.base_url,
            timeout=_get_model_timeout(model),
        )
    except Exception as e:
        return f"[Error] Failed to create LLM client: {e}"

    max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))
    _accumulated_usage = TokenUsage()

    # Tool-calling loop
    # Cache identical tool calls within one request to avoid repeated search/read churn.
    tool_result_cache: dict[str, str | list] = {}
    for round_i in range(_max_tool_rounds):
        # 取消检查：若 cancel_event 已设置，立即中断工具循环
        if cancel_event and cancel_event.is_set():
            logger.info("[LLM] cancel_event 已设置，中断工具循环（round={}）", round_i)
            break
        # Dynamic tool-call limit warning（#119 修复：LSP4J 模式阈值前移）
        if tool_warning_mode == "lsp4j":
            _warn_first = 8  # LSP4J: 第 8 轮即警告收敛
        else:
            _warn_first = int(_max_tool_rounds * 0.8)  # 默认: 80% 时警告
        _warn_threshold_96 = _max_tool_rounds - 2
        if round_i == _warn_first:
            if tool_warning_mode == "lsp4j":
                api_messages.append(
                    LLMMessage(
                        role="user",
                        content=(
                            f"⏱️ 已消耗 {round_i} 轮工具调用（上限 {_max_tool_rounds}）。"
                            "请直接评估当前进度：\n"
                            "1. 已获取的信息是否足够？足够则立即给出最终回复\n"
                            "2. 多个独立的只读工具（search/read/list）可同时调用以减少轮数\n"
                            "3. 不要重复查询已获取的信息\n"
                            "4. 如任务接近完成，直接给出最终回复"
                        ),
                    )
                )
            else:
                api_messages.append(
                    LLMMessage(
                        role="user",
                        content=(
                            f"⚠️ 你已使用 {round_i}/{_max_tool_rounds} 轮工具调用。"
                            "如果当前任务尚未完成，请尽快使用 upsert_focus_item 保存进度，"
                            "并使用 set_trigger 设置续接触发器，在剩余轮次中做好收尾。"
                        ),
                    )
                )
        elif round_i == _warn_threshold_96:
            api_messages.append(
                LLMMessage(
                    role="user",
                    content="🚨 仅剩 2 轮工具调用。请立即使用 upsert_focus_item 保存进度并设置续接触发器。",
                )
            )

        # 上下文截断：每轮结束后估算 token 数，超出窗口时保留 system + 最近 3 轮
        # #121 修复：8000 → 12000，DeepSeek V4 支持 128K 上下文，充分利用长窗口避免截断
        MAX_CTX_TOKENS = 12000
        _total_est = 0
        for _m in api_messages:
            _c = _m.content or ""
            if isinstance(_c, str):
                # 中文: 1 字符 ≈ 0.3 token; 英文: 1 字符 ≈ 0.25 token
                _chinese_count = sum(1 for ch in _c if '一' <= ch <= '鿿')
                _total_est += int(_chinese_count * 0.3 + (len(_c) - _chinese_count) * 0.25)
        if _total_est > MAX_CTX_TOKENS and len(api_messages) > 8:
            # 保护 tool_call/tool_result 配对不被截断拆分——#72 修复
            # 在消息序列中，tool_call(role="assistant", tool_calls=[...]) 在前，
            # tool_result(role="tool", tool_call_id=...) 在后。
            # 截断时若尾块起始是 tool_result，需向前扩展包含对应的 tool_call。
            _tail_start = max(2, len(api_messages) - 6)
            while _tail_start > 2 and api_messages[_tail_start].role == "tool":
                _tail_start -= 1
            # 尾块若以 tool 开头，尽量把对应的 assistant(tool_calls) 一并保留进尾块
            while _tail_start < len(api_messages) and api_messages[_tail_start].role == "tool":
                owner_info = _find_tool_calls_owner(api_messages, _tail_start)
                tool_call_id = api_messages[_tail_start].tool_call_id or ""
                if owner_info:
                    owner, owner_idx = owner_info
                    if tool_call_id in _assistant_tool_call_ids(owner):
                        if owner_idx < _tail_start:
                            _tail_start = owner_idx
                        break
                if _tail_start > 2:
                    prev = api_messages[_tail_start - 1]
                    if prev.role == "assistant" and prev.tool_calls:
                        _tail_start -= 1
                        continue
                logger.warning(
                    "[LLM-CTX] 截断跳过孤立 tool: tool_call_id={} tail_start={}",
                    tool_call_id,
                    _tail_start,
                )
                _tail_start += 1
            tail = api_messages[_tail_start:]
            api_messages = api_messages[:2] + tail
            logger.info(
                "[LLM-CTX] 上下文截断: estimated_tokens={} messages={}",
                _total_est,
                len(api_messages),
            )

        _repair_openai_tool_call_pairing(api_messages)
        # #region agent log
        _audit = _audit_tool_pairing(api_messages)
        _dbg_llm(
            "C_E",
            "caller.py:call_llm:before_stream",
            "pairing_audit",
            {"round": round_i + 1, **_audit},
        )
        # #endregion

        try:
            # DeepSeek V4 思考模式参数（#118：按轮数分级开关）
            _thinking_kwargs = _get_thinking_kwargs(model, round_i)

            # ★ Timing: measure LLM stream round latency
            _llm_round_start = asyncio.get_event_loop().time()
            _msg_count = len(api_messages)
            _msg_total_chars = sum(len(m.content or "") if isinstance(m.content, str) else 0 for m in api_messages)
            logger.info(
                "[LLM-TIMING] Round {} START: model={} messages={} chars={} thinking={}",
                round_i + 1,
                model.model,
                _msg_count,
                _msg_total_chars,
                bool(_thinking_kwargs),
            )

            # Use streaming API for real-time responses
            async def _buffer_chunk(_text: str) -> None:
                # Stream deltas to callers (IDE LSP4J chat/answer, web WS chunks).
                # Final assembled text still comes from response / finish_call.
                if on_chunk and _text:
                    await on_chunk(_text)

            response = await client.stream(
                messages=api_messages,
                tools=tools_for_llm if tools_for_llm else None,
                temperature=model.temperature,
                max_tokens=max_tokens,
                on_chunk=_buffer_chunk,
                on_tool_delta=on_tool_delta,
                on_thinking=on_thinking,
                cancel_event=cancel_event,
                **_thinking_kwargs,
            )
            _llm_round_elapsed = asyncio.get_event_loop().time() - _llm_round_start
            logger.info(
                "[LLM-TIMING] Round {} END: elapsed={:.1f}s tools={} content_len={}",
                round_i + 1,
                _llm_round_elapsed,
                len(response.tool_calls or []),
                len(response.content or ""),
            )
        except LLMError as e:
            # #region agent log
            _dbg_llm(
                "E",
                "caller.py:call_llm:LLMError",
                "http_error",
                {
                    "round": round_i + 1,
                    "err": str(e)[:300],
                    "insufficient_tool": "insufficient tool" in str(e).lower(),
                    **_audit_tool_pairing(api_messages),
                },
            )
            # #endregion
            logger.error(
                f"[LLM] LLMError: provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}"
            )
            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            await client.close()
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            await client.close()
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        # Track tokens for this round
        _accumulated_usage.add(_usage_from_response_or_estimate(response, api_messages))

        # Plain assistant text with no tool calls: auto-finish (no extra reminder round).
        # The model intentionally returned text without invoking finish() — that's
        # semantically equivalent to a finish response (#93 修复).
        if not response.tool_calls:
            if response.content:
                if agent_id and _accumulated_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _accumulated_usage)
                await client.close()
                return response.content
            # Empty text + no tool calls is abnormal — fall through to reminder
            api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
            continue

        # Execute tool calls
        logger.info(f"[LLM] Round {round_i + 1}: {len(response.tool_calls)} tool call(s)")
        sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
        if retry_instruction:
            api_messages.append(LLMMessage(role="user", content=retry_instruction))
            continue

        finish_call = find_finish_call(sanitized_tool_calls)
        if finish_call:
            if finish_call.valid:
                if agent_id and _accumulated_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _accumulated_usage)
                await client.close()
                return finish_call.content

            api_messages.append(
                LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=sanitized_tool_calls,
                    reasoning_content=response.reasoning_content,
                )
            )
            api_messages.append(
                LLMMessage(
                    role="tool",
                    content=finish_call.error or "`finish` was invalid.",
                    tool_call_id=finish_call.call_id,
                )
            )
            continue

        # Add assistant message with tool calls
        api_messages.append(
            LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )

        full_reasoning_content = response.reasoning_content or ""

        # ─── Parallel tool execution (conservative: read-only only) ────────
        _READONLY_TOOLS = frozenset(
            {
                "read_file",
                "list_files",
                "search_in_files",
                "jina_search",
                "web_search",
                "jina_read",
                "read_document",
                "get_working_directory",
            }
        ) | (parallel_tools_extra_readonly or frozenset())

        tool_calls = sanitized_tool_calls or []
        readonly_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name") in _READONLY_TOOLS]
        write_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name") not in _READONLY_TOOLS]

        async def _exec_tool(tc):
            return await _process_tool_call(
                tc=tc,
                api_messages=api_messages,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                supports_vision=supports_vision,
                on_tool_call=on_tool_call,
                on_code_output=on_code_output,
                full_reasoning_content=full_reasoning_content,
                allowed_tool_names=allowed_tool_names,
                tool_result_cache=tool_result_cache,
            )

        # Execute read-only tools in parallel
        if readonly_calls:
            if cancel_event and cancel_event.is_set():
                _repair_openai_tool_call_pairing(api_messages)
                break
            # _process_tool_call 已负责把 tool 结果写入 api_messages，此处勿重复 append
            await asyncio.gather(*[_exec_tool(tc) for tc in readonly_calls], return_exceptions=True)

        # 按文件路径分组——同文件串行，不同文件并行（#89 修复）
        if cancel_event and cancel_event.is_set():
            _repair_openai_tool_call_pairing(api_messages)
            break
        file_groups: dict[str, list[dict]] = {}
        for tc in write_calls:
            fp = tc.get("parameters", {}).get("filePath") or tc.get("parameters", {}).get("file_path", "")
            file_groups.setdefault(fp, []).append(tc)

        async def _run_write_group(tools: list[dict]) -> list[tuple[dict, str | None]]:
            """串行执行同一文件路径下的写工具，返回 (tc, error_or_None) 列表。"""
            results: list[tuple[dict, str | None]] = []
            for t in tools:
                try:
                    err = await _process_tool_call(
                        tc=t,
                        api_messages=api_messages,
                        agent_id=agent_id,
                        user_id=user_id,
                        session_id=session_id,
                        supports_vision=supports_vision,
                        on_tool_call=on_tool_call,
                        full_reasoning_content=full_reasoning_content,
                        allowed_tool_names=allowed_tool_names,
                        tool_result_cache=tool_result_cache,
                    )
                    results.append((t, err))
                except Exception as exc:
                    logger.error(
                        "[LLM] 写工具执行失败: tool={} call_id={} err={}",
                        (t.get("function") or {}).get("name"),
                        t.get("id"),
                        exc,
                    )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            content=f"[工具执行失败] {type(exc).__name__}: {str(exc)[:200]}",
                            tool_call_id=t.get("id", ""),
                        )
                    )
                    results.append((t, str(exc)))
            return results

        if file_groups:
            group_results = await asyncio.gather(
                *(_run_write_group(group) for group in file_groups.values()),
                return_exceptions=True,
            )
            for grp in group_results:
                if isinstance(grp, Exception):
                    logger.error("[LLM] write group 执行异常: {}", grp)
                    continue
                # _process_tool_call 已在内部写入 tool 消息；此处仅记录 group 级异常
                for tc, tool_error in grp:
                    if tool_error:
                        logger.warning(
                            "[LLM] write tool returned error string (already appended): tool={} call_id={}",
                            (tc.get("function") or {}).get("name"),
                            tc.get("id"),
                        )

    # Record tokens even on "too many rounds" exit
    if agent_id and _accumulated_usage.total_tokens > 0:
        await record_token_usage(agent_id, _accumulated_usage)
    await client.close()
    logger.warning("[LLM] tool rounds exhausted: max={} agent_id={}", _max_tool_rounds, agent_id)
    return format_tool_rounds_limit_reply(_max_tool_rounds)


async def call_llm_with_failover(
    primary_model,
    fallback_model,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_tool_call=None,
    on_tool_delta=None,
    supports_vision=False,
    on_failover=None,
    cancel_event: asyncio.Event | None = None,
    skip_tools: bool = False,
    parallel_tools_extra_readonly: set[str] | None = None,
    tool_warning_mode: str = "default",
    on_code_output=None,
) -> str:
    """Call LLM with automatic failover support.

    Args:
        cancel_event: 可选的取消事件，透传至 call_llm。
    """
    guard = FailoverGuard()

    # Config-level fallback: if no primary, use fallback directly
    if primary_model is None and fallback_model is not None:
        logger.info("[Failover] Primary model not configured, using fallback directly")
        primary_model = fallback_model
        fallback_model = None

    if primary_model is None:
        return "⚠️ 未配置 LLM 模型"

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_tool_call(data: dict):
        if data.get("status") == "done":
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=supports_vision,
        cancel_event=cancel_event,
        skip_tools=skip_tools,
        parallel_tools_extra_readonly=parallel_tools_extra_readonly,
        tool_warning_mode=tool_warning_mode,
        on_code_output=on_code_output,
    )

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        # 区分正常回复和真正的非重试错误
        is_error = (
            primary_result.startswith("[LLM Error]")
            or primary_result.startswith("[LLM call error]")
            or primary_result.startswith("[Error]")
        )
        if not is_error:
            # 正常回复，不需要 failover，不打 WARNING
            return primary_result
        logger.warning(f"[Failover] Canceled: Primary model returned a non-retryable error: {primary_result[:150]}")
        return primary_result

    # Check guard conditions
    if not guard.can_failover():
        if guard.tool_executed:
            logger.warning("[Failover] Blocked: side-effecting tool already executed")
        elif guard.streaming_started:
            logger.warning("[Failover] Blocked: streaming already started")
        elif guard.failover_done:
            logger.warning("[Failover] Blocked: failover already done once")
        return primary_result

    # No fallback available
    if fallback_model is None:
        logger.warning("[Failover] No fallback model available")
        return primary_result

    # Runtime failover: retry with fallback model
    logger.info(f"[Failover] Retrying with fallback model: {fallback_model.provider}/{fallback_model.model}")

    if on_failover:
        try:
            await on_failover(f"Switched to fallback model: {fallback_model.model}")
        except Exception:
            pass

    guard.mark_failover_done()

    # Call fallback with fresh callbacks
    fallback_guard = FailoverGuard()
    fallback_guard.mark_failover_done()

    async def _fallback_on_chunk(text: str):
        fallback_guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _fallback_on_tool_call(data: dict):
        if data.get("status") == "done":
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=getattr(fallback_model, "supports_vision", False),
        cancel_event=cancel_event,
        skip_tools=skip_tools,
        parallel_tools_extra_readonly=parallel_tools_extra_readonly,
        tool_warning_mode=tool_warning_mode,
        on_code_output=on_code_output,
    )

    # Combine error messages if fallback also failed
    if is_retryable_error(fallback_result) or fallback_result.startswith("⚠️") or fallback_result.startswith("[Error]"):
        return f"⚠️ 调用模型出错: Primary: {primary_result[:80]} | Fallback: {fallback_result[:80]}"

    return fallback_result


# ═══════════════════════════════════════════════════════════════════════════════
# High-level Agent Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    supports_vision: bool = False,
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.core.permissions import is_agent_expired

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    # Load primary model
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        primary_model = model_result.scalar_one_or_none()

    # Load fallback model
    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()

    # Config-level fallback: primary missing -> use fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None
        logger.warning(f"[call_agent_llm] Primary model unavailable, using fallback: {primary_model.model}")

    if not primary_model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

    # Build conversation messages
    messages: list[dict] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    # Use unified call_llm_with_failover
    try:
        reply = await call_llm_with_failover(
            primary_model=primary_model,
            fallback_model=fallback_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            supports_vision=supports_vision or getattr(primary_model, "supports_vision", False),
        )
        return reply
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[call_agent_llm] Unexpected error: {error_msg}")
        return f"⚠️ 调用模型出错: {error_msg[:150]}"


async def call_agent_llm_with_tools(
    db: AsyncSession,
    agent_id: uuid.UUID,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int = 50,
    session_id: str = "",
) -> str:
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # Load agent and models
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ Agent not found"

    # Load models
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        primary_model = model_result.scalar_one_or_none()

    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()

    # Config-level fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None

    if not primary_model:
        return f"⚠️ {agent.name} has no LLM model configured"

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    # Load tools
    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        tool_executed = False
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=get_model_api_key(model),
                model=model.model,
                base_url=model.base_url,
                timeout=_get_model_timeout(model),
            )

            max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))

            # DeepSeek V4 思考模式参数
            _thinking_kwargs = _get_thinking_kwargs(model)

            # Tool-calling loop
            api_messages = list(messages)
            for round_i in range(max_rounds):
                try:
                    response = await client.complete(
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                        **_thinking_kwargs,
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client.close()
                    if agent_id and _accumulated_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _accumulated_usage)
                    raise

                # Track tokens for this round
                _accumulated_usage.add(_usage_from_response_or_estimate(response, api_messages))

                if not response.tool_calls:
                    if response.content:
                        if agent_id and _accumulated_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _accumulated_usage)
                        await client.close()
                        return response.content, True, tool_executed
                    api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                    continue

                # Execute tool calls
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue

                finish_call = find_finish_call(sanitized_tool_calls)
                if finish_call:
                    if finish_call.valid:
                        if agent_id and _accumulated_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _accumulated_usage)
                        await client.close()
                        return finish_call.content, True, tool_executed
                    api_messages.append(
                        LLMMessage(
                            role="assistant",
                            content=response.content or None,
                            tool_calls=sanitized_tool_calls,
                            reasoning_content=response.reasoning_content,
                        )
                    )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=finish_call.call_id,
                            content=finish_call.error or "`finish` was invalid.",
                        )
                    )
                    continue

                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )

                for tc in sanitized_tool_calls or []:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError:
                        args = {}

                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(
                            f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={agent_id}"
                        )
                        result = _tool_not_enabled_message(tool_name)
                    else:
                        result = await execute_tool(
                            tool_name,
                            args,
                            agent_id=agent_id,
                            user_id=agent.creator_id,
                            session_id=session_id,
                        )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tc["id"],
                            content=str(result),
                        )
                    )

            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            await client.close()
            logger.warning("[LLM] tool rounds exhausted: max={} agent_id={}", max_rounds, agent_id)
            return format_tool_rounds_limit_reply(max_rounds), False, tool_executed

        except Exception as e:
            if agent_id and _accumulated_usage.total_tokens > 0:
                await record_token_usage(agent_id, _accumulated_usage)
            return f"[Error] {e}", False, tool_executed

    # Try primary model
    reply, success, primary_tool_executed = await _try_model(primary_model)
    if success:
        return reply

    # Primary failed - check if retryable
    error_type = classify_error(Exception(reply))
    if error_type == FailoverErrorType.NON_RETRYABLE or not fallback_model:
        return reply

    if primary_tool_executed:
        logger.warning("[call_agent_llm_with_tools] Blocked fallback: side-effecting tool already executed")
        return reply

    # Try fallback model
    logger.info(f"[call_agent_llm_with_tools] Retrying with fallback: {fallback_model.model}")
    reply2, success2, _fallback_tool_executed = await _try_model(fallback_model)
    if success2:
        return reply2

    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"


__all__ = [
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "FailoverGuard",
    "is_retryable_error",
]
