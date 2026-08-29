import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.storage import StorageEntry


def _context_patches(
    *,
    soul: str = "",
    memory: str = "",
    skills: str = "",
    reflections: str = "",
    user_profile: str = "",
    inject_reflections: bool | None = None,
):
    agent_id_holder: dict[str, uuid.UUID] = {}

    async def fake_read_file(key, _max_chars=3000):
        agent_id = agent_id_holder["agent_id"]
        if key == f"{agent_id}/soul.md":
            return soul
        if key in {f"{agent_id}/memory/memory.md", f"{agent_id}/memory.md"}:
            return memory
        if key == f"{agent_id}/memory/reflections.md":
            return reflections
        if key == f"{agent_id}/memory/user_profile.md":
            return user_profile
        return ""

    inject_patch = (
        patch(
            "app.services.agent_context._load_reflections_injection_enabled",
            new_callable=AsyncMock,
            return_value=bool(inject_reflections),
        )
        if inject_reflections is not None
        else None
    )

    return agent_id_holder, (
        patch("app.services.agent_context._read_file_safe", side_effect=fake_read_file),
        patch(
            "app.services.agent_context._load_skills_index",
            new_callable=AsyncMock,
            return_value=skills,
        ),
        patch(
            "app.services.agent_context._load_relationships_from_db",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "app.services.timezone_utils.get_agent_timezone",
            new_callable=AsyncMock,
            return_value="UTC",
        ),
        inject_patch,
    )


@pytest.mark.asyncio
async def test_memory_maintenance_policy_follows_read_write_capabilities():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        without_write, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait"},
        )
        with_write, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait", "read_file", "write_file"},
        )

    assert "Memory Maintenance" not in without_write
    assert "Memory Maintenance" in with_write
    # 义务要点（语义断言，非逐字文案）
    assert "memory/memory.md" in with_write
    # D（2026-08-29 废弃 INDEX）：义务句已移除——平台代码零消费方。
    assert "memory/MEMORY_INDEX.md" not in with_write
    assert "reading the file first" in with_write
    assert "Never blind-overwrite" in with_write
    assert "do not write anything" in with_write
    assert "temporary task progress" in with_write
    assert "explicit instruction overrides" in with_write
    assert "never blocks delivering" in with_write
    # 收尾固化判定（D6）：交卷前判一次，无则跳过
    assert "before returning the final answer" in with_write.lower()
    assert "decide once" in with_write.lower()
    assert "do nothing" in with_write.lower()


@pytest.mark.asyncio
async def test_base_prompt_starts_with_name_and_soul_and_never_injects_self_role():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches(
        soul="# Soul\nBe precise and preserve evidence.",
        memory="# Memory\nThe release owner is Alice.",
    )
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        static, stable_dynamic, _unstable_dynamic = await build_agent_context(
            agent_id,
            "TestAgent",
            "THIS ROLE MUST NOT ENTER THE MODEL",
            allowed_tool_names={"wait"},
        )

    assert static.startswith("# Identity\n\nYou are TestAgent, a digital employee in Clawith.")
    assert "<soul>\nBe precise and preserve evidence.\n</soul>" in static
    assert static.index("<soul>") < static.index("# Clawith Environment")
    assert "THIS ROLE MUST NOT ENTER THE MODEL" not in f"{static}\n{stable_dynamic}\n{_unstable_dynamic}"
    assert "# Memory" in static
    assert "The release owner is Alice." not in static
    assert "The release owner is Alice." in stable_dynamic
    assert "## Role" not in static
    assert "call `finish`" not in static
    assert "return the exact final answer as normal Assistant content" in static


@pytest.mark.asyncio
async def test_focus_mechanism_is_constant_but_tool_policy_follows_effective_tools():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        without_tools, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait"},
        )
        with_focus_tools, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "list_focus_items",
                "upsert_focus_item",
                "complete_focus_item",
            },
        )

    assert "## Focus" in without_tools
    assert "Focus is your structured persistent working state" in without_tools
    assert "list_focus_items" not in without_tools
    assert "list_focus_items" in with_focus_tools
    assert "Do not read or write `focus.md`" in with_focus_tools


