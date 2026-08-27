"""One-off read-only context profile for one agent/thread (run inside backend container).

Usage (inside container):
  PYTHONPATH=/tmp/tokenize python /tmp/measure_context_profile.py

Prints per-section char/token (cl100k_base) counts for:
  - static system prompt (build_agent_context)
  - stable dynamic block prompt
  - turn-local dynamic prompt (current time)
  - thread history (latest langgraph checkpoint messages, by role)
"""
import asyncio
import sys

sys.path.insert(0, "/tmp/tokenize")

import tiktoken

enc = tiktoken.get_encoding("cl100k_base")

AGENT_ID = "b1a73489-b808-4b3c-9335-838d29128ac4"
AGENT_NAME = "Android 工程师06"
THREAD_ID = "fc321e8a-d04b-4dd5-b98e-fdee007ce155"
TOOLS = [
    "android_compile", "duckduckgo_search", "exa_search", "execute_code",
    "execute_code_e2b", "google_search", "jina_read", "jina_search",
    "mcp_add_resource", "mcp_cancel_watch", "mcp_code_expand",
    "mcp_code_outline", "mcp_code_search", "mcp_find", "mcp_forget",
    "mcp_glob", "mcp_grep", "mcp_health", "mcp_list", "mcp_list_watches",
    "mcp_read", "mcp_recall", "mcp_remember", "mcp_search", "query_directory",
    "read_webpage", "send_channel_file", "send_channel_message",
    "send_file_to_agent", "send_message_to_agent", "send_platform_message",
    "tavily_search", "upload_image", "web_search",
]


def toks(text: str) -> int:
    return len(enc.encode(text or ""))


def section(label: str, text: str) -> None:
    print(f"{label}\tchars={len(text)}\ttokens={toks(text)}")


async def main() -> None:
    from app.services.agent_context import build_agent_context

    static_prompt, stable_dynamic_prompt, turn_local_dynamic_prompt = (
        await build_agent_context(
            uuid.UUID(AGENT_ID),
            AGENT_NAME,
            allowed_tool_names=TOOLS,
        )
    )
    section("STATIC_PROMPT", static_prompt)
    section("STABLE_DYNAMIC_PROMPT", stable_dynamic_prompt)
    section("TURN_LOCAL_DYNAMIC_PROMPT", turn_local_dynamic_prompt)
    section("STATIC+DYNAMIC", static_prompt + "\n" + stable_dynamic_prompt + "\n" + turn_local_dynamic_prompt)

    # Thread history from the latest langgraph checkpoint.
    from app.services.agent_runtime.checkpointer import (
        checkpoint_database_url,
        checkpoint_serializer,
        _to_psycopg_url,
    )

    dsn = _to_psycopg_url(checkpoint_database_url())
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(dsn, serde=checkpoint_serializer()) as saver:
        await saver.setup()
        result = await saver.aget_tuple(
            {"configurable": {"thread_id": THREAD_ID}}
        )

    if result is None or result.checkpoint is None:
        print("NO_CHECKPOINT_FOUND")
        return
    messages = result.checkpoint.get("channel_values", {}).get("messages", [])
    print(f"TOTAL_THREAD_MESSAGES\t{len(messages)}")
    by_role: dict[str, tuple[int, int]] = {}
    total_toks = 0
    for msg in messages:
        role = str(msg.type) if hasattr(msg, "type") else str(getattr(msg, "role", "?"))
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p)
                for p in content
            )
        t = toks(content)
        total_toks += t
        c, n = by_role.get(role, (0, 0))
        by_role[role] = (c + len(content), n + 1)
        if role in {"tool", "assistant"}:
            by_role.setdefault(f"{role}_tokens", (0, 0))
            ct, nt = by_role[f"{role}_tokens"]
            by_role[f"{role}_tokens"] = (ct + t, nt + 1)
    print(f"THREAD_TOTAL_TOKENS\t{total_toks}")
    for role, (chars, count) in sorted(by_role.items()):
        if role.endswith("_tokens"):
            continue
        print(f"THREAD_ROLE\t{role}\tcount={count}\tchars={chars}")

    # Top 10 largest individual tool results.
    tool_msgs = [
        (i, m) for i, m in enumerate(messages)
        if getattr(m, "type", None) == "tool"
    ]
    tool_msgs.sort(
        key=lambda item: toks(
            " ".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p)
                for p in (getattr(item[1], "content", "") or "")
                if isinstance(getattr(item[1], "content", "") or "", list)
            ) if isinstance(getattr(item[1], "content", ""), list)
            else str(getattr(item[1], "content", "") or "")
        ),
        reverse=True,
    )
    print("TOP_TOOL_RESULTS")
    for i, m in tool_msgs[:10]:
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) if isinstance(p, dict) else str(p)
                for p in content
            )
        name = getattr(m, "name", "?")
        print(f"  idx={i}\tname={name}\tchars={len(content)}\ttokens={toks(content)}")
        print(f"    head={content[:120]!r}")


if __name__ == "__main__":
    import uuid

    asyncio.run(main())
