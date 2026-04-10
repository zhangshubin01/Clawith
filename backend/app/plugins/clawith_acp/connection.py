"""ndJson stream connection over WebSocket for ACP.

This module handles the ndJson (newline-delimited JSON) streaming I/O with the IDE client.
"""
import asyncio
import json
from typing import Any, Dict, Optional
from uuid import UUID
from .types import RequestError


class AgentSideConnection:
    """ACP Agent-side connection wrapping ndJson stream over WebSocket."""
    
    def __init__(self, websocket):
        self._websocket = websocket
        self._closed = False
        self._closed_future = asyncio.Future[None]()
    
    async def read_message(self) -> Optional[Dict[str, Any]]:
        """Read one line from stdin, parse as JSON."""
        text = await self._websocket.receive_text()
        if not text:
            return None
        text = text.strip()
        if not text:
            return None
        return json.loads(text)
    
    async def send_message(self, data: Dict[str, Any]) -> None:
        """Send one JSON message as a line."""
        json_str = json.dumps(data, ensure_ascii=False)
        await self._websocket.send_text(json_str + '\n')
    
    def close(self) -> None:
        """Close the connection."""
        if not self._closed:
            self._closed = True
            self._closed_future.set_result(None)
    
    @property
    def closed(self) -> asyncio.Future[None]:
        return self._closed_future
    
    # === File system RPC ===
    async def read_text_file(self, *, path: str, sessionId: UUID) -> Dict[str, Any]:
        """RPC: ask IDE to read a text file."""
        await self.send_message({
            "jsonrpc": "2.0",
            "method": "fs/readTextFile",
            "params": {"path": path, "sessionId": str(sessionId)},
        })
        # Response will be received in the receive loop by call_id
        # This method is only used when issuing synchronous RPC calls
        # for permission request approval flow in file editing
        return {"path": path, "sessionId": sessionId}
    
    async def write_text_file(self, *, path: str, content: str, sessionId: UUID) -> None:
        """RPC: ask IDE to write a text file."""
        await self.send_message({
            "jsonrpc": "2.0",
            "method": "fs/writeTextFile",
            "params": {
                "path": path,
                "content": content,
                "sessionId": str(sessionId),
            },
        })
