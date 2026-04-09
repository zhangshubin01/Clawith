"""OKR REST API — objectives, key results, settings, reports and periods.

All endpoints are tenant-scoped: data is filtered by the requesting user's
tenant_id so cross-tenant leakage is impossible.

Route summary
─────────────
GET/PUT   /api/okr/settings
GET       /api/okr/periods
GET/POST  /api/okr/objectives
PATCH     /api/okr/objectives/{id}
DELETE    /api/okr/objectives/{id}
GET/POST  /api/okr/objectives/{id}/key-results
PATCH     /api/okr/key-results/{id}
POST      /api/okr/key-results/{id}/progress        (manual progress update)
DELETE    /api/okr/key-results/{id}
GET       /api/okr/key-results/{id}/progress-log    (P3: history curve)
POST      /api/okr/alignments                       (P3: create alignment)
DELETE    /api/okr/alignments/{id}                  (P3: remove alignment)
GET       /api/okr/reports
GET       /api/okr/members-without-okr              (P4: members with no OKR this period)
POST      /api/okr/trigger-member-outreach          (P4: fire OKR Agent to nudge those members)
"""

import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select, delete

from app.api.auth import get_current_user
from app.database import async_session
from app.models.okr import (
    OKRAlignment,
    OKRKeyResult,
    OKRObjective,
    OKRProgressLog,
    OKRSettings,
    WorkReport,
)

router = APIRouter(prefix="/api/okr", tags=["okr"])


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _get_or_create_settings(db, tenant_id: uuid.UUID) -> OKRSettings:
    """Return the OKRSettings row for this tenant, creating it if missing."""
    result = await db.execute(
        select(OKRSettings).where(OKRSettings.tenant_id == tenant_id)
    )
    settings = result.scalar_one_or_none()
    if not settings:
        settings = OKRSettings(tenant_id=tenant_id)
        db.add(settings)
        await db.flush()
    return settings


def _compute_current_period(
    frequency: str, length_days: int | None
) -> tuple[date, date]:
    """Compute the start and end dates of the current OKR period.

    This is a simple deterministic calculation from today's date so the
    frontend and API always agree on what "the current period" is.
    """
    today = date.today()
    if frequency == "monthly":
        start = today.replace(day=1)
        # Last day of this month
        if today.month == 12:
            end = today.replace(month=12, day=31)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        # Align to multiples of length_days from the Unix epoch
        epoch = date(1970, 1, 1)
        days_since_epoch = (today - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        # Default: quarterly (Q1/Q2/Q3/Q4)
        quarter = (today.month - 1) // 3 + 1
        start = date(today.year, (quarter - 1) * 3 + 1, 1)
        if quarter == 4:
            end = date(today.year, 12, 31)
        else:
            end = date(today.year, quarter * 3 + 1, 1) - timedelta(days=1)
    return start, end


# ─── Pydantic schemas ─────────────────────────────────────────────────────────


class OKRSettingsOut(BaseModel):
    enabled: bool
    daily_report_enabled: bool
    daily_report_time: str
    weekly_report_enabled: bool
    weekly_report_day: int
    period_frequency: str
    period_length_days: int | None = None


class OKRSettingsUpdate(BaseModel):
    enabled: bool | None = None
    daily_report_enabled: bool | None = None
    daily_report_time: str | None = None
    weekly_report_enabled: bool | None = None
    weekly_report_day: int | None = None
    period_frequency: str | None = None
    period_length_days: int | None = None


class KeyResultOut(BaseModel):
    id: str
    objective_id: str
    title: str
    target_value: float
    current_value: float
    unit: str | None = None
    focus_ref: str | None = None
    status: str
    last_updated_at: str
    created_at: str
    # Alignment refs (read-only summary)
    alignments: list[dict] = []


class ObjectiveOut(BaseModel):
    id: str
    title: str
    description: str | None = None
    owner_type: str
    owner_id: str | None = None
    # Display info for the owner (resolved from User/Agent table)
    owner_display_name: str | None = None
    owner_avatar_url: str | None = None
    period_start: str
    period_end: str
    status: str
    created_at: str
    key_results: list[KeyResultOut] = []
    # Alignment refs — each entry: {id, target_type, target_id, target_title}
    alignments: list[dict] = []


class ObjectiveCreate(BaseModel):
    title: str
    description: str | None = None
    owner_type: str = "company"
    owner_id: str | None = None
    period_start: str
    period_end: str


class ObjectiveUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None


class KeyResultCreate(BaseModel):
    title: str
    target_value: float = 100.0
    unit: str | None = None
    focus_ref: str | None = None


class KeyResultUpdate(BaseModel):
    title: str | None = None
    current_value: float | None = None
    target_value: float | None = None
    unit: str | None = None
    focus_ref: str | None = None
    status: str | None = None


class ProgressUpdate(BaseModel):
    value: float
    note: str | None = None
    # Optional explicit status override; when omitted, auto-computed from progress ratio
    status: str | None = None


class PeriodOut(BaseModel):
    start: str
    end: str
    label: str
    is_current: bool


class WorkReportOut(BaseModel):
    id: str
    author_type: str
    author_id: str
    report_type: str
    period_date: str
    content: str
    source: str
    created_at: str


class ProgressLogOut(BaseModel):
    """Single OKRProgressLog entry for progress curve display."""
    id: str
    kr_id: str
    previous_value: float
    new_value: float
    source: str
    note: str | None = None
    created_at: str


class AlignmentCreate(BaseModel):
    """Create an alignment: source objective → target objective/key-result."""
    source_type: str = "objective"   # "objective" | "key_result"
    source_id: str
    target_type: str = "objective"   # "objective" | "key_result"
    target_id: str


class AlignmentOut(BaseModel):
    id: str
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    target_title: str = ""  # Convenience: resolved title of the target
    created_at: str


# ─── Settings ─────────────────────────────────────────────────────────────────


@router.get("/settings", response_model=OKRSettingsOut)
async def get_okr_settings(user=Depends(get_current_user)):
    """Return OKR configuration for the current tenant."""
    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)
        await db.commit()
        return OKRSettingsOut(
            enabled=settings.enabled,
            daily_report_enabled=settings.daily_report_enabled,
            daily_report_time=settings.daily_report_time,
            weekly_report_enabled=settings.weekly_report_enabled,
            weekly_report_day=settings.weekly_report_day,
            period_frequency=settings.period_frequency,
            period_length_days=settings.period_length_days,
        )


