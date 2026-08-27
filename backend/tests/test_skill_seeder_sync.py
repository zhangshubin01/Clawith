from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from app.models.skill import Skill, SkillFile
from app.services import skill_seeder
from app.services.skill_seeder import (
    BUILTIN_SKILL_VERSION_WHITELIST_KEY,
    BUILTIN_SKILLS,
    _align_default_skill_files,
    _content_md5,
    _default_skills_sync_digest,
    _load_version_whitelist,
)


def _capture_logs(level: str = "WARNING"):
    """Attach a local loguru sink; returns (messages, sink_id) — remove the
    sink with ``_release_logs``. A plain-callable sink receives the formatted
    message strings."""
    messages = []
    sink_id = logger.add(messages.append, level=level)
    return messages, sink_id


def _release_logs(sink_id) -> None:
    logger.remove(sink_id)


def _skill(*files: tuple[str, str]):
    return SimpleNamespace(
        folder_name="budget-approval-workflow",
        name="Budget Approval Workflow",
        files=[SimpleNamespace(path=path, content=content) for path, content in files],
    )


class Storage:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})
        self.writes: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def read_text(self, key: str, encoding: str = "utf-8") -> str:
        return self.files[key]

    async def write_text(self, key: str, content: str, *, encoding: str) -> None:
        assert encoding == "utf-8"
        self.files[key] = content
        self.writes.append((key, content))

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.deletes.append(key)


def test_default_skills_sync_digest_tracks_files_and_content() -> None:
    base = _skill(("SKILL.md", "instructions"))
    with_script = _skill(
        ("SKILL.md", "instructions"),
        ("scripts/auth.py", "authenticate()"),
    )
    changed_script = _skill(
        ("SKILL.md", "instructions"),
        ("scripts/auth.py", "authenticate_v2()"),
    )

    assert _default_skills_sync_digest([base]) != _default_skills_sync_digest([with_script])
    assert _default_skills_sync_digest([with_script]) != _default_skills_sync_digest([changed_script])


@pytest.mark.asyncio
async def test_align_fills_missing_files() -> None:
    storage = Storage()
    written, aligned, skipped = await _align_default_skill_files(
        storage,
        "agent",
        _skill(("SKILL.md", "registry"), ("scripts/auth.py", "authenticate()")),
        {},
    )

    assert (written, aligned, skipped) == (2, 0, 0)
    assert storage.files["agent/skills/budget-approval-workflow/SKILL.md"] == "registry"
    assert storage.writes == [
        ("agent/skills/budget-approval-workflow/SKILL.md", "registry"),
        ("agent/skills/budget-approval-workflow/scripts/auth.py", "authenticate()"),
    ]


@pytest.mark.asyncio
async def test_align_leaves_registry_matching_files_untouched() -> None:
    storage = Storage({"agent/skills/budget-approval-workflow/SKILL.md": "registry"})
    written, aligned, skipped = await _align_default_skill_files(
        storage,
        "agent",
        _skill(("SKILL.md", "registry")),
        {},
    )

    assert (written, aligned, skipped) == (0, 0, 0)
    assert storage.writes == []


@pytest.mark.asyncio
async def test_align_overwrites_whitelisted_historical_version() -> None:
    old = "old builtin content"
    storage = Storage({"agent/skills/budget-approval-workflow/SKILL.md": old})
    written, aligned, skipped = await _align_default_skill_files(
        storage,
        "agent",
        _skill(("SKILL.md", "new registry")),
        {"budget-approval-workflow/SKILL.md": {_content_md5(old)}},
    )

    assert (written, aligned, skipped) == (0, 1, 0)
    assert storage.files["agent/skills/budget-approval-workflow/SKILL.md"] == "new registry"
    assert storage.writes == [("agent/skills/budget-approval-workflow/SKILL.md", "new registry")]


@pytest.mark.asyncio
async def test_align_keeps_custom_content_not_in_whitelist() -> None:
    messages, sink_id = _capture_logs()
    try:
        storage = Storage({"agent/skills/budget-approval-workflow/SKILL.md": "custom"})
        written, aligned, skipped = await _align_default_skill_files(
            storage,
            "agent",
            _skill(("SKILL.md", "registry"), ("scripts/auth.py", "authenticate()")),
            {},
        )
    finally:
        _release_logs(sink_id)

    assert (written, aligned, skipped) == (1, 0, 1)
    assert storage.files["agent/skills/budget-approval-workflow/SKILL.md"] == "custom"
    assert storage.writes == [
        ("agent/skills/budget-approval-workflow/scripts/auth.py", "authenticate()")
    ]
    # The skipped file is reported with the agent, the file path and its md5.
    messages = list(messages)
    expected_md5 = _content_md5("custom")
    assert any(
        "budget-approval-workflow/SKILL.md" in m
        and "agent" in m
        and expected_md5 in m
        and "not in whitelist" in m
        for m in messages
    ), messages


# --- full-chain tests for push_default_skills_to_existing_agents ---


