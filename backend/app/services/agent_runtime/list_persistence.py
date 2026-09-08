"""Platform-derived list scope and legacy 清单.md parsing.

The numbered-list channel is now structured — ``agent_list_items`` plus the
``list/upsert/complete_list_item`` tools — so the free-text extraction path
(R1) and the open_items pointer mechanism are gone. This module keeps only the
two pieces the migration still needs: ``extract_workspace_project`` (the
platform-derived list scope) and ``parse_list_file`` (the legacy 清单.md
parser used by ``migrate_legacy_list_file`` for the one-time import).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
import uuid

from app.services.agent_runtime.state import JsonObject

LIST_FILE_PATH = "memory/清单.md"

# Model-visible numbering contract, injected once per Run into the system
# prompt. Stable wording: changes here are a model-visible contract change
# (backend/AGENTS.md).
LIST_NUMBERING_CONTRACT = (
    "\n\n# Numbered Lists\n\n"
    "清单（待办/候选/未决事项）必须通过 list_list_items / upsert_list_item / complete_list_item 工具声明与维护，"
    "不得只写在回复文本或手写 memory/清单.md。清单作用域（project）由平台派生，不得自行发明 list_id。\n\n"
    "- 编号即身份：每条目编号 = 工具分配的 sort_order（per-project 单调递增、永不重排）。向用户呈现清单时编号必须与 "
    "list_list_items 输出一致；呈现子集/过滤视图时保留原 sort_order 编号，绝不从 1 重新编号。\n"
    "- 解析优先级：用户以裸编号（如「先做 #92+#93」）引用清单时，按次序解析：①本对话注入的清单索引"
    "（= system 提示词动态尾注入的活跃项 title 索引，逐项 `{sort_order}. {title} ({key})` 原文）；②list_list_items 读出的当前清单。"
    "均以工具输出/清单索引的编号与条目原文为准，不得自行重排、猜测或补造候选。注入窗口有界（活跃项 N 项），"
    "窗口外编号引用（含 completed 项）不在注入索引里——此时用稳定编号做一次 list_list_items 确定性解析（查得准，非猜）；"
    "若查无该编号/key（如 completed 项已归档后仍被引用），如实说明该编号无对应未完成项并追问，绝不顺延到邻近编号。\n"
    "- 完成凭据：complete_list_item 以 key 为凭据；注入索引每项已带 key，直接用 key 完成；只有裸编号且无 key 时才 list_list_items 解析。\n"
    "- 兜底消歧：多份清单并存且无法唯一确定所指时，先复述候选条目（编号+标题，逐字引用）向用户确认后再动工；"
    "查无该编号/key 时如实说明并追问，绝不顺延到邻近编号。"
)

_ITEM_LINE = re.compile(r"^\s*(\d+)\s*[.、)）]\s*(.+?)\s*$")
_HEADER_LINE = re.compile(r"^## list:([0-9a-fA-F-]{36}) \| project: (.+?) \| 标题：(.+?) \| (.+?)\s*$")
_WORKSPACE_SEGMENT = re.compile(r"^workspace/([^/]+)/")


@dataclass(frozen=True, slots=True)
class ListItem:
    """One numbered entry of a legacy persisted list."""

    number: int
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class ListSection:
    """One ``## list:...`` section of ``memory/清单.md``."""

    list_id: uuid.UUID
    project: str | None
    title: str
    timestamp: str
    items: tuple[ListItem, ...]


@dataclass(frozen=True, slots=True)
class ListFile:
    """Parsed ``memory/清单.md``; foreign content is preserved verbatim."""

    blocks: tuple[str | ListSection, ...]

    @property
    def sections(self) -> tuple[ListSection, ...]:
        return tuple(block for block in self.blocks if isinstance(block, ListSection))


def _split_title_description(body: str) -> tuple[str, str] | None:
    if " — " not in body:
        return None
    title, _, description = body.partition(" — ")
    title = title.strip()
    description = description.strip()
    if not title or not description:
        return None
    return title, description


def extract_workspace_project(messages: Sequence[JsonObject]) -> str | None:
    """Most frequent ``workspace/<name>`` first segment across tool-call paths.

    Only paths carrying the explicit ``workspace/`` prefix participate (D1);
    ties break by first occurrence, which keeps the result deterministic.
    Returns None when no such path exists — the caller degrades to a
    session-level scope.
    """
    counts: Counter[str] = Counter()
    for message in messages:
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                continue
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                continue
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    raw_arguments = json.loads(raw_arguments)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not isinstance(raw_arguments, Mapping):
                continue
            path = raw_arguments.get("path")
            if not isinstance(path, str):
                continue
            normalized = path.replace("\\", "/").strip()
            match = _WORKSPACE_SEGMENT.match(normalized)
            if match is None:
                continue
            name = match.group(1).strip()
            if name:
                counts[name] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _parse_section_lines(lines: list[str]) -> ListSection | None:
    header = _HEADER_LINE.match(lines[0])
    if header is None:
        return None
    try:
        list_id = uuid.UUID(header.group(1))
    except ValueError:
        return None
    project_text = header.group(2).strip()
    items: list[ListItem] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _ITEM_LINE.match(line)
        if match is None:
            return None
        body = _split_title_description(match.group(2))
        if body is None:
            return None
        title, description = body
        items.append(ListItem(number=int(match.group(1)), title=title, description=description))
    return ListSection(
        list_id=list_id,
        project=None if project_text == "-" else project_text,
        title=header.group(3).strip(),
        timestamp=header.group(4).strip(),
        items=tuple(items),
    )


def parse_list_file(content: str) -> ListFile:
    """Split ``memory/清单.md`` into list sections and verbatim raw blocks.

    A ``## list:`` block that cannot be parsed completely (bad UUID, stray
    non-item line) is kept as raw text instead of being rewritten — foreign or
    hand-edited content is never dropped.
    """
    raw_lines: list[str] = []
    blocks: list[str | ListSection] = []
    section_lines: list[str] | None = None

    def flush_raw() -> None:
        if raw_lines:
            blocks.append("".join(raw_lines))
            raw_lines.clear()

    def flush_section() -> None:
        nonlocal section_lines
        if section_lines is None:
            return
        trailing: list[str] = []
        while section_lines and not section_lines[-1].strip():
            trailing.append(section_lines.pop())
        section = _parse_section_lines(section_lines)
        trailing.reverse()
        if section is None:
            raw_lines.extend([*section_lines, *trailing])
        else:
            flush_raw()
            blocks.append(section)
            raw_lines.extend(trailing)
        section_lines = None

    for line in content.splitlines(keepends=True):
        if _HEADER_LINE.match(line) is not None or (line.startswith("## list:") and section_lines is None):
            flush_section()
            section_lines = [line]
            continue
        if section_lines is not None:
            if not line.strip():
                section_lines.append(line)
                continue
            match = _ITEM_LINE.match(line)
            if match is not None and _split_title_description(match.group(2)) is not None:
                section_lines.append(line)
                continue
            flush_section()
            raw_lines.append(line)
            continue
        raw_lines.append(line)
    flush_section()
    flush_raw()
    return ListFile(blocks=tuple(blocks))


__all__ = [
    "LIST_FILE_PATH",
    "LIST_NUMBERING_CONTRACT",
    "ListItem",
    "ListFile",
    "ListSection",
    "extract_workspace_project",
    "parse_list_file",
]