@router.put("/settings", response_model=OKRSettingsOut)
async def update_okr_settings(body: OKRSettingsUpdate, user=Depends(get_current_user)):
    """Update OKR configuration. Org admins only."""
    # Allow org admins and platform admins to modify OKR settings.
    # user.role is the canonical authority; is_admin is not a real field.
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can modify OKR settings")

    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)

        if body.enabled is not None:
            settings.enabled = body.enabled
        if body.daily_report_enabled is not None:
            settings.daily_report_enabled = body.daily_report_enabled
        if body.daily_report_time is not None:
            settings.daily_report_time = body.daily_report_time
        if body.weekly_report_enabled is not None:
            settings.weekly_report_enabled = body.weekly_report_enabled
        if body.weekly_report_day is not None:
            settings.weekly_report_day = body.weekly_report_day
        if body.period_frequency is not None:
            settings.period_frequency = body.period_frequency
        if body.period_length_days is not None:
            settings.period_length_days = body.period_length_days

        await db.commit()
        return OKRSettingsOut(
            enabled=settings.enabled,
            daily_report_enabled=settings.daily_report_enabled,
            daily_report_time=settings.daily_report_time,
            weekly_report_enabled=settings.weekly_report_enabled,
            weekly_report_day=settings.weekly_report_day,
            period_frequency=settings.period_frequency,
            period_length_days=settings.period_length_days,
        )


# ─── Periods ──────────────────────────────────────────────────────────────────


@router.get("/periods", response_model=list[PeriodOut])
async def list_periods(user=Depends(get_current_user)):
    """Return an ordered list of OKR periods (past 2 + current + next 1).

    Periods are computed from the tenant's OKR settings frequency, not from
    database rows, so they always exist even if they have no OKRs yet.
    """
    async with async_session() as db:
        settings = await _get_or_create_settings(db, user.tenant_id)
        await db.commit()

    freq = settings.period_frequency
    length = settings.period_length_days

    cur_start, cur_end = _compute_current_period(freq, length)

    def _period_label(start: date, freq: str) -> str:
        if freq == "monthly":
            return start.strftime("%b %Y")
        elif freq == "quarterly":
            q = (start.month - 1) // 3 + 1
            return f"Q{q} {start.year}"
        else:
            return f"{start.isoformat()} – {(start + timedelta(days=(length or 90) - 1)).isoformat()}"

    def _prev_start(start: date) -> date:
        if freq == "quarterly":
            return (start - timedelta(days=1)).replace(
                day=1, month=((start.month - 4) % 12) + 1
            )
        elif freq == "monthly":
            m = start.month - 1 or 12
            y = start.year if start.month > 1 else start.year - 1
            return start.replace(year=y, month=m, day=1)
        else:
            return start - timedelta(days=length or 90)

    def _next_start(end: date) -> date:
        return end + timedelta(days=1)

    periods = []
    # Previous 2 periods
    s = cur_start
    for _ in range(2):
        s = _prev_start(s)
    for _ in range(2):
        ps, pe = _compute_current_period(freq, length) if s == cur_start else (s, _compute_current_period(freq, length)[1])
        ns = _next_start(s)
        _, pe = _compute_current_period(freq, length) if s == cur_start else (None, None)
        if pe is None:
            # For non-quarterly/monthly we approximate end
            pe = s + timedelta(days=(length or 90) - 1)
        periods.append(PeriodOut(
            start=s.isoformat(),
            end=pe.isoformat(),
            label=_period_label(s, freq),
            is_current=(s == cur_start),
        ))
        s = _next_start(pe)

    # Current period
    periods.append(PeriodOut(
        start=cur_start.isoformat(),
        end=cur_end.isoformat(),
        label=_period_label(cur_start, freq),
        is_current=True,
    ))
    # Next period
    next_start = _next_start(cur_end)
    next_end = next_start + timedelta(days=(length or 90) - 1)
    periods.append(PeriodOut(
        start=next_start.isoformat(),
        end=next_end.isoformat(),
        label=_period_label(next_start, freq),
        is_current=False,
    ))

    return sorted(periods, key=lambda p: p.start)