class _Result:
    def __init__(self, *, scalar=None, scalars=None):
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._scalars)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _push_dao(default_skills, hash_setting, whitelist_setting, agents):
    added = []

    async def execute(db, statement):
        text = str(statement)
        params = statement.compile().params
        if "system_settings" in text:
            key = params.get("key_1")
            if key == "default_skills_sync_hash":
                return _Result(scalar=hash_setting)
            if key == BUILTIN_SKILL_VERSION_WHITELIST_KEY:
                return _Result(scalar=whitelist_setting)
            raise AssertionError(f"unexpected setting key: {key}")
        if "skills.is_default" in text:
            return _Result(scalars=default_skills)
        if "agents.deleted_at" in text:
            return _Result(scalars=agents)
        raise AssertionError(f"unexpected statement: {text}")

    return SimpleNamespace(
        session=lambda: _SessionContext(SimpleNamespace()),
        execute=execute,
        add=lambda db, instance: added.append(instance),
        commit=AsyncMock(),
        added=added,
    )


def _install_push_fakes(monkeypatch, dao, storage):
    monkeypatch.setattr(skill_seeder, "query_dao", dao)
    monkeypatch.setattr("app.services.storage.get_storage_backend", lambda: storage)
    monkeypatch.setattr(
        "app.services.agent_manager.agent_manager",
        SimpleNamespace(_agent_storage_prefix=lambda agent_id: f"agent-{agent_id}"),
    )


@pytest.mark.asyncio
async def test_push_skips_alignment_when_sync_hash_matches(monkeypatch):
    skill = _skill(("SKILL.md", "registry"))
    whitelist = _load_version_whitelist(None)
    hash_setting = SimpleNamespace(
        key="default_skills_sync_hash",
        value={"hash": _default_skills_sync_digest([skill], whitelist)},
    )
    storage = Storage()
    dao = _push_dao([skill], hash_setting, None, [])

    _install_push_fakes(monkeypatch, dao, storage)
    await skill_seeder.push_default_skills_to_existing_agents()

    assert storage.writes == []
    assert storage.deletes == []
    assert hash_setting.value == {"hash": _default_skills_sync_digest([skill], whitelist)}


@pytest.mark.asyncio
async def test_push_aligns_stale_builtin_versions_when_whitelist_changes_digest(monkeypatch):
    registry = "registry v2"
    stale = "stale builtin v1"
    skill = _skill(("SKILL.md", registry))
    whitelist_setting = SimpleNamespace(
        key=BUILTIN_SKILL_VERSION_WHITELIST_KEY,
        value={"versions": {f"{skill.folder_name}/SKILL.md": [_content_md5(stale)]}},
    )
    whitelist = _load_version_whitelist(whitelist_setting.value)
    # Old-style digest (whitelist not folded in) so the gate opens.
    hash_setting = SimpleNamespace(
        key="default_skills_sync_hash",
        value={"hash": _default_skills_sync_digest([skill])},
    )
    storage = Storage({f"agent-1/skills/{skill.folder_name}/SKILL.md": stale})
    dao = _push_dao([skill], hash_setting, whitelist_setting, [SimpleNamespace(id=1)])

    _install_push_fakes(monkeypatch, dao, storage)
    await skill_seeder.push_default_skills_to_existing_agents()

    assert storage.files[f"agent-1/skills/{skill.folder_name}/SKILL.md"] == registry
    # Digest (with whitelist folded in) is persisted for the next run.
    assert hash_setting.value == {"hash": _default_skills_sync_digest([skill], whitelist)}


@pytest.mark.asyncio
async def test_push_keeps_custom_content_and_still_reports(monkeypatch):
    messages, sink_id = _capture_logs(level="INFO")
    try:
        skill = _skill(("SKILL.md", "registry"))
        whitelist = _load_version_whitelist(None)
        hash_setting = SimpleNamespace(
            key="default_skills_sync_hash",
            value={"hash": _default_skills_sync_digest([skill])},
        )
        storage = Storage({f"agent-1/skills/{skill.folder_name}/SKILL.md": "tenant customisation"})
        dao = _push_dao([skill], hash_setting, None, [SimpleNamespace(id=1)])

        _install_push_fakes(monkeypatch, dao, storage)
        await skill_seeder.push_default_skills_to_existing_agents()
    finally:
        _release_logs(sink_id)

    assert storage.files[f"agent-1/skills/{skill.folder_name}/SKILL.md"] == "tenant customisation"
    assert hash_setting.value == {"hash": _default_skills_sync_digest([skill], whitelist)}
    # A skill whose files were only "kept" is still reported per agent.
    messages = list(messages)
    assert any("kept (custom)" in m and "agent 1" in m for m in messages), messages


