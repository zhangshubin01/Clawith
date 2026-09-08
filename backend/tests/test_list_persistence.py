"""List-scope derivation (project) and legacy 清单.md parser / numbering contract tests.

The free-text extraction handler (R1) and its helpers are gone — lists are now
structured via ``agent_list_items``. This file keeps the surviving pure pieces:
``extract_workspace_project`` (platform-derived scope), ``parse_list_file``
(legacy 清单.md parser for the one-time import), and the model-visible
``LIST_NUMBERING_CONTRACT``.
"""

from __future__ import annotations

import json

from app.services.agent_runtime.context_builder import RuntimeContextBuild
from app.services.agent_runtime.list_persistence import (
    LIST_NUMBERING_CONTRACT,
    ListSection,
    extract_workspace_project,
    parse_list_file,
)
from app.services.agent_runtime.model_step_service import _prompt_messages


def test_extract_workspace_project_most_frequent_segment() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "workspace/mydome1/src/A.kt"}),
                    },
                },
                {
                    "id": "c2",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({"path": "workspace/mydome1/src/B.kt"}),
                    },
                },
                {
                    "id": "c3",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "workspace/other/README.md"}),
                    },
                },
            ],
        },
    ]
    assert extract_workspace_project(messages) == "mydome1"


def test_extract_workspace_project_breaks_ties_by_first_seen() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "workspace/alpha/x"}),
                    },
                },
                {
                    "id": "c2",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "workspace/beta/y"}),
                    },
                },
            ],
        },
    ]
    assert extract_workspace_project(messages) == "alpha"


def test_extract_workspace_project_none_without_workspace_paths() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "memory/memory.md"}),
                    },
                },
            ],
        },
    ]
    assert extract_workspace_project(messages) is None
    assert extract_workspace_project([]) is None
    assert (
        extract_workspace_project(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "bad",
                            "function": {
                                "name": "write_file",
                                "arguments": "not json",
                            },
                        },
                    ],
                },
            ]
        )
        is None
    )


def test_parse_list_file_preserves_foreign_content_and_section() -> None:
    content = (
        "# 手写备忘\n"
        "这是 agent 自己写的内容。\n\n"
        "## list:00000000-0000-0000-0000-000000000001 | project: mydome1 | "
        "标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204\n"
        "2. 超大指数上限 — power()\n\n"
        "尾部注释\n"
    )
    parsed_file = parse_list_file(content)
    assert len(parsed_file.sections) == 1
    section = parsed_file.sections[0]
    assert isinstance(section, ListSection)
    assert str(section.list_id) == "00000000-0000-0000-0000-000000000001"
    assert section.project == "mydome1"
    assert section.title == "app 优化清单"
    assert [item.title for item in section.items] == ["输入精度截断", "超大指数上限"]
    # Foreign content around the section is preserved verbatim as raw blocks.
    assert parsed_file.blocks[0].startswith("# 手写备忘\n这是 agent 自己写的内容。")
    assert "尾部注释\n" in parsed_file.blocks[-1]


def test_parse_list_file_treats_broken_section_headers_as_raw() -> None:
    content = "## list:not-a-uuid | whatever\n1. A — 甲\n"
    parsed_file = parse_list_file(content)
    assert parsed_file.sections == ()
    assert parsed_file.blocks == (content,)


def test_prompt_messages_inject_the_numbering_contract_into_system() -> None:
    build = RuntimeContextBuild(
        session_context_snapshot={"version": 1, "summary": "shared"},
        current_run={"goal": "Answer"},
        related_run_summaries=(),
        pending_session_messages_snapshot=(),
        recent_session_messages_snapshot=(),
        thread_running_summary=None,
        recent_thread_messages=(),
        initial_input={"message_id": "s1"},
        resume_input=None,
        omitted_tool_exchanges=(),
        retry_model=False,
        blocked=False,
        requires_confirmation=False,
    )
    messages = _prompt_messages(
        static_prompt="Static",
        dynamic_prompt="Dynamic",
        build=build,
    )
    system = messages[0].content
    assert "编号即身份" in system
    assert "list_list_items" in system
    assert "sort_order" in system
    assert "由平台派生" in system
    # Stable byte prefix across turns: the contract is a constant appended once.
    again = _prompt_messages(static_prompt="Static", dynamic_prompt="Dynamic", build=build)
    assert again[0].content == system
    # Contract text lives with the parser it serves (single source of truth).
    assert "list_list_items" in LIST_NUMBERING_CONTRACT
