from types import SimpleNamespace

import pytest

from app.models.system_settings import SystemSetting
from app.services.skill_seeder import (
    BUILTIN_SKILL_VERSION_SEED,
    _default_skills_sync_digest,
    _load_version_whitelist,
    _persist_replaced_versions,
    _record_replaced_versions,
)


def _skill(*files: tuple[str, str]):
    return SimpleNamespace(
        folder_name="budget-approval-workflow",
        files=[SimpleNamespace(path=path, content=content) for path, content in files],
    )


def test_default_skills_sync_digest_includes_whitelist() -> None:
    skill = _skill(("SKILL.md", "instructions"))
    wl_a = {"skill-creator/SKILL.md": {"d0cffe612ecddd187c06e21f2a314bb0"}}
    wl_b = {
        "skill-creator/SKILL.md": {
            "d0cffe612ecddd187c06e21f2a314bb0",
            "ffffffffffffffffffffffffffffffff",
        }
    }

    assert _default_skills_sync_digest([skill], wl_a) != _default_skills_sync_digest(
        [skill], wl_b
    )
    assert _default_skills_sync_digest([skill], wl_a) != _default_skills_sync_digest([skill])
    assert _default_skills_sync_digest([skill], None) == _default_skills_sync_digest([skill])


def test_seed_contains_the_investigated_historical_versions() -> None:
    # Ground truth from the 2026-08-27 file-layer investigation: the six
    # skill-creator files whose agent-file-layer versions lag behind the DB.
    assert BUILTIN_SKILL_VERSION_SEED == {
        "skill-creator/SKILL.md": {"d0cffe612ecddd187c06e21f2a314bb0"},
        "skill-creator/scripts/clawith_runner.py": {"2f1ec894797734bb6d2ff7fe5508edcf"},
        "skill-creator/scripts/improve_description.py": {"c72cbcbb8f9b0b72de4371547c5f8687"},
        "skill-creator/scripts/quick_validate.py": {"b74b2cdce300d20ed6c64d0bf4738a0c"},
        "skill-creator/scripts/run_eval.py": {"80c38214917bf1dfbf5024efd7d47bd9"},
        "skill-creator/scripts/run_loop.py": {"f4662ff05f8946960571362f9c4dcfbd"},
    }


def test_load_version_whitelist_merges_seed_and_persisted() -> None:
    assert _load_version_whitelist(None) == BUILTIN_SKILL_VERSION_SEED

    persisted = {
        "versions": {
            "skill-creator/SKILL.md": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
            "other-skill/x.py": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        }
    }
    merged = _load_version_whitelist(persisted)
    assert merged["skill-creator/SKILL.md"] == {
        "d0cffe612ecddd187c06e21f2a314bb0",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    assert merged["other-skill/x.py"] == {"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
    # Seed paths untouched by persisted data stay present.
    assert merged["skill-creator/scripts/run_eval.py"] == {"80c38214917bf1dfbf5024efd7d47bd9"}


def test_record_replaced_versions_appends_and_dedups() -> None:
    first = _record_replaced_versions(None, {"p1": "aaa", "p2": "bbb"})
    assert first == {"versions": {"p1": ["aaa"], "p2": ["bbb"]}}

    second = _record_replaced_versions(first, {"p1": "aaa", "p3": "ccc"})
    assert second == {"versions": {"p1": ["aaa"], "p2": ["bbb"], "p3": ["ccc"]}}


class _FakeDao:
    def __init__(self, setting: object | None) -> None:
        self.setting = setting
        self.added: list[object] = []

    async def execute(self, db, statement):
        assert "FROM system_settings" in str(statement)
        assert "WHERE system_settings.key = :key_1" in str(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.setting)

    def add(self, db, instance) -> None:
        self.added.append(instance)


@pytest.mark.asyncio
async def test_persist_replaced_versions_creates_setting_when_missing() -> None:
    dao = _FakeDao(None)
    await _persist_replaced_versions(
        None,
        dao,
        {"skill-creator/SKILL.md": "d0cffe612ecddd187c06e21f2a314bb0"},
    )

    assert len(dao.added) == 1
    setting = dao.added[0]
    assert isinstance(setting, SystemSetting)
    assert setting.key == "builtin_skill_version_whitelist"
    assert setting.value == {
        "versions": {"skill-creator/SKILL.md": ["d0cffe612ecddd187c06e21f2a314bb0"]}
    }


@pytest.mark.asyncio
async def test_persist_replaced_versions_updates_existing_setting() -> None:
    existing = SimpleNamespace(value={"versions": {"p1": ["aaa"]}})
    dao = _FakeDao(existing)

    await _persist_replaced_versions(None, dao, {"p1": "aaa", "p2": "bbb"})

    assert dao.added == []
    assert existing.value == {"versions": {"p1": ["aaa"], "p2": ["bbb"]}}


@pytest.mark.asyncio
async def test_persist_replaced_versions_noops_when_nothing_replaced() -> None:
    dao = _FakeDao(None)

    await _persist_replaced_versions(None, dao, {})

    assert dao.added == []
