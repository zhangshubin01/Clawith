"""LSP4J 插件端到端测试脚本。

模拟通义灵码 IDE 插件的 WebSocket 连接行为，
验证 Clawith LSP4J 后端的完整功能链路。

使用方法：
1. 在 Clawith Web UI (http://localhost:3008) 中获取 API Key
2. 修改下方 API_KEY 和 AGENT_ID
3. 运行: cd backend && .venv/bin/python3 ../test_lsp4j.py

前置条件：
- Clawith 后端已启动（端口 8008）
- PostgreSQL 已启动
- API Key 有有效的智能体
"""

import asyncio
import json
import sys
import time

# ──────────────────────────────────────────
# 配置（请修改为你自己的值）
# ──────────────────────────────────────────
API_KEY = "cw-YOUR_API_KEY_HERE"  # 在 Clawith Web UI 设置页获取
AGENT_ID = "4db60c64-ba74-4387-95f4-c2b152e57ac7"  # Clawith全栈开发工程师
WS_URL = f"ws://localhost:8008/api/plugins/clawith-lsp4j/ws?agent_id={AGENT_ID}&token={API_KEY}"


def format_lsp_message(msg: dict) -> str:
    """格式化 LSP Base Protocol 消息"""
    body = json.dumps(msg, ensure_ascii=False)
    body_bytes = body.encode("utf-8")
    return f"Content-Length: {len(body_bytes)}\r\n\r\n{body}"


async def test_lsp4j():
    """LSP4J 端到端测试"""
    try:
        import websockets
    except ImportError:
        print("错误: 需要安装 websockets 库")
        print("  运行: pip install websockets")
        sys.exit(1)

    print(f"连接: {WS_URL.replace(API_KEY, 'cw-***')}")
    
    try:
        async with websockets.connect(WS_URL) as ws:
            print("WebSocket 连接成功!")
            
            # ── 测试 1: initialize ──
            print("\n=== 测试 1: initialize ===")
            init_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": 12345,
                    "rootUri": "file:///tmp/test-project",
                    "capabilities": {},
                }
            }
            await ws.send(format_lsp_message(init_msg))
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"响应: {resp[:200]}")
            
            # ── 测试 2: chat/ask ──
            print("\n=== 测试 2: chat/ask ===")
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            ask_msg = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "chat/ask",
                "params": {
                    "requestId": "req-001",
                    "sessionId": session_id,
                    "questionText": "你好，请简单介绍你自己",
                    "stream": True,
                }
            }
            await ws.send(format_lsp_message(ask_msg))
            print("已发送 chat/ask，等待流式响应...")
            
            # 收集所有流式响应
            full_answer = ""
            messages_received = 0
            timeout = 60
            start = time.time()
            
            while time.time() - start < timeout:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    print("等待响应超时")
                    break
                
                # 解析 LSP Base Protocol
                if "Content-Length:" in raw:
                    parts = raw.split("\r\n\r\n", 1)
                    if len(parts) > 1:
                        body = parts[1]
                    else:
                        body = raw
                else:
                    body = raw
                
                try:
                    msg = json.loads(body)
                except json.JSONDecodeError:
                    # 可能有多条消息粘在一起
                    continue
                
                method = msg.get("method", "")
                params = msg.get("params", {})
                
                if method == "chat/think":
                    step = params.get("step", "")
                    text = params.get("text", "")
                    if step == "start" and text:
                        print(f"  [思考] {text}")
                    elif step == "done":
                        print(f"  [思考完成]")
                
                elif method == "chat/answer":
                    text = params.get("text", "")
                    full_answer += text
                    print(f"  [流式] {text}", end="", flush=True)
                
                elif method == "chat/finish":
                    reason = params.get("reason", "")
                    full_ans = params.get("fullAnswer", "")
                    print(f"\n  [完成] reason={reason}, answer_len={len(full_ans)}")
                    break
                
                messages_received += 1
            
            print(f"\n收到 {messages_received} 条消息")
            print(f"完整回答: {full_answer[:200]}...")
            
            # ── 测试 3: shutdown + exit ──
            print("\n=== 测试 3: shutdown + exit ===")
            shutdown_msg = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "shutdown",
                "params": {},
            }
            await ws.send(format_lsp_message(shutdown_msg))
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            print(f"shutdown 响应: {resp[:100]}")
            
            exit_msg = {
                "jsonrpc": "2.0",
                "method": "exit",
                "params": {},
            }
            await ws.send(format_lsp_message(exit_msg))
            print("exit 通知已发送")
            
            print("\n=== 所有测试完成! ===")
    
    except Exception as e:
        print(f"测试失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    if API_KEY == "cw-YOUR_API_KEY_HERE":
        print("请先修改脚本中的 API_KEY！")
        print("获取方式：")
        print("  1. 打开 http://localhost:3008")
        print("  2. 登录后进入设置页面")
        print("  3. 生成或复制 API Key（cw- 前缀）")
        sys.exit(1)
    
    asyncio.run(test_lsp4j())
