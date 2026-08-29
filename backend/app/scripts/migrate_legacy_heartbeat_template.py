"""Safely replace the retired Plaza-era HEARTBEAT template.

The default mode is a read-only audit. Pass ``--apply`` to replace a file only
when its SHA-256 exactly matches the known official legacy template. Customized,
missing, and already-current files are never written.

Usage:
    python -m app.scripts.migrate_legacy_heartbeat_template
    python -m app.scripts.migrate_legacy_heartbeat_template --apply
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field, fields
import hashlib
from pathlib import Path
from typing import Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.services.storage_runtime.base import StorageBackend, WriteCondition
from app.services.storage_runtime.facade import get_storage_backend
from app.services.storage_runtime.fallback import FallbackStorageBackend

LEGACY_HEARTBEAT_SHA256 = "377e8e367d3aaa13d3932335787340363a88105fabe9717f758d90480843a6cd"
# All known retired HEARTBEAT templates, in the order they were shipped:
# - 377e8e36: original Plaza-era template
# - 5aed0d8c: four-phase reflections template (former app/templates fallback)
# - cb4dfa9c: minimal "awareness check" template (former agent_template, pre-2026-08-27)
# - a2ae09a8: unified template without the Phase 3 curiosity-convergence step
#   (retired 2026-08-28, replaced by the convergence template)
# - ed3de530: convergence template whose converge step could be read as conditional
#   (retired 2026-08-29, replaced by the unconditional-convergence template)
LEGACY_HEARTBEAT_SHA256S = frozenset(
    {
        LEGACY_HEARTBEAT_SHA256,
        "5aed0d8c1971483e23bc7124d336e2386b1ca24b19a08ba589f075996ef440e2",
        "cb4dfa9c49a226a39cd1befd266f7d43a36685f80485c13f14833b2d330a25cd",
        "a2ae09a8f34c1461d2473b1ad8229b855ecfb02d41088507c8c6c75e47364cc3",
        "ed3de530d1ae8b8fce0f59cbd2238d613f04fad48d2fae3d0dab46887ca029f6",
    }
)
HEARTBEAT_FILENAME = "HEARTBEAT.md"
HEARTBEAT_CONTENT_TYPE = "text/markdown; charset=utf-8"


@dataclass
class MigrationCounts:
    agents_scanned: int = 0
    legacy_matches: int = 0
    migrated: int = 0
    dry_run_matches: int = 0
    skipped_current: int = 0
    skipped_custom: int = 0
    skipped_missing: int = 0
    skipped_fallback_unmaterialized: int = 0
    conflicts: int = 0
    errors: int = 0

    def add(self, other: "MigrationCounts") -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))


@dataclass
class MigrationReport:
    total: MigrationCounts = field(default_factory=MigrationCounts)
    by_tenant: dict[str, MigrationCounts] = field(default_factory=dict)


@dataclass(frozen=True)
class _StorageSnapshot:
    data: bytes
    source: str
    write_condition: WriteCondition | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _heartbeat_key(agent_id: object) -> str:
    return f"{agent_id}/{HEARTBEAT_FILENAME}"


async def _read_snapshot(storage: StorageBackend, key: str) -> _StorageSnapshot | None:
    """Read without allowing FallbackStorageBackend to mutate during dry-run."""
    if isinstance(storage, FallbackStorageBackend):
        primary_version = await storage.primary.get_version(key)
        if primary_version.exists:
            if primary_version.is_dir:
                raise IsADirectoryError(key)
            return _StorageSnapshot(
                data=await storage.primary.read_bytes(key),
                source="primary",
                write_condition=WriteCondition(version_token=primary_version.token),
            )

        fallback_version = await storage.fallback.get_version(key)
        if not fallback_version.exists:
            return None
        if fallback_version.is_dir:
            raise IsADirectoryError(key)
        return _StorageSnapshot(
            data=await storage.fallback.read_bytes(key),
            source="fallback",
            write_condition=None,
        )

    version = await storage.get_version(key)
    if not version.exists:
        return None
    if version.is_dir:
        raise IsADirectoryError(key)
    return _StorageSnapshot(
        data=await storage.read_bytes(key),
        source="backend",
        write_condition=WriteCondition(version_token=version.token),
    )


def _audit_agent(
    *,
    tenant_id: object,
    agent_id: object,
    action: str,
    observed_sha256: str | None = None,
    error_type: str | None = None,
) -> None:
    details = f"tenant_id={tenant_id} agent_id={agent_id} action={action}"
    if observed_sha256:
        details += f" observed_sha256={observed_sha256}"
    if error_type:
        details += f" error_type={error_type}"
    logger.info(details)


def _legacy_set(legacy_sha256: str | frozenset[str]) -> frozenset[str]:
    """Normalize the single-string and set forms to one collection."""
    return {legacy_sha256} if isinstance(legacy_sha256, str) else legacy_sha256


async def _migrate_agent(
    storage: StorageBackend,
    *,
    tenant_id: object,
    agent_id: object,
    current_template: bytes,
    current_sha256: str,
    legacy_sha256: str | frozenset[str],
    apply: bool,
) -> MigrationCounts:
    counts = MigrationCounts(agents_scanned=1)
    key = _heartbeat_key(agent_id)
    legacy_set = _legacy_set(legacy_sha256)
    try:
        snapshot = await _read_snapshot(storage, key)
    except Exception as exc:
        counts.errors = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="read_error",
            error_type=type(exc).__name__,
        )
        return counts

    if snapshot is None:
        counts.skipped_missing = 1
        _audit_agent(tenant_id=tenant_id, agent_id=agent_id, action="skip_missing")
        return counts

    observed_sha256 = _sha256(snapshot.data)
    if observed_sha256 == current_sha256:
        counts.skipped_current = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="skip_current",
            observed_sha256=observed_sha256,
        )
        return counts
    if observed_sha256 not in legacy_set:
        counts.skipped_custom = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="skip_custom",
            observed_sha256=observed_sha256,
        )
        return counts

    counts.legacy_matches = 1
    if not apply:
        counts.dry_run_matches = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="would_migrate",
            observed_sha256=observed_sha256,
        )
        return counts

    if snapshot.source == "fallback":
        counts.conflicts = 1
        counts.skipped_fallback_unmaterialized = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="skip_fallback_unmaterialized",
            observed_sha256=observed_sha256,
        )
        return counts

    if snapshot.write_condition is None:
        raise RuntimeError("Writable storage snapshot is missing a write condition")

    try:
        result = await storage.write_bytes_if_match(
            key,
            current_template,
            condition=snapshot.write_condition,
            content_type=HEARTBEAT_CONTENT_TYPE,
        )
    except Exception as exc:
        counts.errors = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="write_error",
            observed_sha256=observed_sha256,
            error_type=type(exc).__name__,
        )
        return counts

    if not result.ok:
        counts.conflicts = 1
        _audit_agent(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action="skip_conflict",
            observed_sha256=observed_sha256,
        )
        return counts

    counts.migrated = 1
    _audit_agent(
        tenant_id=tenant_id,
        agent_id=agent_id,
        action="migrated",
        observed_sha256=observed_sha256,
    )
    return counts


async def migrate_legacy_heartbeat_templates(
    db: AsyncSession,
    storage: StorageBackend,
    *,
    current_template: bytes,
    apply: bool = False,
    legacy_sha256: str | frozenset[str] = LEGACY_HEARTBEAT_SHA256S,
) -> MigrationReport:
    """Audit or migrate non-deleted Agents, preserving tenant boundaries."""
    current_sha256 = _sha256(current_template)
    legacy_set = _legacy_set(legacy_sha256)
    if current_sha256 in legacy_set:
        raise ValueError("Current HEARTBEAT template still matches a legacy template")

    tenant_result = await db.execute(
        select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.id)
    )
    tenant_ids = tenant_result.scalars().all()
    report = MigrationReport()

    for tenant_id in tenant_ids:
        agent_result = await db.execute(
            select(Agent)
            .where(
                Agent.tenant_id == tenant_id,
                Agent.deleted_at.is_(None),
            )
            .order_by(Agent.id)
        )
        tenant_counts = MigrationCounts()
        for agent in agent_result.scalars().all():
            agent_counts = await _migrate_agent(
                storage,
                tenant_id=tenant_id,
                agent_id=agent.id,
                current_template=current_template,
                current_sha256=current_sha256,
                legacy_sha256=legacy_sha256,
                apply=apply,
            )
            tenant_counts.add(agent_counts)

        report.by_tenant[str(tenant_id)] = tenant_counts
        report.total.add(tenant_counts)
        logger.info("tenant_id={} summary={}", tenant_id, tenant_counts)

    logger.info(
        "heartbeat_template_migration mode={} active_tenants={} total={}",
        "apply" if apply else "dry-run",
        len(tenant_ids),
        report.total,
    )
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit or migrate known legacy HEARTBEAT templates",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write replacements; without this flag the script is read-only",
    )
    return parser.parse_args(argv)


def _current_template_bytes() -> bytes:
    template_path = Path(__file__).resolve().parents[2] / "agent_template" / HEARTBEAT_FILENAME
    return template_path.read_bytes()


async def main(*, apply: bool = False) -> MigrationReport:
    mode = "apply" if apply else "dry-run"
    logger.info("Starting legacy HEARTBEAT template migration in {} mode", mode)
    async with async_session() as db:
        return await migrate_legacy_heartbeat_templates(
            db,
            get_storage_backend(),
            current_template=_current_template_bytes(),
            apply=apply,
        )


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(main(apply=arguments.apply))