# ─── Objectives ───────────────────────────────────────────────────────────────


def _kr_to_out(kr: OKRKeyResult) -> KeyResultOut:
    return KeyResultOut(
        id=str(kr.id),
        objective_id=str(kr.objective_id),
        title=kr.title,
        target_value=kr.target_value,
        current_value=kr.current_value,
        unit=kr.unit,
        focus_ref=kr.focus_ref,
        status=kr.status,
        last_updated_at=kr.last_updated_at.isoformat() if kr.last_updated_at else "",
        created_at=kr.created_at.isoformat() if kr.created_at else "",
    )


def _obj_to_out(
    obj: OKRObjective,
    krs: list[OKRKeyResult] | None = None,
    alignments: list[dict] | None = None,
    owner_display_name: str | None = None,
    owner_avatar_url: str | None = None,
) -> ObjectiveOut:
    return ObjectiveOut(
        id=str(obj.id),
        title=obj.title,
        description=obj.description,
        owner_type=obj.owner_type,
        owner_id=str(obj.owner_id) if obj.owner_id else None,
        owner_display_name=owner_display_name,
        owner_avatar_url=owner_avatar_url,
        period_start=obj.period_start.isoformat(),
        period_end=obj.period_end.isoformat(),
        status=obj.status,
        created_at=obj.created_at.isoformat() if obj.created_at else "",
        key_results=[_kr_to_out(kr) for kr in (krs or [])],
        alignments=alignments or [],
    )


@router.get("/objectives", response_model=list[ObjectiveOut])
async def list_objectives(
    period_start: str | None = None,
    period_end: str | None = None,
    user=Depends(get_current_user),
):
    """List all Objectives for the current tenant within a period.

    If period_start / period_end are not supplied, defaults to the current
    OKR period computed from the tenant's OKR settings.

    Each Objective includes owner_display_name and owner_avatar_url so the
    frontend can render member attribution without additional API calls.
    """
    from app.models.user import User
    from app.models.agent import Agent

    async with async_session() as db:
        if not period_start or not period_end:
            settings = await _get_or_create_settings(db, user.tenant_id)
            ps, pe = _compute_current_period(
                settings.period_frequency, settings.period_length_days
            )
            await db.commit()
        else:
            ps = date.fromisoformat(period_start)
            pe = date.fromisoformat(period_end)

        result = await db.execute(
            select(OKRObjective)
            .where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
            )
            .order_by(OKRObjective.owner_type, OKRObjective.created_at)
        )
        objectives = result.scalars().all()

        # Collect owner IDs by type for batch resolution
        user_owner_ids: list[uuid.UUID] = []
        agent_owner_ids: list[uuid.UUID] = []
        for o in objectives:
            if o.owner_id:
                if o.owner_type == "user":
                    user_owner_ids.append(o.owner_id)
                elif o.owner_type == "agent":
                    agent_owner_ids.append(o.owner_id)

        # Batch-fetch owner display info from User and Agent tables
        user_display: dict[uuid.UUID, tuple[str, str]] = {}  # id -> (name, avatar_url)
        agent_display: dict[uuid.UUID, tuple[str, str]] = {}

        if user_owner_ids:
            ur = await db.execute(
                select(User.id, User.full_name, User.avatar_url)
                .where(User.id.in_(user_owner_ids))
            )
            for row in ur.all():
                user_display[row[0]] = (row[1] or "", row[2] or "")

        if agent_owner_ids:
            ar = await db.execute(
                select(Agent.id, Agent.name, Agent.avatar_url)
                .where(Agent.id.in_(agent_owner_ids))
            )
            for row in ar.all():
                agent_display[row[0]] = (row[1] or "", row[2] or "")

        # Fetch all KRs for these objectives in one query
        obj_ids = [o.id for o in objectives]
        krs_result = await db.execute(
            select(OKRKeyResult)
            .where(OKRKeyResult.objective_id.in_(obj_ids))
            .order_by(OKRKeyResult.created_at)
        )
        all_krs = krs_result.scalars().all()

        # Group KRs by objective
        krs_by_obj: dict[uuid.UUID, list[OKRKeyResult]] = {}
        for kr in all_krs:
            krs_by_obj.setdefault(kr.objective_id, []).append(kr)

        # Fetch alignments where source is one of these objectives, with target titles
        alignments_result = await db.execute(
            select(OKRAlignment)
            .where(
                OKRAlignment.source_type == "objective",
                OKRAlignment.source_id.in_(obj_ids),
            )
        )
        all_alignments = alignments_result.scalars().all()

        # Resolve target titles from objectives table
        target_obj_ids = [
            a.target_id for a in all_alignments if a.target_type == "objective"
        ]
        target_objs: dict[uuid.UUID, str] = {}
        if target_obj_ids:
            target_result = await db.execute(
                select(OKRObjective.id, OKRObjective.title)
                .where(OKRObjective.id.in_(target_obj_ids))
            )
            for row in target_result.all():
                target_objs[row[0]] = row[1]

        # Group alignments by source objective id
        alignments_by_obj: dict[uuid.UUID, list[dict]] = {}
        for al in all_alignments:
            target_title = target_objs.get(al.target_id, "") if al.target_type == "objective" else ""
            alignments_by_obj.setdefault(al.source_id, []).append({
                "id": str(al.id),
                "source_type": al.source_type,
                "source_id": str(al.source_id),
                "target_type": al.target_type,
                "target_id": str(al.target_id),
                "target_title": target_title,
            })

        out = []
        for o in objectives:
            # Resolve display name and avatar from pre-fetched maps
            display_name: str | None = None
            avatar_url: str | None = None
            if o.owner_id:
                if o.owner_type == "user" and o.owner_id in user_display:
                    display_name, avatar_url = user_display[o.owner_id]
                elif o.owner_type == "agent" and o.owner_id in agent_display:
                    display_name, avatar_url = agent_display[o.owner_id]

            out.append(_obj_to_out(
                o,
                krs_by_obj.get(o.id, []),
                alignments_by_obj.get(o.id, []),
                owner_display_name=display_name,
                owner_avatar_url=avatar_url,
            ))
        return out


