"""LSP Base Protocol 解析器。

通义灵码使用 LSP4J 框架，消息格式遵循 LSP Base Protocol：
    Content-Length: {UTF-8 字节数}\\r\\n\\r\\n{JSON-RPC 消息}

关键约束：
- Content-Length 是 UTF-8 字节长度，不是字符数
- header 和 body 之间用 \\r\\n\\r\\n 分隔
- 整条消息（header + body）作为一个 WebSocket 帧发送
- 必须按字节操作，不能按字符操作（中文等多字节字符会出错）
- WebSocket 接收的是文本帧，需要先编码为 bytes 再按字节解析
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

# 匹配 Content-Length header 行（split 后不含 \r\n 尾部）
_CONTENT_LENGTH_RE = re.compile(rb"^Content-Length:\s*(\d+)")

# 缓冲区最大字节数（10MB），防止恶意客户端耗尽内存
_MAX_BUFFER_SIZE = 10 * 1024 * 1024

# 单条消息 Content-Length 上限（5MB），超过则丢弃该消息
_MAX_CONTENT_LENGTH = 5 * 1024 * 1024


class ParseError:
    """JSON 解析失败标记类，供 JSONRPCRouter 识别并返回 -32700 错误。"""

    def __init__(self, message: str = "Parse error") -> None:
        self.message = message


class LSPBaseProtocolParser:
    """LSP Base Protocol 消息解析器，处理字节级分帧。

    支持粘包场景：多条消息可能在同一个 WebSocket 帧中到达，
    内部维护字节缓冲区确保完整解析。
    """

    def __init__(self) -> None:
        self._buffer_bytes: bytes = b""

    def read_message(self, data: str | bytes) -> list[dict[str, Any]]:
        """解析 LSP Base Protocol 消息，返回解析出的 JSON-RPC 消息列表。

        Args:
            data: WebSocket 收到的原始数据（str 或 bytes）

        Returns:
            解析出的 JSON-RPC 消息列表，可能为空（数据不完整时缓冲等待）
        """
        # 统一转为 bytes 处理
        if isinstance(data, str):
            incoming = data.encode("utf-8")
        else:
            incoming = data

        # 缓冲区溢出保护：超出限制则清空缓冲区并丢弃当前数据
        if len(self._buffer_bytes) + len(incoming) > _MAX_BUFFER_SIZE:
            logger.error(
                "LSP4J: 缓冲区溢出（当前 {} + 新增 {} > 上限 {}），清空缓冲区",
                len(self._buffer_bytes),
                len(incoming),
                _MAX_BUFFER_SIZE,
            )
            self._buffer_bytes = b""
            return []

        self._buffer_bytes += incoming

        messages: list[dict[str, Any]] = []

        while True:
            msg = self._try_parse_one()
            if msg is None:
                break
            messages.append(msg)

        return messages

    def _try_parse_one(self) -> dict[str, Any] | None:
        """尝试从缓冲区解析一条完整的 LSP 消息。

        LSP Base Protocol 格式：
            Content-Length: {length}\\r\\n\\r\\n{body}

        返回 None 表示缓冲区中没有完整消息。
        """
        # 查找 header 与 body 的分隔符 \\r\\n\\r\\n
        header_end = self._buffer_bytes.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        header_block = self._buffer_bytes[:header_end]
        content_length = self._parse_content_length(header_block)
        if content_length is None:
            # 无效 header，丢弃直到分隔符后继续
            logger.warning("LSP4J: 无法解析 Content-Length，丢弃无效 header")
            self._buffer_bytes = self._buffer_bytes[header_end + 4 :]
            return None

        # Content-Length 超上限，丢弃该消息防止内存耗尽
        if content_length > _MAX_CONTENT_LENGTH:
            logger.warning(
                "LSP4J: Content-Length {} 超过上限 {}，丢弃该消息",
                content_length,
                _MAX_CONTENT_LENGTH,
            )
            body_start = header_end + 4
            body_end = body_start + content_length
            # 如果缓冲区已有完整 body 则跳过，否则清空等待
            if len(self._buffer_bytes) >= body_end:
                self._buffer_bytes = self._buffer_bytes[body_end:]
            else:
                self._buffer_bytes = self._buffer_bytes[header_end + 4 :]
            return None

        body_start = header_end + 4
        body_end = body_start + content_length

        # body 不完整，等待更多数据
        if len(self._buffer_bytes) < body_end:
            return None

        body_bytes = self._buffer_bytes[body_start:body_end]
        # 从缓冲区移除已消费的数据
        self._buffer_bytes = self._buffer_bytes[body_end:]

        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error("LSP4J: JSON 解析失败: {}", e)
            return ParseError(str(e))

    @staticmethod
    def format_message(message: dict[str, Any]) -> str:
        """将 JSON-RPC 消息格式化为 LSP Base Protocol 文本帧。

        注意：Content-Length 必须是 UTF-8 编码后的字节长度，
        不能用 len(json_str)，因为中文等多字节字符长度不同。

        Args:
            message: JSON-RPC 消息字典

        Returns:
            格式化后的 LSP Base Protocol 字符串
        """
        body = json.dumps(message, ensure_ascii=False)
        body_bytes = body.encode("utf-8")
        content_length = len(body_bytes)
        return f"Content-Length: {content_length}\r\n\r\n{body}"

    @staticmethod
    def _parse_content_length(header_block: bytes) -> int | None:
        """从 header 块中解析 Content-Length 值。

        Args:
            header_block: header 部分的原始字节（不含 \\r\\n\\r\\n 分隔符）

        Returns:
            Content-Length 整数值，解析失败返回 None
        """
        for line in header_block.split(b"\r\n"):
            m = _CONTENT_LENGTH_RE.match(line)
            if m:
                return int(m.group(1))
        return None
