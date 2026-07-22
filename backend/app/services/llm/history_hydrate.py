"""ACP 历史记录 hydration — 工具结果注入聊天上下文。

被 acp_session.py 导入，将工具调用结果注入历史消息流。
"""

from __future__ import annotations

from loguru import logger


async def hydrate_history_tool_results(
    messages: list[dict],
) -> list[dict]:
    """将工具调用结果注入聊天消息历史。

    ACP 路径下的历史 hydration：确保 LLM 看到的每条 tool_call
    都有对应的 tool_result 消息。
    """
    hydrated: list[dict] = []
    pending_tool_calls: dict[str, int] = {}  # tool_call_id → index

    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                call_id = tc.get("id", "")
                if call_id:
                    pending_tool_calls[call_id] = len(hydrated)
            hydrated.append(msg)
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            if tool_call_id in pending_tool_calls:
                hydrated.append(msg)
            else:
                logger.debug(
                    "[ACP-HYDRATE] 孤立 tool_result 跳过: call_id={}",
                    tool_call_id,
                )
        else:
            hydrated.append(msg)

    return hydrated