@router.post("/objectives", response_model=ObjectiveOut)
async def create_objective(body: ObjectiveCreate, user=Depends(get_current_user)):
    """Create a new Objective."""
    async with async_session() as db:
        obj = OKRObjective(
            tenant_id=user.tenant_id,
            title=body.title,
            description=body.description,
            owner_type=body.owner_type,
            owner_id=uuid.UUID(body.owner_id) if body.owner_id else None,
            period_start=date.fromisoformat(body.period_start),
            period_end=date.fromisoformat(body.period_end),
        )
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return _obj_to_out(obj)


@router.patch("/objectives/{objective_id}", response_model=ObjectiveOut)
async def update_objective(
    objective_id: uuid.UUID,
    body: ObjectiveUpdate,
    user=Depends(get_current_user),
):
    """Update an Objective's title, description or status."""
    async with async_session() as db:
        result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        obj = result.scalar_one_or_none()
        if not obj:
            raise HTTPException(404, "Objective not found")

        if body.title is not None:
            obj.title = body.title
        if body.description is not None:
            obj.description = body.description
        if body.status is not None:
            obj.status = body.status

        await db.commit()
        await db.refresh(obj)
        return _obj_to_out(obj)


@router.delete("/objectives/{objective_id}")
async def delete_objective(
    objective_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Soft delete an Objective (set status to archived)."""
    async with async_session() as db:
        result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        obj = result.scalar_one_or_none()
        if not obj:
            raise HTTPException(404, "Objective not found")

        # Soft delete
        obj.status = "archived"
        await db.commit()

        return {"status": "success"}


# ─── Key Results ──────────────────────────────────────────────────────────────


@router.get(
    "/objectives/{objective_id}/key-results", response_model=list[KeyResultOut]
)
async def list_key_results(
    objective_id: uuid.UUID, user=Depends(get_current_user)
):
    """List all KRs for the given Objective."""
    async with async_session() as db:
        # Verify objective belongs to this tenant
        obj_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not obj_result.scalar_one_or_none():
            raise HTTPException(404, "Objective not found")

        result = await db.execute(
            select(OKRKeyResult)
            .where(OKRKeyResult.objective_id == objective_id)
            .order_by(OKRKeyResult.created_at)
        )
        return [_kr_to_out(kr) for kr in result.scalars().all()]


@router.post(
    "/objectives/{objective_id}/key-results", response_model=KeyResultOut
)
async def create_key_result(
    objective_id: uuid.UUID,
    body: KeyResultCreate,
    user=Depends(get_current_user),
):
    """Create a new Key Result under the specified Objective."""
    async with async_session() as db:
        # Verify objective belongs to this tenant
        obj_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not obj_result.scalar_one_or_none():
            raise HTTPException(404, "Objective not found")

        kr = OKRKeyResult(
            objective_id=objective_id,
            title=body.title,
            target_value=body.target_value,
            unit=body.unit,
            focus_ref=body.focus_ref,
        )
        db.add(kr)
        await db.commit()
        await db.refresh(kr)
        return _kr_to_out(kr)


@router.patch("/key-results/{kr_id}", response_model=KeyResultOut)
async def update_key_result(
    kr_id: uuid.UUID,
    body: KeyResultUpdate,
    user=Depends(get_current_user),
):
    """Update a Key Result's fields or current progress value.

    When current_value changes, an OKRProgressLog entry is created
    automatically to maintain the complete progress history.
    """
    async with async_session() as db:
        result = await db.execute(
            select(OKRKeyResult, OKRObjective)
            .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
            .where(
                OKRKeyResult.id == kr_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        row = result.first()
        if not row:
            raise HTTPException(404, "Key Result not found")
        kr, _ = row

        prev_value = kr.current_value

        if body.title is not None:
            kr.title = body.title
        if body.target_value is not None:
            kr.target_value = body.target_value
        if body.current_value is not None:
            kr.current_value = body.current_value
        if body.unit is not None:
            kr.unit = body.unit
        if body.focus_ref is not None:
            kr.focus_ref = body.focus_ref
        if body.status is not None:
            kr.status = body.status

        # Log progress change when current_value was updated
        if body.current_value is not None and body.current_value != prev_value:
            log = OKRProgressLog(
                kr_id=kr_id,
                previous_value=prev_value,
                new_value=body.current_value,
                source="manual",
            )
            db.add(log)

        await db.commit()
        await db.refresh(kr)
        return _kr_to_out(kr)


@router.post("/key-results/{kr_id}/progress", response_model=KeyResultOut)
async def update_kr_progress_endpoint(
    kr_id: uuid.UUID,
    body: ProgressUpdate,
    user=Depends(get_current_user),
):
    """Convenience endpoint for updating only the current progress value.

    Used by the update_kr_progress agent tool and the OKR Agent.
    Records an OKRProgressLog entry with the provided note.
    """
    async with async_session() as db:
        result = await db.execute(
            select(OKRKeyResult, OKRObjective)
            .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
            .where(
                OKRKeyResult.id == kr_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        row = result.first()
        if not row:
            raise HTTPException(404, "Key Result not found")
        kr, _ = row

        prev_value = kr.current_value
        kr.current_value = body.value
        kr.last_updated_at = datetime.utcnow()

        # Update status: use explicit override or auto-compute from progress ratio
        if body.status and body.status in ("on_track", "at_risk", "behind", "completed"):
            kr.status = body.status
        elif kr.target_value:
            ratio = body.value / kr.target_value
            if ratio >= 1.0:
                kr.status = "completed"
            elif ratio >= 0.7:
                kr.status = "on_track"
            elif ratio >= 0.4:
                kr.status = "at_risk"
            else:
                kr.status = "behind"

        log = OKRProgressLog(
            kr_id=kr_id,
            previous_value=prev_value,
            new_value=body.value,
            source="manual",
            note=body.note,
        )
        db.add(log)
        await db.commit()
        await db.refresh(kr)
        return _kr_to_out(kr)


@router.delete("/key-results/{kr_id}")
async def delete_key_result(
    kr_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Hard delete a key result."""
    from app.models.okr import OKRProgressLog
    async with async_session() as db:
        result = await db.execute(
            select(OKRKeyResult, OKRObjective)
            .join(OKRObjective, OKRKeyResult.objective_id == OKRObjective.id)
            .where(
                OKRKeyResult.id == kr_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        row = result.first()
        if not row:
            raise HTTPException(404, "Key Result not found")
        kr, _ = row

        # Manual cascade delete logs
        await db.execute(delete(OKRProgressLog).where(OKRProgressLog.kr_id == kr_id))
        await db.execute(delete(OKRKeyResult).where(OKRKeyResult.id == kr_id))
        
        await db.commit()
        return {"status": "success"}


# ─── Reports ──────────────────────────────────────────────────────────────────


@router.get("/reports", response_model=list[WorkReportOut])
async def list_reports(
    report_type: str | None = None,  # "daily" | "weekly" | None for both
    limit: int = 50,
    user=Depends(get_current_user),
):
    """List work reports for the current tenant, newest first."""
    async with async_session() as db:
        query = (
            select(WorkReport)
            .where(WorkReport.tenant_id == user.tenant_id)
            .order_by(WorkReport.period_date.desc(), WorkReport.created_at.desc())
            .limit(limit)
        )
        if report_type:
            query = query.where(WorkReport.report_type == report_type)

        result = await db.execute(query)
        reports = result.scalars().all()

    return [
        WorkReportOut(
            id=str(r.id),
            author_type=r.author_type,
            author_id=str(r.author_id),
            report_type=r.report_type,
            period_date=r.period_date.isoformat(),
            content=r.content,
            source=r.source,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in reports
    ]


# ─── Progress Log (P3) ────────────────────────────────────────────────────────


@router.get("/key-results/{kr_id}/progress-log", response_model=list[ProgressLogOut])
async def get_kr_progress_log(
    kr_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Return the full progress history for a single Key Result.

    Results are ordered oldest-first so the frontend can render a
    time-series line chart directly from the response.
    """
    async with async_session() as db:
        # Verify the KR belongs to this tenant via the parent Objective
        kr_result = await db.execute(
            select(OKRKeyResult).where(OKRKeyResult.id == kr_id)
        )
        kr = kr_result.scalar_one_or_none()
        if not kr:
            raise HTTPException(404, "Key Result not found")

        # Check tenant ownership through parent objective
        obj_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == kr.objective_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not obj_result.scalar_one_or_none():
            raise HTTPException(403, "Access denied")

        logs_result = await db.execute(
            select(OKRProgressLog)
            .where(OKRProgressLog.kr_id == kr_id)
            .order_by(OKRProgressLog.created_at.asc())
        )
        logs = logs_result.scalars().all()

    return [
        ProgressLogOut(
            id=str(lg.id),
            kr_id=str(lg.kr_id),
            previous_value=lg.previous_value,
            new_value=lg.new_value,
            source=lg.source,
            note=lg.note,
            created_at=lg.created_at.isoformat() if lg.created_at else "",
        )
        for lg in logs
    ]


# ─── Alignments (P3) ─────────────────────────────────────────────────────────


@router.post("/alignments", response_model=AlignmentOut)
async def create_alignment(
    body: AlignmentCreate,
    user=Depends(get_current_user),
):
    """Create an alignment relationship between two OKR entities.

    Source is typically an individual's Objective; target is a company
    Objective or Key Result they wish to align toward.
    """
    async with async_session() as db:
        # Validate source objective belongs to this tenant
        src_obj = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == uuid.UUID(body.source_id),
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not src_obj.scalar_one_or_none():
            raise HTTPException(404, "Source Objective not found")

        # Check for duplicate
        existing = await db.execute(
            select(OKRAlignment).where(
                OKRAlignment.source_type == body.source_type,
                OKRAlignment.source_id == uuid.UUID(body.source_id),
                OKRAlignment.target_type == body.target_type,
                OKRAlignment.target_id == uuid.UUID(body.target_id),
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(409, "Alignment already exists")

        # Resolve target title for the response
        target_title = ""
        if body.target_type == "objective":
            tgt_result = await db.execute(
                select(OKRObjective.title).where(
                    OKRObjective.id == uuid.UUID(body.target_id),
                    OKRObjective.tenant_id == user.tenant_id,
                )
            )
            row = tgt_result.scalar_one_or_none()
            target_title = row or ""

        alignment = OKRAlignment(
            source_type=body.source_type,
            source_id=uuid.UUID(body.source_id),
            target_type=body.target_type,
            target_id=uuid.UUID(body.target_id),
        )
        db.add(alignment)
        await db.commit()
        await db.refresh(alignment)

    return AlignmentOut(
        id=str(alignment.id),
        source_type=alignment.source_type,
        source_id=str(alignment.source_id),
        target_type=alignment.target_type,
        target_id=str(alignment.target_id),
        target_title=target_title,
        created_at=alignment.created_at.isoformat() if alignment.created_at else "",
    )


@router.delete("/alignments/{alignment_id}")
async def delete_alignment(
    alignment_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Remove an alignment relationship.

    Only members of the same tenant may delete alignments.
    """
    async with async_session() as db:
        result = await db.execute(
            select(OKRAlignment).where(OKRAlignment.id == alignment_id)
        )
        al = result.scalar_one_or_none()
        if not al:
            raise HTTPException(404, "Alignment not found")

        # Verify tenant ownership via source objective
        src_result = await db.execute(
            select(OKRObjective).where(
                OKRObjective.id == al.source_id,
                OKRObjective.tenant_id == user.tenant_id,
            )
        )
        if not src_result.scalar_one_or_none():
            raise HTTPException(403, "Access denied")

        await db.execute(delete(OKRAlignment).where(OKRAlignment.id == alignment_id))
        await db.commit()

    return {"status": "deleted"}


# ─── P4: Member OKR Outreach ──────────────────────────────────────────────────


class MemberWithoutOKR(BaseModel):
    """A single member (user or agent) who has no Objective for the current period."""
    type: str          # "user" | "agent"
    id: str
    name: str
    avatar_url: str
    channel: str | None = None       # e.g. "feishu", "dingtalk", or None for platform-only users
    channel_user_id: str | None = None  # channel-specific user ID for direct messaging


class MembersWithoutOKROut(BaseModel):
    period_start: str
    period_end: str
    company_okr_exists: bool   # False → hide nudge button on frontend
    okr_agent_id: str | None   # For the Settings page to build the chat link
    members: list[MemberWithoutOKR]


class TriggerOutreachResponse(BaseModel):
    triggered: bool
    message: str
    member_count: int


@router.get("/members-without-okr", response_model=MembersWithoutOKROut)
async def members_without_okr(user=Depends(get_current_user)):
    """Return all org members who have no Objective set for the current period.

    Also returns:
    - company_okr_exists: whether the nudge button should be shown
    - okr_agent_id: the OKR Agent's UUID, used by frontend to build the Chat link

    Includes both platform users and Agents (excluding system agents).
    """
    from app.models.user import User
    from app.models.agent import Agent

    async with async_session() as db:
        # Determine current period
        settings = await _get_or_create_settings(db, user.tenant_id)
        ps, pe = _compute_current_period(
            settings.period_frequency, settings.period_length_days
        )

        # Check whether company-level OKRs exist (gate for the nudge button)
        company_obj_result = await db.execute(
            select(OKRObjective.id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.owner_type == "company",
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
            ).limit(1)
        )
        company_okr_exists = company_obj_result.scalar_one_or_none() is not None

        # Collect all owner IDs that already have an Objective this period
        existing_result = await db.execute(
            select(OKRObjective.owner_id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
                OKRObjective.owner_id.isnot(None),
            )
        )
        owners_with_okr: set[uuid.UUID] = {row[0] for row in existing_result.all()}

        # Fetch all non-admin platform users in this tenant
        users_result = await db.execute(
            select(User).where(
                User.tenant_id == user.tenant_id,
                User.is_active == True,
                User.role.notin_(["platform_admin"]),
            )
        )
        all_users = users_result.scalars().all()

        # Fetch all non-system agents in this tenant
        agents_result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == user.tenant_id,
                Agent.is_system == False,
                Agent.status.notin_(["archived", "stopped"]),
            )
        )
        all_agents = agents_result.scalars().all()

        # Find OKR Agent for the settings-page Chat link
        okr_agent_result = await db.execute(
            select(Agent.id).where(
                Agent.tenant_id == user.tenant_id,
                Agent.name == "OKR Agent",
            ).limit(1)
        )
        okr_agent_id_row = okr_agent_result.scalar_one_or_none()
        okr_agent_id_str = str(okr_agent_id_row) if okr_agent_id_row else None

        await db.commit()

    members: list[MemberWithoutOKR] = []

    # Platform users without OKR
    for u in all_users:
        if u.id not in owners_with_okr:
            members.append(MemberWithoutOKR(
                type="user",
                id=str(u.id),
                name=u.full_name or u.email or "Unknown",
                avatar_url=u.avatar_url or "",
                # channel info can be extended later when org-sync stores channel_user_id
                channel=None,
                channel_user_id=None,
            ))

    # Agents without OKR
    for ag in all_agents:
        if ag.id not in owners_with_okr:
            members.append(MemberWithoutOKR(
                type="agent",
                id=str(ag.id),
                name=ag.name,
                avatar_url=ag.avatar_url or "",
                channel=None,
                channel_user_id=None,
            ))

    return MembersWithoutOKROut(
        period_start=ps.isoformat(),
        period_end=pe.isoformat(),
        company_okr_exists=company_okr_exists,
        okr_agent_id=okr_agent_id_str,
        members=members,
    )


@router.post("/trigger-member-outreach", response_model=TriggerOutreachResponse)
async def trigger_member_outreach(user=Depends(get_current_user)):
    """Trigger the OKR Agent to reach out to all members without an OKR.

    Only available when company-level OKRs already exist.
    The OKR Agent runs asynchronously — the response returns immediately
    after the task is queued.

    The Agent will:
    - For platform users: send a web notification via send_web_message
    - For Agent members: send a one-shot message via send_message_to_agent
      asking them to reply with their proposed OKR (company OKR provided as context)
    - For channel users (Feishu, etc.): send a channel message if configured;
      otherwise notify Admin to configure the channel bot
    """
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can trigger OKR outreach")

    from app.models.agent import Agent

    async with async_session() as db:
        # Guard: require company OKR to exist first
        settings = await _get_or_create_settings(db, user.tenant_id)
        ps, pe = _compute_current_period(
            settings.period_frequency, settings.period_length_days
        )
        company_obj_result = await db.execute(
            select(OKRObjective.id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.owner_type == "company",
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
            ).limit(1)
        )
        if not company_obj_result.scalar_one_or_none():
            raise HTTPException(
                400,
                "Company OKR must be set before triggering member outreach. "
                "Please chat with the OKR Agent to establish company Objectives first."
            )

        # Collect members without OKR this period
        from app.models.user import User
        existing_result = await db.execute(
            select(OKRObjective.owner_id).where(
                OKRObjective.tenant_id == user.tenant_id,
                OKRObjective.period_start >= ps,
                OKRObjective.period_end <= pe,
                OKRObjective.status != "archived",
                OKRObjective.owner_id.isnot(None),
            )
        )
        owners_with_okr: set[uuid.UUID] = {row[0] for row in existing_result.all()}

        users_result = await db.execute(
            select(User).where(
                User.tenant_id == user.tenant_id,
                User.is_active == True,
                User.role.notin_(["platform_admin"]),
            )
        )
        agents_result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == user.tenant_id,
                Agent.is_system == False,
                Agent.status.notin_(["archived", "stopped"]),
            )
        )

        users_without_okr = [u for u in users_result.scalars().all() if u.id not in owners_with_okr]
        agents_without_okr = [a for a in agents_result.scalars().all() if a.id not in owners_with_okr]

        # Find OKR Agent
        okr_agent_result = await db.execute(
            select(Agent).where(
                Agent.tenant_id == user.tenant_id,
                Agent.name == "OKR Agent",
            ).limit(1)
        )
        okr_agent = okr_agent_result.scalar_one_or_none()
        if not okr_agent:
            raise HTTPException(404, "OKR Agent not found. Please contact platform admin.")

        await db.commit()

    total_members = len(users_without_okr) + len(agents_without_okr)
    if total_members == 0:
        return TriggerOutreachResponse(
            triggered=False,
            message="All members already have OKRs set for this period.",
            member_count=0,
        )

    # Build the task prompt for OKR Agent's async LLM run.
    # We pass enough context so the Agent can compose the messages without
    # needing to call get_okr (reducing latency for the outreach session).
    user_lines = [f"- {u.full_name or u.email} (user_id={u.id})" for u in users_without_okr]
    agent_lines = [f"- {a.name} (agent_id={a.id})" for a in agents_without_okr]

    outreach_prompt = f"""# Task: OKR Member Outreach

You have been asked by an admin to contact all team members who have not yet set
their individual OKRs for the current period ({ps.isoformat()} – {pe.isoformat()}).

## Members Without OKR

### Platform Users ({len(users_without_okr)})
{chr(10).join(user_lines) if user_lines else '(none)'}

### Agent Colleagues ({len(agents_without_okr)})
{chr(10).join(agent_lines) if agent_lines else '(none)'}

## Your Task

1. Call `get_okr` to retrieve the current company OKRs (you will share this as context).

2. For each **platform user** in the list above:
   - Call `send_web_message` to send them a friendly notification.
   - Message should: (a) mention that company OKRs are ready, (b) invite them to
     chat with you (the OKR Agent) to define their personal OKRs, OR to add their
     OKRs directly on the OKR page if they prefer.

3. For each **Agent colleague** in the list above:
   - Call `send_message_to_agent` with a single detailed message that includes:
     (a) the full company OKR text you got from step 1,
     (b) a request for them to deeply think about their role's contribution and
         reply in ONE message with their proposed O (title + description) and
         each KR (title, target value, unit). You will then create the OKRs on
         their behalf using your tools.
   - After they reply (in a future heartbeat or trigger session), parse their
     response and call `create_objective` and `create_key_result` to record it.

4. If there are any channel-synced users (e.g. Feishu users) you cannot contact
   because the OKR Agent has no Feishu channel configured, send a `send_web_message`
   to the admin listing those unreachable users and asking them to configure
   the channel bot for the OKR Agent.

5. After completing all outreach, post a brief summary to Plaza using
   `plaza_create_post` so the team knows OKR setup is underway.

Be warm and supportive in tone — this is an invitation, not a demand.
"""

    # Fire OKR Agent asynchronously — reuse the heartbeat execution path
    # which already handles the full LLM loop with tools.
    import asyncio
    from app.services.heartbeat import _execute_heartbeat

    # Patch: pass a one-shot prompt instead of the standard heartbeat instruction.
    # We do this by temporarily writing the task to a temporary HEARTBEAT.md-like
    # mechanism through the agent's workspace, then triggering the heartbeat.
    # Simpler approach: import and call _run_agent_task directly.
    asyncio.create_task(
        _run_okr_outreach_task(okr_agent.id, outreach_prompt)
    )

    return TriggerOutreachResponse(
        triggered=True,
        message=f"OKR Agent is reaching out to {total_members} member(s). Check back in a few minutes.",
        member_count=total_members,
    )


async def _run_okr_outreach_task(agent_id: uuid.UUID, task_prompt: str) -> None:
    """Run the OKR Agent with a specific outreach task prompt.

    Reuses the same LLM loop as the heartbeat but with a custom prompt
    instead of the HEARTBEAT.md content. Runs as a background asyncio task.
    """
    try:
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.llm import LLMModel
        from app.services.agent_context import build_agent_context
        from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
        from app.services.llm_utils import create_llm_client, get_max_tokens, LLMMessage, LLMError
        from app.services.token_tracker import record_token_usage, extract_usage_tokens, estimate_tokens_from_chars
        import json

        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.error(f"[OKR Outreach] Agent {agent_id} not found")
                return

            model_id = agent.primary_model_id or agent.fallback_model_id
            if not model_id:
                logger.error(f"[OKR Outreach] OKR Agent has no model configured")
                return

            model_result = await db.execute(select(LLMModel).where(LLMModel.id == model_id))
            model = model_result.scalar_one_or_none()
            if not model:
                logger.error(f"[OKR Outreach] Model {model_id} not found")
                return

            agent_name = agent.name
            agent_role = agent.role_description or ""
            agent_creator_id = agent.creator_id
            model_provider = model.provider
            model_api_key = model.api_key_encrypted
            model_model = model.model
            model_base_url = model.base_url
            model_temperature = model.temperature
            model_max_output_tokens = getattr(model, "max_output_tokens", None)
            model_request_timeout = getattr(model, "request_timeout", None)

            static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent_role)
            await db.commit()

        client = create_llm_client(
            provider=model_provider,
            api_key=model_api_key,
            model=model_model,
            base_url=model_base_url,
            timeout=float(model_request_timeout or 120.0),
        )

        tools_for_llm = await get_agent_tools_for_llm(agent_id)
        llm_messages = [
            LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
            LLMMessage(role="user", content=task_prompt),
        ]

        _accumulated_tokens = 0

        for _round in range(30):  # Allow more rounds for multi-member outreach
            try:
                response = await client.complete(
                    messages=llm_messages,
                    tools=tools_for_llm,
                    temperature=model_temperature,
                    max_tokens=get_max_tokens(model_provider, model_model, model_max_output_tokens),
                )
            except LLMError as e:
                logger.error(f"[OKR Outreach] LLM error: {e}")
                break
            except Exception as e:
                logger.error(f"[OKR Outreach] LLM call error: {e}")
                break

            real_tokens = extract_usage_tokens(response.usage)
            _accumulated_tokens += real_tokens if real_tokens else estimate_tokens_from_chars(
                sum(len(m.content or "") for m in llm_messages) + len(response.content or "")
            )

            if response.tool_calls:
                llm_messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=[{"id": tc["id"], "type": "function", "function": tc["function"]} for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                ))
                for tc in response.tool_calls:
                    fn = tc["function"]
                    try:
                        args = json.loads(fn.get("arguments", "{}") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tool_result = await execute_tool(fn["name"], args, agent_id, agent_creator_id)
                    llm_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=tc["id"],
                        content=str(tool_result),
                    ))
            else:
                break  # No more tool calls — task complete

        await client.close()

        # Record token usage
        if _accumulated_tokens > 0:
            await record_token_usage(agent_id, _accumulated_tokens)

        logger.info(f"[OKR Outreach] Completed outreach task for agent {agent_name}")

    except Exception as e:
        logger.exception(f"[OKR Outreach] Unexpected error for agent {agent_id}: {e}")