@pytest.mark.asyncio
async def test_push_continues_when_one_agent_fails(monkeypatch):
    messages, sink_id = _capture_logs()
    try:
        skill = _skill(("SKILL.md", "registry"))
        old_hash = _default_skills_sync_digest([skill])
        hash_setting = SimpleNamespace(
            key="default_skills_sync_hash",
            value={"hash": old_hash},
        )
        storage = Storage()
        dao = _push_dao([skill], hash_setting, None, [SimpleNamespace(id=1), SimpleNamespace(id=2)])
        _install_push_fakes(monkeypatch, dao, storage)

        def prefix(agent_id):
            if agent_id == 1:
                raise RuntimeError("storage unavailable for agent 1")
            return f"agent-{agent_id}"

        monkeypatch.setattr(
            "app.services.agent_manager.agent_manager",
            SimpleNamespace(_agent_storage_prefix=prefix),
        )

        await skill_seeder.push_default_skills_to_existing_agents()
    finally:
        _release_logs(sink_id)

    # Agent 2 still got aligned...
    assert storage.files[f"agent-2/skills/{skill.folder_name}/SKILL.md"] == "registry"
    # ...but the sync hash was NOT persisted, so the next startup retries the
    # failed agent instead of skipping it forever behind the hash gate.
    assert hash_setting.value == {"hash": old_hash}
    assert dao.commit.await_count == 1
    # The failure is reported with the agent id and the exception repr.
    messages = list(messages)
    assert any(
        "agent 1" in m and "storage unavailable for agent 1" in m for m in messages
    ), messages


@pytest.mark.asyncio
async def test_push_does_not_persist_hash_when_agent_fails_without_existing_setting(monkeypatch):
    skill = _skill(("SKILL.md", "registry"))
    storage = Storage()
    # No hash setting exists yet.
    dao = _push_dao([skill], None, None, [SimpleNamespace(id=1)])
    _install_push_fakes(monkeypatch, dao, storage)

    def prefix(agent_id):
        raise RuntimeError(f"storage unavailable for agent {agent_id}")

    monkeypatch.setattr(
        "app.services.agent_manager.agent_manager",
        SimpleNamespace(_agent_storage_prefix=prefix),
    )

    await skill_seeder.push_default_skills_to_existing_agents()

    # Nothing was persisted: no new SystemSetting row, but the commit still ran.
    assert dao.added == []
    assert dao.commit.await_count == 1


# --- seed_skills records replaced builtin versions into the whitelist ---


def _seed_result(scalar):
    return SimpleNamespace(scalar_one_or_none=lambda: scalar, scalar_one=lambda: scalar)


def _seed_dao(existing_skills_by_folder: dict):
    added = []

    async def execute(db, statement):
        text = str(statement)
        params = statement.compile().params
        # Both selects list every skill column; dispatch on the WHERE clause.
        if "WHERE skills.folder_name = :folder_name_1" in text:
            folder = params.get("folder_name_1")
            return _seed_result(existing_skills_by_folder.get(folder))
        if "WHERE skills.id = :id_1" in text:
            skill_id = params.get("id_1")
            skill = next(s for s in existing_skills_by_folder.values() if s.id == skill_id)
            return _seed_result(skill)
        raise AssertionError(f"unexpected statement: {text}")

    return SimpleNamespace(
        session=lambda: _SessionContext(SimpleNamespace()),
        execute=execute,
        add=lambda db, instance: added.append(instance),
        flush=AsyncMock(),
        commit=AsyncMock(),
        added=added,
    )


@pytest.mark.asyncio
async def test_seed_skills_records_replaced_versions_into_whitelist(monkeypatch):
    # Only web-research (BUILTIN_SKILLS[0]) already exists in the DB with an
    # old SKILL.md; every other builtin skill is newly created.
    old_content = "old web research skill content"
    web_skill = Skill(
        name="Web Research",
        description="old description",
        category="research",
        icon="🔍",
        folder_name="web-research",
        is_builtin=True,
        is_default=False,
        files=[
            SkillFile(path="SKILL.md", content=old_content),
            SkillFile(path="scripts/search_helper.py", content=BUILTIN_SKILLS[0]["files"][1]["content"]),
        ],
    )
    web_skill.id = 1
    dao = _seed_dao({"web-research": web_skill})

    # Make the registry version of web-research/SKILL.md differ from the DB one.
    monkeypatch.setitem(BUILTIN_SKILLS[0]["files"][0], "content", "updated registry content")
    # skill-creator files are populated from the filesystem at runtime; keep
    # them empty for determinism.
    monkeypatch.setattr(
        "app.services.skill_creator_content.get_skill_creator_files", lambda: []
    )
    persist_mock = AsyncMock()
    monkeypatch.setattr(skill_seeder, "_persist_replaced_versions", persist_mock)
    monkeypatch.setattr(skill_seeder, "query_dao", dao)

    await skill_seeder.seed_skills()

    # The superseded DB version was recorded and handed to the persister.
    persist_mock.assert_awaited_once()
    _db, _query_dao, replaced = persist_mock.await_args.args
    assert replaced == {"web-research/SKILL.md": _content_md5(old_content)}
    # The DB row was updated to the registry content.
    assert web_skill.files[0].content == "updated registry content"
    # All other 13 builtin skills were created from scratch.
    assert len([i for i in dao.added if isinstance(i, Skill)]) == len(BUILTIN_SKILLS) - 1
    assert dao.commit.await_count == 1
