import asyncio
import json
import sys

sys.path.insert(0, "/tmp/tokenize")

import tiktoken

enc = tiktoken.get_encoding("cl100k_base")

AGENT_ID = "b1a73489-b808-4b3c-9335-838d29128ac4"


async def main() -> None:
    from app.services.agent_runtime.agent_tool_runtime import resolve_agent_tools

    resolved = await resolve_agent_tools(AGENT_ID, enabled_only=True)
    names = [t.get("function", {}).get("name") for t in resolved]
    s = json.dumps(resolved, ensure_ascii=False)
    print("TOOL_COUNT", len(names))
    print("SCHEMA_CHARS", len(s))
    print("SCHEMA_TOKENS", len(enc.encode(s)))
    # per-tool top sizes
    sizes = []
    for t in resolved:
        ts = json.dumps(t, ensure_ascii=False)
        sizes.append((t.get("function", {}).get("name"), len(ts), len(enc.encode(ts))))
    sizes.sort(key=lambda x: -x[2])
    for name, chars, toks in sizes[:10]:
        print(f"TOP_TOOL\t{name}\tchars={chars}\ttokens={toks}")


asyncio.run(main())