@pytest.mark.asyncio
async def test_skill_catalog_requires_read_file_and_prompt_has_no_hardcoded_channel_manuals():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches(
        skills="| Risk Review | Check release risks | skills/risk/SKILL.md |",
    )
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        without_loader, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait"},
        )
        with_loader, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait", "read_file", "list_files"},
        )

    assert "Risk Review" not in without_loader
    assert "# Available Skills" in with_loader
    assert "skills/risk/SKILL.md" in with_loader
    assert "MCP Import Rules" not in with_loader
    assert "atlassian_jira_search_issues" not in with_loader
    assert "Pre-installed Feishu Tools" not in with_loader


@pytest.mark.asyncio
async def test_lowercase_skill_entry_advertises_the_actual_readable_path(monkeypatch):
    from app.services import agent_context

    agent_id = uuid.uuid4()
    prefix = f"{agent_id}/skills"
    folder_key = f"{prefix}/risk-review"
    lowercase_key = f"{folder_key}/skill.md"

    class _Storage:
        async def exists(self, key):
            return key in {prefix, folder_key, lowercase_key}

        async def is_dir(self, key):
            return key in {prefix, folder_key}

        async def list_dir(self, key):
            assert key == prefix
            return [
                StorageEntry(
                    name="risk-review",
                    key=folder_key,
                    is_dir=True,
                )
            ]

        async def read_text(self, key, **_kwargs):
            assert key == lowercase_key
            return "---\nname: Risk Review\ndescription: Check release risks\n---\n"

    monkeypatch.setattr(agent_context, "get_storage_backend", lambda: _Storage())

    catalog = await agent_context._load_skills_index(agent_id)

    assert "skills/risk-review/skill.md" in catalog
    assert "skills/risk-review/SKILL.md" not in catalog


@pytest.mark.asyncio
async def test_directory_and_human_send_policies_only_name_enabled_tools():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        static, stable_dynamic, _unstable_dynamic = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "query_directory",
                "send_platform_message",
                "send_channel_message",
            },
        )

    prompt = f"{static}\n{stable_dynamic}\n{_unstable_dynamic}"
    assert "send_feishu_message" not in prompt
    assert "query_directory" in prompt
    assert "send_platform_message" in prompt
    assert "send_channel_message" in prompt


@pytest.mark.asyncio
async def test_experience_policy_is_short_and_only_names_enabled_operations():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        read_only, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "search_experience",
                "read_experience",
            },
        )
        with_draft, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={
                "wait",
                "search_experience",
                "read_experience",
                "propose_experience_draft",
            },
        )

    assert "search_experience" in read_only
    assert "read_experience" in read_only
    assert "propose_experience_draft" not in read_only
    assert "现有标签" not in read_only
    assert "propose_experience_draft" in with_draft


@pytest.mark.asyncio
async def test_memory_maintenance_routes_this_run_lessons_to_reflections():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches()
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3]:
        with_write, _stable, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait", "read_file", "write_file"},
        )

    # 常驻段把「本次 run 学到的教训」路由到 reflections 四节之一。
    assert "memory/reflections.md" in with_write
    assert "Open Questions" in with_write
    assert "Hypotheses & Experiments" in with_write
    assert "Insights & Discoveries" in with_write
    assert "Next Cycle Seeds" in with_write
    # 分类判据（短半衰期）与格式约束（append + 不新建节）入断言。
    assert "shorter half-life" in with_write
    assert "do not create new" in with_write
    # 跳过句覆盖两条分支：既无耐用信息也无教训才不写。
    assert "If neither durable information nor lessons" in with_write
    # D（2026-08-29 废弃 INDEX）：义务句已移除——平台代码零消费方。
    assert "memory/MEMORY_INDEX.md" not in with_write


def test_extract_reflections_injection_keeps_only_conclusions():
    from app.services.agent_context import _extract_reflections_injection

    content = "\n".join(
        [
            "# Reflections Journal",
            "",
            "## Open Questions",
            "- 旧待办问题 A",
            "- 旧待办问题 B",
            "",
            "## Hypotheses & Experiments",
            "- ✅ 已验证：UDF 适合小应用",
            "- ❌ 已证伪：手写 BackStack 优于状态切换",
            "- 🔄 进行中：导航模式待需求明确",
            "",
            "## Insights & Discoveries",
            "- 发现一：collectAsState 而非 observeAsState",
            "- 发现二：Navigation3 demo 可作参考",
            "",
            "## Next Cycle Seeds",
            "- 下周期探索 X",
        ]
    )

    extracted = _extract_reflections_injection(content)

    # Insights 全节进，Insights 优先于 Hypotheses。
    assert extracted.index("发现一") < extracted.index("✅")
    assert "发现一" in extracted
    assert "发现二" in extracted
    assert "✅ 已验证" in extracted
    assert "❌ 已证伪" in extracted
    # 待办与进行中内容一律不进。
    assert "旧待办问题" not in extracted
    assert "🔄" not in extracted
    assert "下周期探索" not in extracted


