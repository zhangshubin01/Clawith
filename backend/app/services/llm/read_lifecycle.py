"""Read lifecycle — stale/superseded read 替换为 CCR marker。

port headroom read_lifecycle.py；与 Tier1 exclude 互补：exclude 保护新注入 read，
lifecycle 回收历史中已失效的 read 字节。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger

from .compression_config import (
    MUTATING_TOOL_NAMES,
    READ_TOOL_NAMES,
    ReadLifecycleConfig,
    read_lifecycle_config_from_settings,
)
from .ccr_store import ccr_marker, store_entry
from .context_compressor import _est_tokens_str


class ReadState(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    SUPERSEDED = "superseded"


@dataclass
class FileOperation:
    msg_index: int
    tool_call_id: str
    tool_name: str
    file_path: str
    operation: str
    content_size: int = 0
    read_offset: int | None = None
    read_limit: int | None = None


@dataclass
class ReadClassification:
    msg_index: int
    tool_call_id: str
    file_path: str
    tool_name: str
    state: ReadState
    content_size: int


@dataclass
class ReadLifecycleResult:
    messages: list[Any]
    reads_total: int = 0
    reads_stale: int = 0
    reads_superseded: int = 0
    reads_fresh: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


def _msg_role(msg: Any) -> str:
    return getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else "")


def _msg_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _msg_tool_call_id(msg: Any) -> str:
    if isinstance(msg, dict):
        return msg.get("tool_call_id") or ""
    return getattr(msg, "tool_call_id", None) or ""


def _set_content(msg: Any, content: str) -> Any:
    if isinstance(msg, dict):
        return {**msg, "content": content}
    msg.content = content
    return msg


def _iter_tool_calls(msg: Any):
    tcs = getattr(msg, "tool_calls", None) if not isinstance(msg, dict) else msg.get("tool_calls")
    for tc in tcs or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        yield tc.get("id", ""), fn.get("name", ""), fn.get("arguments", "{}")


class ReadLifecycleManager:
    def __init__(self, config: ReadLifecycleConfig | None = None):
        self.config = config or read_lifecycle_config_from_settings()

    @staticmethod
    def _read_covers(later: FileOperation, earlier: FileOperation) -> bool:
        if later.read_offset is None and later.read_limit is None:
            return True
        if earlier.read_offset is None and earlier.read_limit is None:
            return False
        later_start = later.read_offset or 0
        later_end = later_start + (later.read_limit or 2000)
        earlier_start = earlier.read_offset or 0
        earlier_end = earlier_start + (earlier.read_limit or 2000)
        return later_start <= earlier_start and later_end >= earlier_end

    def _build_tool_metadata(self, messages: list[Any]) -> dict[str, tuple[str, str | None, int | None, int | None]]:
        metadata: dict[str, tuple[str, str | None, int | None, int | None]] = {}
        for msg in messages:
            if _msg_role(msg) != "assistant":
                continue
            for tc_id, name, raw_args in _iter_tool_calls(msg):
                if not tc_id or not name:
                    continue
                file_path = None
                offset = None
                limit = None
                try:
                    args = json.loads(raw_args or "{}")
                    file_path = args.get("file_path") or args.get("path")
                    offset = args.get("offset")
                    limit = args.get("limit")
                except (json.JSONDecodeError, TypeError):
                    pass
                metadata[tc_id] = (name, file_path, offset, limit)
        return metadata

    def _find_tool_call_msg_index(self, messages: list[Any], tool_call_id: str) -> int | None:
        for i, msg in enumerate(messages):
            if _msg_role(msg) != "assistant":
                continue
            for tc_id, _name, _args in _iter_tool_calls(msg):
                if tc_id == tool_call_id:
                    return i
        return None

    def _build_file_operation_index(
        self,
        messages: list[Any],
        tool_metadata: dict[str, tuple[str, str | None, int | None, int | None]],
    ) -> dict[str, list[FileOperation]]:
        file_ops: dict[str, list[FileOperation]] = defaultdict(list)
        for tc_id, (name, file_path, offset, limit) in tool_metadata.items():
            if not file_path:
                continue
            if name in READ_TOOL_NAMES:
                operation = "read"
            elif name in MUTATING_TOOL_NAMES:
                operation = "edit"
            else:
                continue
            msg_idx = self._find_tool_call_msg_index(messages, tc_id)
            if msg_idx is None:
                continue
            content_size = 0
            for msg in messages:
                if _msg_role(msg) == "tool" and _msg_tool_call_id(msg) == tc_id:
                    c = _msg_content(msg)
                    if isinstance(c, str):
                        content_size = len(c.encode("utf-8"))
                    break
            file_ops[file_path].append(
                FileOperation(
                    msg_index=msg_idx,
                    tool_call_id=tc_id,
                    tool_name=name,
                    file_path=file_path,
                    operation=operation,
                    content_size=content_size,
                    read_offset=offset if operation == "read" else None,
                    read_limit=limit if operation == "read" else None,
                )
            )
        return dict(file_ops)

    def _classify_reads(self, file_ops: dict[str, list[FileOperation]]) -> list[ReadClassification]:
        classifications: list[ReadClassification] = []
        for _path, ops in file_ops.items():
            reads = [op for op in ops if op.operation == "read"]
            edits = [op for op in ops if op.operation == "edit"]
            if not reads:
                continue
            for read_op in reads:
                is_stale = self.config.compress_stale and any(
                    e.msg_index > read_op.msg_index for e in edits
                )
                is_superseded = self.config.compress_superseded and any(
                    r.msg_index > read_op.msg_index and self._read_covers(r, read_op) for r in reads
                )
                if is_stale:
                    state = ReadState.STALE
                elif is_superseded:
                    state = ReadState.SUPERSEDED
                else:
                    state = ReadState.FRESH
                classifications.append(
                    ReadClassification(
                        msg_index=read_op.msg_index,
                        tool_call_id=read_op.tool_call_id,
                        file_path=read_op.file_path,
                        tool_name=read_op.tool_name,
                        state=state,
                        content_size=read_op.content_size,
                    )
                )
        return classifications

    async def apply_async(
        self,
        messages: list[Any],
        *,
        session_id: str,
        agent_id,
        ctx_path: str,
        frozen_message_count: int = 0,
        tools_available: bool = True,
        model_name: str = "",
    ) -> ReadLifecycleResult:
        if not self.config.enabled or not messages:
            return ReadLifecycleResult(messages=messages)

        tool_metadata = self._build_tool_metadata(messages)
        file_ops = self._build_file_operation_index(messages, tool_metadata)
        classifications = self._classify_reads(file_ops)
        if not classifications:
            return ReadLifecycleResult(messages=messages)

        if frozen_message_count > 0:
            for c in classifications:
                if c.msg_index < frozen_message_count and c.state != ReadState.FRESH:
                    c.state = ReadState.FRESH

        replacements = {c.tool_call_id: c for c in classifications if c.state != ReadState.FRESH}
        if not replacements:
            counts = {s: 0 for s in ReadState}
            for c in classifications:
                counts[c.state] += 1
            return ReadLifecycleResult(
                messages=messages,
                reads_total=len(classifications),
                reads_fresh=counts[ReadState.FRESH],
            )

        result: list[Any] = []
        bytes_before = 0
        bytes_after = 0
        counts = {s: 0 for s in ReadState}
        for c in classifications:
            counts[c.state] += 1

        for msg in messages:
            if _msg_role(msg) != "tool":
                result.append(msg)
                continue
            tc_id = _msg_tool_call_id(msg)
            classification = replacements.get(tc_id)
            content = _msg_content(msg)
            if not classification or not isinstance(content, str):
                result.append(msg)
                continue
            if "<!-- ccr:" in content or "<!-- ccr:retrieved -->" in content:
                result.append(msg)
                continue
            if len(content.encode("utf-8")) < self.config.min_size_bytes:
                result.append(msg)
                continue

            before_tok = _est_tokens_str(content, model_name)
            h = await store_entry(
                session_id=session_id,
                agent_id=agent_id,
                content=content,
                tool_name=classification.tool_name,
                path=ctx_path,
                original_tokens=before_tok,
                compressed_tokens=max(1, before_tok // 10),
            )
            if not h or not tools_available:
                result.append(msg)
                continue

            file_display = classification.file_path or "unknown"
            if classification.state == ReadState.STALE:
                summary = (
                    f"[Read stale: `{file_display}` 在读取后被修改；请重新 read 获取最新内容]"
                )
            else:
                summary = (
                    f"[Read superseded: `{file_display}` 已被后续读取覆盖；需要时请重新 read]"
                )
            body = emit_guarded_from_parts(summary, ccr_marker(h), content, model_name, ctx_path=ctx_path)
            bytes_before += len(content.encode("utf-8"))
            bytes_after += len(body.encode("utf-8"))
            result.append(_set_content(msg, body))
            logger.info(
                "[CTX-READ-LC] state={} file={} path={} bytes {}→{}",
                classification.state.value, file_display, ctx_path, len(content), len(body),
            )

        return ReadLifecycleResult(
            messages=result,
            reads_total=len(classifications),
            reads_stale=counts[ReadState.STALE],
            reads_superseded=counts[ReadState.SUPERSEDED],
            reads_fresh=counts[ReadState.FRESH],
            bytes_before=bytes_before,
            bytes_after=bytes_after,
        )


def emit_guarded_from_parts(summary: str, hint: str, original: str, model_name: str = "", *, ctx_path: str = "") -> str:
    from .emit_guarded import emit_guarded
    return emit_guarded(summary, hint, original, model_name, ctx_path=ctx_path)
