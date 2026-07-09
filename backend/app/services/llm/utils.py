"""Shared LLM provider configuration and utilities.

Centralizes provider URLs and provider-specific API parameters
so they don't need to be duplicated across websocket.py, scheduler.py,
task_executor.py, agent_tools.py, and feishu.py.

This module also exports the unified LLM client classes from client.py
for convenient access.
"""

from app.core.security import decrypt_data
from app.config import get_settings
from app.models.llm import LLMModel
from loguru import logger

# Re-export all client classes and functions from client.py
from .client import (
    AnthropicClient,
    GeminiClient,
    LLMClient,
    LLMError,
    LLMMessage,
    LLMResponse,
    LLMStreamChunk,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
    PROVIDER_ALIASES,
    PROVIDER_REGISTRY,
    ProviderSpec,
    PROVIDER_URLS,
    TOOL_CHOICE_PROVIDERS,
    chat_complete,
    chat_stream,
    create_llm_client,
    get_max_tokens,
    get_provider_manifest,
    get_provider_base_url,
    get_provider_spec,
    normalize_provider,
)

# Keep ANTHROPIC_API_PROVIDERS for backward compatibility
ANTHROPIC_API_PROVIDERS = {"anthropic"}

# Keep the original PROVIDER_URLS reference (already exported from client)


def get_model_api_key(model: LLMModel) -> str:
    """Decrypt the model's API key, with backward compatibility for plaintext keys.

    优先读取运行时密钥 _runtime_api_key（BYOK 场景），不入库、不写日志。
    """
    # 优先读取运行时密钥（IDE BYOK 场景，用户自带密钥不入库）
    runtime_key = getattr(model, "_runtime_api_key", None)
    if runtime_key:
        return runtime_key

    raw = model.api_key_encrypted or ""
    if not raw:
        return ""
    try:
        settings = get_settings()
        return decrypt_data(raw, settings.SECRET_KEY)
    except ValueError:
        return raw


def get_tool_params(provider: str) -> dict:
    """Return provider-specific tool calling parameters.

    Qwen and OpenAI support `tool_choice` and `parallel_tool_calls`.
    Anthropic uses a different tool calling format, so we skip these params.

    Note: This function is kept for backward compatibility.
    The new client classes handle this internally.
    """
    if provider in TOOL_CHOICE_PROVIDERS:
        return {
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
    return {}


_HISTORY_MIN_CHARS = 512
_HISTORY_MAX_CHARS = 16384


def _history_tool_char_limit(tool_name: str, ctx_window: int = 100_000) -> int:
    """跨 prompt 加载单条 tool 结果的字符上限（对齐运行时 token budget）。"""
    from .tool_trim import _TOOL_HARD_CEIL_CHARS, _tool_token_budget

    settings = get_settings()
    cap = int(getattr(settings, "CTX_HISTORY_TOOL_MAX_CHARS", 0) or 0)
    if cap > 0:
        return max(_HISTORY_MIN_CHARS, min(cap, _TOOL_HARD_CEIL_CHARS))
    budget_chars = _tool_token_budget(tool_name or "", ctx_window) * 4
    return max(_HISTORY_MIN_CHARS, min(budget_chars, _TOOL_HARD_CEIL_CHARS))


from .truncate_caps import never_worse


def emit_guarded_history(excerpt: str, marker: str, original: str, *, ctx_path: str = "history") -> str:
    """历史 hydrate 用：marker + excerpt，走 never_worse。"""
    if marker in excerpt:
        return excerpt
    return never_worse(excerpt, marker, original, ctx_path=ctx_path)


def format_history_tool_content(
    tool_name: str,
    raw: str,
    *,
    ctx_window: int = 100_000,
    path: str = "history",
    ccr_hash: str | None = None,
) -> tuple[str, str]:
    """DB 历史 tool 结果格式化：budget 内 verbatim，超长 head/tail + 省略通知。"""
    from .tool_trim import _TOOL_TYPE_ROUTE, _hard_head_tail, _list_head_tail

    text = raw if isinstance(raw, str) else str(raw)
    before = len(text)
    if before == 0:
        return text, "empty"

    if ccr_hash:
        from .ccr_store import ccr_marker

        excerpt = text
        if before > _history_tool_char_limit(tool_name, ctx_window):
            excerpt = _hard_head_tail(text, max_chars=min(before, 1200))
        body = emit_guarded_history(excerpt, ccr_marker(ccr_hash), text, ctx_path=path)
        logger.info(
            "[CTX-HISTORY] path={} tool={} chars={}→{} strategy=ccr_hash",
            path, tool_name, before, len(body),
        )
        return body, "ccr_hash"

    limit = _history_tool_char_limit(tool_name, ctx_window)
    if before <= limit:
        if before > 500:
            logger.info(
                "[CTX-HISTORY] path={} tool={} chars={}→{} strategy=verbatim",
                path, tool_name, before, before,
            )
        return text, "verbatim"

    route = _TOOL_TYPE_ROUTE.get(tool_name or "", "")
    if route == "list":
        trimmed = _list_head_tail(text)
        strategy = "list_head_tail"
    else:
        trimmed = _hard_head_tail(text, max_chars=limit)
        strategy = "hard_head_tail"

    omitted = before - len(trimmed)
    if omitted > 0 and "已省略" not in trimmed:
        trimmed = trimmed + f"\n[... {omitted} 字符已省略；完整内容见 CCR retrieve]"
    logger.info(
        "[CTX-HISTORY] path={} tool={} chars={}→{} strategy={}",
        path, tool_name, before, len(trimmed), strategy,
    )
    return trimmed, strategy


def convert_chat_messages_to_llm_format(
    messages,
    *,
    ctx_window: int = 100_000,
    path: str = "history",
) -> list[dict]:
    """Convert ChatMessage DB records to LLM-compatible message dicts.

    Properly handles ``tool_call`` role records by splitting them into an
    assistant message (with ``tool_calls`` array) followed by a tool result
    message — the format required by OpenAI / Anthropic / Gemini APIs.

    Without this conversion, ``tool_call`` records would be passed with
    ``role="tool_call"`` (an invalid role), causing LLM API errors or
    silently lost context.

    Args:
        messages: Iterable of ChatMessage ORM objects (with ``role``,
                  ``content``, ``id``, and optional ``thinking`` attributes).

    Returns:
        List of dicts suitable for passing to ``call_llm()`` or
        ``call_llm_with_failover()``.
    """
    import json as _json

    result: list[dict] = []
    for msg in messages:
        if msg.role == "tool_call":
            try:
                tc_data = _json.loads(msg.content)
                tc_name = tc_data.get("name", "unknown")
                tc_args = tc_data.get("args", {})
                tc_result = tc_data.get("result", "")
                tc_ccr_hash = tc_data.get("ccr_hash") or ""
                tc_id = f"call_{msg.id}"  # synthetic tool_call_id

                # Assistant message with tool_calls array
                asst_msg: dict = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": tc_name,
                            "arguments": _json.dumps(tc_args, ensure_ascii=False),
                        },
                    }],
                }
                if tc_data.get("reasoning_content"):
                    asst_msg["reasoning_content"] = tc_data["reasoning_content"]
                result.append(asst_msg)

                # Tool result message
                try:
                    from app.services.vision_inject import sanitize_history_tool_result
                    sanitized_result = sanitize_history_tool_result(str(tc_result))
                except ImportError:
                    sanitized_result = str(tc_result)
                tool_content, _ = format_history_tool_content(
                    tc_name,
                    sanitized_result,
                    ctx_window=ctx_window,
                    path=path,
                    ccr_hash=str(tc_ccr_hash).strip() or None,
                )
                result.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_content,
                })
            except Exception:
                continue  # Skip malformed tool_call records
        else:
            entry: dict = {"role": msg.role, "content": msg.content}
            if hasattr(msg, "thinking") and msg.thinking:
                entry["thinking"] = msg.thinking
            result.append(entry)

    return result