def test_extract_reflections_injection_empty_without_sections():
    from app.services.agent_context import _extract_reflections_injection

    assert _extract_reflections_injection("") == ""
    assert _extract_reflections_injection("no section headers\njust prose\n") == ""


def test_extract_reflections_injection_truncates_at_cap():
    from app.services.agent_context import _extract_reflections_injection

    content = (
        "## Insights & Discoveries\n"
        + "- " + "很长的内容" * 500 + "\n"
    )
    extracted = _extract_reflections_injection(content, max_chars=100)
    assert len(extracted) <= 100 + len("\n...(truncated)")
    assert extracted.endswith("\n...(truncated)")


@pytest.mark.asyncio
async def test_load_reflections_injection_enabled_switch_semantics():
    from app.services.agent_context import _load_reflections_injection_enabled

    agent_id = uuid.uuid4()

    class _Setting:
        def __init__(self, value):
            self.value = value

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalar_one_or_none(self):
            return self._rows[0] if self._rows else None

    class _Db:
        def __init__(self, rows):
            self._rows = rows
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return _Result(self._rows)

    # enabled: true → True，且查询键名正确。
    db_on = _Db([_Setting({"enabled": True})])
    assert await _load_reflections_injection_enabled(db_on, agent_id) is True
    compiled = db_on.statements[0].compile()
    assert list(compiled.params.values())[0] == (
        f"context_inject_reflections_{agent_id}"
    )

    # 无行 → False（缺省关闭）。
    db_empty = _Db([])
    assert await _load_reflections_injection_enabled(db_empty, agent_id) is False

    # 非 dict value → False（畸形行安全降级）。
    db_bad = _Db([_Setting("not-a-dict")])
    assert await _load_reflections_injection_enabled(db_bad, agent_id) is False

    # enabled: false → False。
    db_off = _Db([_Setting({"enabled": False})])
    assert await _load_reflections_injection_enabled(db_off, agent_id) is False


@pytest.mark.asyncio
async def test_reflections_injection_off_switch_injects_nothing():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches(
        reflections="## Insights & Discoveries\n- 不应出现\n",
        user_profile="用户档案内容\n",
        inject_reflections=False,
    )
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        _static, stable_dynamic, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait", "read_file", "write_file"},
        )

    assert "Reflections Snapshot" not in stable_dynamic
    assert "User Profile" not in stable_dynamic
    assert "不应出现" not in stable_dynamic
    assert "用户档案内容" not in stable_dynamic


@pytest.mark.asyncio
async def test_reflections_injection_on_injects_filtered_sections_and_profile():
    from app.services.agent_context import build_agent_context

    agent_id = uuid.uuid4()
    holder, patches = _context_patches(
        memory="memory 内容\n",
        reflections="\n".join(
            [
                "## Open Questions",
                "- 待办不应出现",
                "## Insights & Discoveries",
                "- 已沉淀的洞察",
                "## Next Cycle Seeds",
                "- 种子不应出现",
            ]
        ),
        user_profile="用户档案内容\n",
        inject_reflections=True,
    )
    holder["agent_id"] = agent_id

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        _static, stable_dynamic, _unstable = await build_agent_context(
            agent_id,
            "TestAgent",
            allowed_tool_names={"wait", "read_file", "write_file"},
        )

    assert "## Reflections Snapshot" in stable_dynamic
    assert "<reflections_context>" in stable_dynamic
    assert "已沉淀的洞察" in stable_dynamic
    assert "待办不应出现" not in stable_dynamic
    assert "种子不应出现" not in stable_dynamic
    # 低信任声明随注入块出现。
    assert "hypotheses with evidence, not facts" in stable_dynamic
    # user_profile 独立注入。
    assert "## User Profile" in stable_dynamic
    assert "用户档案内容" in stable_dynamic
