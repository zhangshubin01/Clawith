"""CCR 相关稳定上下文块 — system appendix 与 retrieve_context 工具定义单一真源。

固定 JSON key 顺序，便于 prompt cache 与 invariant 测试。
"""

from __future__ import annotations

import json

CCR_SYSTEM_APPENDIX = """

## 上下文归档 (CCR)
工具结果若包含 `<!-- ccr:<hash> -->`，说明展示内容可能是压缩版，完整原文已归档。
需要遗漏细节时调用 retrieve_context(hash="<hash>")；需要分页时使用 offset/limit（0-based 行号）。
retrieve_context 返回内容带 `<!-- ccr:retrieved -->`，应视为原文，不要再次摘要。
"""

RETRIEVE_CONTEXT_TOOL_NAME = "retrieve_context"

RETRIEVE_CONTEXT_TOOL_DEFINITION: dict = {
    "type": "function",
    "function": {
        "name": RETRIEVE_CONTEXT_TOOL_NAME,
        "description": (
            "Retrieve the full original content that was archived when a tool result was "
            "compressed. When you see a marker like `<!-- ccr:<hash> -->` in a tool result, "
            "the shown text is a compressed summary and the complete content is archived. "
            "Call this tool with that hash to get the verbatim full content (e.g. the complete "
            "file list or search output). Only call it when you actually need the omitted detail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hash": {
                    "type": "string",
                    "description": "The content hash from a `<!-- ccr:<hash> -->` marker.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional 0-based line offset for paged retrieval.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional number of lines to return when offset is provided.",
                },
            },
            "required": ["hash"],
        },
    },
}


def stable_json_dumps(obj: dict) -> str:
    """固定 key 顺序的 JSON 序列化，供 invariant 测试比对 bytes。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_ccr_system_appendix() -> str:
    """返回 CCR system 附录文本（稳定 bytes）。"""
    return CCR_SYSTEM_APPENDIX


def get_retrieve_context_tool_definition() -> dict:
    """返回 retrieve_context OpenAI tool schema 深拷贝稳定结构。"""
    return json.loads(stable_json_dumps(RETRIEVE_CONTEXT_TOOL_DEFINITION))
