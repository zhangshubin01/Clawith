from types import SimpleNamespace
import hashlib
import uuid

import pytest

from app.scripts import migrate_legacy_heartbeat_template as migration
from app.services.storage_runtime.base import ConditionalWriteResult, StorageVersion
from app.services.storage_runtime.fallback import FallbackStorageBackend


LEGACY_FIXTURE = b"# legacy heartbeat fixture\n"
LEGACY_FIXTURE_SHA256 = hashlib.sha256(LEGACY_FIXTURE).hexdigest()

# The pre-unification minimal template (former agent_template/HEARTBEAT.md until
# 2026-08-27). Pinned so the migration keeps recognizing it after unification.
PREUNIFICATION_MINIMAL_TEMPLATE = (
    b"# HEARTBEAT\n"
    b"\n"
    b"When this file is read during a heartbeat, you are performing a **periodic awareness check**.\n"
    b"\n"
    b"## Phase 1: Review Context & Discover Interest Points\n"
    b"\n"
    b"Review your **recent conversations** and your **role/responsibilities**.\n"
    b"Identify topics or questions that:\n"
    b"- Are directly relevant to your role and current work\n"
    b"- Were mentioned by users but not fully explored at the time\n"
    b"- Represent emerging trends or changes in your professional domain\n"
    b"- Could improve your ability to serve your users\n"
    b"\n"
    b"If no genuine, informative topics emerge from recent context, **skip exploration** and go directly to Phase 3.\n"
    b"Do NOT search for generic or obvious topics just to fill time. Quality over quantity.\n"
    b"\n"
    b"## Phase 2: Targeted Exploration (Conditional)\n"
    b"\n"
    b"Only if you identified genuine interest points in Phase 1:\n"
    b"\n"
    b"1. Use `web_search` to investigate (maximum 5 searches per heartbeat)\n"
    b"2. Keep searches **tightly scoped** to your role and recent work topics\n"
    b"3. For each discovery worth keeping:\n"
    b"   - Record it using `write_file` to `memory/curiosity_journal.md`\n"
    b"   - Include the **source URL** and a brief note on **why it matters to your work**\n"
    b"   - Rate its relevance (high/medium/low) to your current responsibilities\n"
    b"\n"
    b"Format for curiosity_journal.md entries:\n"
    b"```\n"
    b"### [Date] - [Topic]\n"
    b"- **Finding**: [What you learned]\n"
    b"- **Source**: [URL]\n"
    b"- **Relevance**: [high/medium/low] \xe2\x80\x94 [Why it matters to your work]\n"
    b"- **Follow-up**: [Optional: questions this raises for next time]\n"
    b"```\n"
    b"\n"
    b"## Phase 3: Wrap Up\n"
    b"\n"
    b"- If nothing needed attention and no exploration was warranted: reply with `HEARTBEAT_OK`\n"
    b"- Otherwise, briefly summarize what you explored and why\n"
    b"\n"
    b"## Key Principles\n"
    b"- Always ground exploration in YOUR role and YOUR recent work context\n"
    b"- Never search for random unrelated topics out of idle curiosity\n"
    b"- If you don't have a specific angle worth investigating, don't search\n"
    b"- Prefer depth over breadth \xe2\x80\x94 one thoroughly explored topic > five surface-level queries\n"
    b"- Generate follow-up questions only when you genuinely want to know more\n"
    b"\n"
    b"## Rules\n"
    b"- \xe2\x9b\x94 **NEVER share private information**: user conversations, memory contents, workspace files, task details\n"
    b"- \xe2\x9c\x85 **Share only public-safe content**: general insights, tips, industry news, web search discoveries with links\n"
    b"- \xf0\x9f\x93\x9d **Limits per heartbeat**: max 1 post + 2 comments\n"
    b"- \xf0\x9f\x94\x8d **Search limits**: max 5 web searches per heartbeat\n"
    b"- \xf0\x9f\xa4\x90 **If nothing interesting to explore or share**, respond with `HEARTBEAT_OK`\n"
)
PREUNIFICATION_MINIMAL_SHA256 = hashlib.sha256(PREUNIFICATION_MINIMAL_TEMPLATE).hexdigest()


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _Session:
    def __init__(self, tenant_ids, agents_by_tenant):
        self._results = [_Result(tenant_ids), *(_Result(agents) for agents in agents_by_tenant)]
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)