def truncate_messages_with_pair_integrity(messages: list[dict], ctx_size: int) -> list[dict]:
    """Truncate message list to ctx_size while preserving assistant+tool pair integrity.

    When context window truncation breaks an assistant(tool_calls) + tool_result
    group, the resulting orphaned messages cause "No tool call found for function
    call output" errors from the LLM API. This function ensures that:

    1. No tool_result message exists without its preceding assistant(tool_calls)
    2. No assistant(tool_calls) message exists without all its tool_result messages
    """
    truncated = messages[-ctx_size:]
    if not truncated:
        return truncated

    # Pass 1: Remove leading tool messages (they have no matching assistant before them)
    while truncated and truncated[0].get("role") == "tool":
        truncated.pop(0)

    if not truncated:
        return truncated

    # Pass 2: Scan for broken pairs within the truncated list.
    assistant_call_ids: set[str] = set()
    tool_call_ids: set[str] = set()

    for msg in truncated:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id:
                    assistant_call_ids.add(tc_id)
        elif msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id:
                tool_call_ids.add(tc_id)

    orphaned_tools = tool_call_ids - assistant_call_ids
    orphaned_assistant_calls = assistant_call_ids - tool_call_ids

    if not orphaned_tools and not orphaned_assistant_calls:
        logger.debug("[CTX-TRUNC] ctx_size={} messages={} no orphans", ctx_size, len(truncated))
        return truncated

    # Remove orphaned tool messages and assistant tool_calls entries
    sanitized = []
    for msg in truncated:
        if msg.get("role") == "tool":
            if msg.get("tool_call_id", "") in orphaned_tools:
                continue  # Remove orphaned tool result
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            filtered_tcs = [
                tc for tc in msg["tool_calls"]
                if tc.get("id", "") not in orphaned_assistant_calls
            ]
            if filtered_tcs:
                new_msg = dict(msg)
                new_msg["tool_calls"] = filtered_tcs
                sanitized.append(new_msg)
            elif msg.get("content"):
                new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                sanitized.append(new_msg)
            # else: drop the entire assistant message (no content, no valid tool_calls)
        else:
            sanitized.append(msg)

    logger.debug("[CTX-TRUNC] ctx_size={} messages={}→{} removed_tools={} removed_calls={}",
        ctx_size, len(truncated), len(sanitized), len(orphaned_tools), len(orphaned_assistant_calls))
    return sanitized


# Keep backward compatibility aliases
__all__ = [
    # Original utilities
    "get_tool_params",
    "get_provider_base_url",
    "get_max_tokens",
    "get_model_api_key",
    # Message conversion utilities
    "convert_chat_messages_to_llm_format",
    "format_history_tool_content",
    "truncate_messages_with_pair_integrity",
    # New client classes
    "LLMClient",
    "OpenAICompatibleClient",
    "OpenAIResponsesClient",
    "GeminiClient",
    "AnthropicClient",
    "LLMMessage",
    "LLMResponse",
    "LLMStreamChunk",
    "LLMError",
    # New functions
    "create_llm_client",
    "chat_complete",
    "chat_stream",
    # Constants
    "ProviderSpec",
    "PROVIDER_ALIASES",
    "PROVIDER_REGISTRY",
    "PROVIDER_URLS",
    "ANTHROPIC_API_PROVIDERS",
    "TOOL_CHOICE_PROVIDERS",
    # Registry helpers
    "normalize_provider",
    "get_provider_spec",
    "get_provider_manifest",
]
