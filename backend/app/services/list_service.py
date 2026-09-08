"""Structured List service (清单线 focus 范式迁移).

Lists are stored in the database. Legacy ``memory/清单.md`` parsing exists only
for a one-time import path; runtime reads and writes must use this service.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from app.dao import list_dao
from app.database import bind_session_context
from app.models.list import AgentListItem as AgentListItemModel
from app.services.agent_runtime.list_persistence import (
    LIST_FILE_PATH,
    ListItem,
    parse_list_file,
)
from app.services.storage import get_storage_backend, normalize_storage_key

VALID_STATUSES = {"pending", "in_progress", "completed"}


def slugify_list_key(value: str, *, fallback: str = "item") -> str:
    """Create a stable ASCII-ish key for one list item (mirrors focus slug)."""
    raw = (value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    return (raw or fallback)[:200]


def is_list_file_path(path: str | None) -> bool:
    """Whether a read_file path targets the legacy list file (intercept)."""
    normalized = (path or "").strip().replace("\\", "/").strip("/")
    return normalized.lower() == "清单.md" or normalized.lower().endswith("/清单.md")


def _serialize_list_item(item: AgentListItemModel) -> dict:
    return {
        "id": str(item.id),
        "agent_id": str(item.agent_id),
        "project": item.project,
        "key": item.key,
        "title": item.title,
        "description": item.description or item.key,
        "status": item.status,
        "sort_order": item.sort_order,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


async def _load_legacy_list_file(agent_id: uuid.UUID) -> str:
    """Read the legacy ``memory/清单.md``, or ``""`` on any miss/failure."""
    storage = get_storage_backend()
    key = normalize_storage_key(f"{agent_id}/{LIST_FILE_PATH}")
    try:
        if not await storage.exists(key) or not await storage.is_file(key):
            return ""
        return await storage.read_text(key, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


async def migrate_legacy_list_file(agent_id: uuid.UUID, db=None) -> int:
    """Import legacy ``清单.md`` once per (agent, project) scope.

    Section numbering is preserved verbatim as ``sort_order`` (so migrated
    ``92. GitLab CI`` keeps sort_order 92, never renumbered from 1). Sections
    without a resolvable project fall back to a synthetic ``legacy:<list_id>``
    scope so distinct NULL-project lists never merge. Idempotent via
    ``on_conflict_do_nothing``.
    """
    if db is not None:
        async with bind_session_context(db):
            return await _migrate_legacy_list_file_impl(agent_id)
    return await _migrate_legacy_list_file_impl(agent_id)


async def _migrate_legacy_list_file_impl(agent_id: uuid.UUID) -> int:
    content = await _load_legacy_list_file(agent_id)
    if not content:
        return 0
    parsed = parse_list_file(content)
    rows: list[dict] = []
    for section in parsed.sections:
        if not section.items:
            continue
        project = section.project or f"legacy:{section.list_id}"
        existing_keys = await list_dao.list_by_project(
            agent_id=agent_id,
            project=project,
            include_completed=True,
        )
        if existing_keys:
            # Already migrated for this scope (idempotent skip).
            continue
        seen: set[str] = set()
        for item in section.items:
            key = slugify_list_key(item.title or item.description)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append({
                "agent_id": agent_id,
                "project": project,
                "key": key,
                "title": item.title,
                "description": item.description,
                "status": "pending",
                "sort_order": item.number,
                "completed_at": None,
            })
    if not rows:
        return 0
    return await list_dao.bulk_insert_legacy_rows(rows)


async def list_list_items(
    agent_id: uuid.UUID,
    *,
    project: str,
    include_completed: bool = False,
    db=None,
) -> list[dict]:
    """List items for one (agent, project) in stable sort_order."""
    if db is not None:
        async with bind_session_context(db):
            await _migrate_legacy_list_file_impl(agent_id)
            items = await list_dao.list_by_project(
                agent_id=agent_id,
                project=project,
                include_completed=include_completed,
            )
            return [_serialize_list_item(item) for item in items]
    await _migrate_legacy_list_file_impl(agent_id)
    items = await list_dao.list_by_project(
        agent_id=agent_id,
        project=project,
        include_completed=include_completed,
    )
    return [_serialize_list_item(item) for item in items]


async def upsert_list_item(
    agent_id: uuid.UUID,
    *,
    project: str,
    key: str | None,
    title: str | None = None,
    description: str,
    status: str = "pending",
    db=None,
) -> dict:
    """Create or update one list item; platform-assigned scope + number.

    The model writes only ``key``/``title``/``description``/``status``.
    ``project`` and ``sort_order`` are platform-controlled (never model
    supplied); ``key`` is normalized with ``slugify_list_key``.
    """
    await migrate_legacy_list_file(agent_id, db=db)
    desc = (description or "").strip()
    item_key = slugify_list_key((key or "").strip() or desc)
    if status not in VALID_STATUSES:
        status = "pending"
    completed_at = datetime.now(timezone.utc) if status == "completed" else None

    if db is not None:
        async with bind_session_context(db):
            item = await list_dao.upsert_item(
                agent_id=agent_id,
                project=project,
                key=item_key,
                title=title,
                description=desc,
                status=status,
                completed_at=completed_at,
            )
            return _serialize_list_item(item)
    item = await list_dao.upsert_item(
        agent_id=agent_id,
        project=project,
        key=item_key,
        title=title,
        description=desc,
        status=status,
        completed_at=completed_at,
    )
    return _serialize_list_item(item)


async def complete_list_item(agent_id: uuid.UUID, *, project: str, key: str, db=None) -> dict | None:
    """Complete one list item by key; ``None`` when not found."""
    await migrate_legacy_list_file(agent_id, db=db)
    if db is not None:
        async with bind_session_context(db):
            item = await list_dao.complete_item(
                agent_id=agent_id,
                project=project,
                key=key,
                completed_at=datetime.now(timezone.utc),
            )
            return _serialize_list_item(item) if item else None
    item = await list_dao.complete_item(
        agent_id=agent_id,
        project=project,
        key=key,
        completed_at=datetime.now(timezone.utc),
    )
    return _serialize_list_item(item) if item else None