class _Storage:
    def __init__(self, files):
        self.files = dict(files)
        self.writes = []

    async def get_version(self, key):
        data = self.files.get(key)
        if data is None:
            return StorageVersion(key=key, exists=False, is_dir=False)
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            version_id=f"version:{hash(data)}",
        )

    async def read_bytes(self, key):
        try:
            return self.files[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    async def write_bytes_if_match(self, key, data, *, condition, content_type=None):
        current = await self.get_version(key)
        if condition.require_absent:
            matches = not current.exists
        else:
            matches = current.token == condition.version_token
        if not matches:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        self.files[key] = data
        self.writes.append((key, data, content_type))
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))


def _agent(tenant_id):
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_dry_run_reports_only_exact_legacy_matches_without_writing():
    tenant_id = uuid.uuid4()
    legacy_agent = _agent(tenant_id)
    current_agent = _agent(tenant_id)
    custom_agent = _agent(tenant_id)
    missing_agent = _agent(tenant_id)
    current = b"# current heartbeat\n"
    storage = _Storage(
        {
            f"{legacy_agent.id}/HEARTBEAT.md": LEGACY_FIXTURE,
            f"{current_agent.id}/HEARTBEAT.md": current,
            f"{custom_agent.id}/HEARTBEAT.md": b"# custom heartbeat\n",
        }
    )
    session = _Session([tenant_id], [[legacy_agent, current_agent, custom_agent, missing_agent]])

    report = await migration.migrate_legacy_heartbeat_templates(
        session,
        storage,
        current_template=current,
        apply=False,
        legacy_sha256=LEGACY_FIXTURE_SHA256,
    )

    assert report.total.agents_scanned == 4
    assert report.total.legacy_matches == 1
    assert report.total.dry_run_matches == 1
    assert report.total.migrated == 0
    assert report.total.skipped_current == 1
    assert report.total.skipped_custom == 1
    assert report.total.skipped_missing == 1
    assert report.total.conflicts == 0
    assert report.total.errors == 0
    assert report.by_tenant[str(tenant_id)] == report.total
    assert storage.writes == []

    tenant_sql = str(session.statements[0])
    agent_sql = str(session.statements[1])
    assert "tenants.is_active IS true" in tenant_sql
    assert "agents.tenant_id" in agent_sql
    assert "agents.status IN" not in agent_sql
    assert "agents.deleted_at IS NULL" in agent_sql


@pytest.mark.asyncio
async def test_apply_replaces_legacy_template_and_second_run_is_idempotent():
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    key = f"{agent.id}/HEARTBEAT.md"
    current = b"# current heartbeat\n"
    storage = _Storage({key: LEGACY_FIXTURE})

    first = await migration.migrate_legacy_heartbeat_templates(
        _Session([tenant_id], [[agent]]),
        storage,
        current_template=current,
        apply=True,
        legacy_sha256=LEGACY_FIXTURE_SHA256,
    )
    second = await migration.migrate_legacy_heartbeat_templates(
        _Session([tenant_id], [[agent]]),
        storage,
        current_template=current,
        apply=True,
        legacy_sha256=LEGACY_FIXTURE_SHA256,
    )

    assert first.total.migrated == 1
    assert second.total.migrated == 0
    assert second.total.skipped_current == 1
    assert storage.files[key] == current
    assert storage.writes == [(key, current, "text/markdown; charset=utf-8")]


@pytest.mark.asyncio
async def test_apply_counts_compare_and_swap_conflict_without_overwriting():
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    key = f"{agent.id}/HEARTBEAT.md"
    storage = _Storage({key: LEGACY_FIXTURE})
    original_write = storage.write_bytes_if_match

    async def racing_write(key, data, *, condition, content_type=None):
        storage.files[key] = b"# user changed this during migration\n"
        return await original_write(key, data, condition=condition, content_type=content_type)

    storage.write_bytes_if_match = racing_write

    report = await migration.migrate_legacy_heartbeat_templates(
        _Session([tenant_id], [[agent]]),
        storage,
        current_template=b"# current heartbeat\n",
        apply=True,
        legacy_sha256=LEGACY_FIXTURE_SHA256,
    )

    assert report.total.legacy_matches == 1
    assert report.total.migrated == 0
    assert report.total.conflicts == 1
    assert storage.files[key] == b"# user changed this during migration\n"


