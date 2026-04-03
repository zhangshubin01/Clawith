# clawith_mcp/server.py
"""薄启动器 — 委托给插件中的 stdio 服务器。

所有 MCP 逻辑位于:
    backend/app/plugins/clawith_mcp/server_stdio.py

用法:
    CLAWITH_URL=http://localhost:8008 CLAWITH_API_KEY=cw-xxx python server.py

Claude Code ~/.claude/settings.json:
    {
      "mcpServers": {
        "clawith": {
          "command": "/opt/homebrew/bin/node",
          "args": ["/path/to/clawith_mcp/server.py"],
          "env": {
            "CLAWITH_URL": "http://localhost:8008",
            "CLAWITH_API_KEY": "cw-your-key"
          }
        }
      }
    }
"""
import sys
import os

# 将 backend/ 加入 sys.path，使 `app.*` 导入可用
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "backend"))

from app.plugins.clawith_mcp.server_stdio import main  # noqa: E402
import asyncio

asyncio.run(main())