@pytest.mark.asyncio
async def test_fallback_storage_dry_run_does_not_materialize_or_write_primary():
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    key = f"{agent.id}/HEARTBEAT.md"
    primary = _Storage({})
    fallback = _Storage({key: LEGACY_FIXTURE})

    report = await migration.migrate_legacy_heartbeat_templates(
        _Session([tenant_id], [[agent]]),
        FallbackStorageBackend(primary=primary, fallback=fallback),
        current_template=b"# current heartbeat\n",
        apply=False,
        legacy_sha256=LEGACY_FIXTURE_SHA256,
    )

    assert report.total.dry_run_matches == 1
    assert primary.files == {}
    assert primary.writes == []


@pytest.mark.asyncio
async def test_fallback_apply_does_not_cross_backend_cas_after_source_race():
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    key = f"{agent.id}/HEARTBEAT.md"
    primary = _Storage({})
    fallback = _Storage({key: LEGACY_FIXTURE})
    original_read = fallback.read_bytes

    async def racing_fallback_read(key):
        data = await original_read(key)
        fallback.files[key] = b"# user changed fallback during migration\n"
        return data

    fallback.read_bytes = racing_fallback_read

    report = await migration.migrate_legacy_heartbeat_templates(
        _Session([tenant_id], [[agent]]),
        FallbackStorageBackend(primary=primary, fallback=fallback),
        current_template=b"# current heartbeat\n",
        apply=True,
        legacy_sha256=LEGACY_FIXTURE_SHA256,
    )

    assert report.total.legacy_matches == 1
    assert report.total.migrated == 0
    assert report.total.conflicts == 1
    assert report.total.skipped_fallback_unmaterialized == 1
    assert primary.files == {}
    assert primary.writes == []
    assert fallback.files[key] == b"# user changed fallback during migration\n"


def test_cli_defaults_to_dry_run_and_requires_apply_flag():
    assert migration.LEGACY_HEARTBEAT_SHA256 == "377e8e367d3aaa13d3932335787340363a88105fabe9717f758d90480843a6cd"
    assert hashlib.sha256(migration._current_template_bytes()).hexdigest() == (
        "30283e1a06bc659e737134c903990f28cbf946ef765f019c060acba93110fdd9"
    )
    assert migration.parse_args([]).apply is False
    assert migration.parse_args(["--apply"]).apply is True


def test_preunification_minimal_template_hash_is_in_legacy_set():
    """统一前的两版模板 hash 必须留在迁移清单里（防再分叉后无法自愈）。"""
    assert PREUNIFICATION_MINIMAL_SHA256 in migration.LEGACY_HEARTBEAT_SHA256S
    assert (
        "5aed0d8c1971483e23bc7124d336e2386b1ca24b19a08ba589f075996ef440e2"
        in migration.LEGACY_HEARTBEAT_SHA256S
    )
    assert migration.LEGACY_HEARTBEAT_SHA256 in migration.LEGACY_HEARTBEAT_SHA256S
    # 收敛步之前的统一版模板也必须在清单里（2026-08-28 退役）。
    assert (
        "a2ae09a8f34c1461d2473b1ad8229b855ecfb02d41088507c8c6c75e47364cc3"
        in migration.LEGACY_HEARTBEAT_SHA256S
    )


@pytest.mark.asyncio
async def test_default_legacy_set_migrates_preunification_minimal_template():
    tenant_id = uuid.uuid4()
    minimal_agent = _agent(tenant_id)
    current = b"# current heartbeat\n"
    storage = _Storage(
        {
            f"{minimal_agent.id}/HEARTBEAT.md": PREUNIFICATION_MINIMAL_TEMPLATE,
        }
    )
    session = _Session([tenant_id], [[minimal_agent]])

    report = await migration.migrate_legacy_heartbeat_templates(
        session,
        storage,
        current_template=current,
        apply=True,
    )

    assert report.total.legacy_matches == 1
    assert report.total.migrated == 1
    assert report.total.skipped_custom == 0
    assert storage.files[f"{minimal_agent.id}/HEARTBEAT.md"] == current
