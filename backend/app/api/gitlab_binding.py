"""GitLab agent binding API — one token + one project per agent (pure git CLI).

See docs/technical-plans/20260820-gitlab-agent-binding for the full design.
Sensitive fields (the PAT) are encrypted at rest and NEVER returned by GET.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import decrypt_data, encrypt_data, get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User

router = APIRouter(prefix="/agents/{agent_id}/gitlab-binding", tags=["gitlab-binding"])

DEFAULT_BRANCH = "f_android_ai"
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


class GitlabBindingPut(BaseModel):
    token: str | None = None  # 首次绑定必填；已有绑定时 None = 保留旧 token
    project_path: str
    default_branch: str | None = None

    @field_validator("project_path")
    @classmethod
    def _project_path_valid(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or any(ch.isspace() for ch in v):
            raise ValueError("project_path 不能为空且不能含空白字符")
        return v

    @field_validator("default_branch")
    @classmethod
    def _branch_valid(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v or not _BRANCH_RE.match(v):
            raise ValueError("default_branch 必须是合法分支名")
        return v

    @field_validator("token")
    @classmethod
    def _token_valid(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 100:
            raise ValueError("token 长度超限")
        return v


class GitlabBindingOut(BaseModel):
    configured: bool
    project_path: str = ""
    default_branch: str = DEFAULT_BRANCH
    has_token: bool = False
    init_status: str = "pending"
    init_error: str | None = None
    init_updated_at: str | None = None


def _to_response(config: ChannelConfig | None) -> GitlabBindingOut:
    if not config:
        return GitlabBindingOut(configured=False)
    extra = config.extra_config or {}
    return GitlabBindingOut(
        configured=bool(config.is_configured),
        project_path=str(extra.get("project_path") or ""),
        default_branch=str(extra.get("default_branch") or DEFAULT_BRANCH),
        has_token=bool(config.app_secret),
        init_status=str(extra.get("init_status") or "pending"),
        init_error=extra.get("init_error"),
        init_updated_at=extra.get("init_updated_at"),
    )


async def _require_manage(db: AsyncSession, current_user: User, agent_id: uuid.UUID) -> None:
    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level not in ("manage",) and current_user.role not in (
        "platform_admin",
        "org_admin",
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage access required for GitLab binding",
        )


async def _load_binding(db: AsyncSession, agent_id: uuid.UUID) -> ChannelConfig | None:
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "gitlab",
        )
    )
    return result.scalar_one_or_none()


@router.get("/")
async def get_binding(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage(db, current_user, agent_id)
    return _to_response(await _load_binding(db, agent_id))


@router.put("/")
async def put_binding(
    agent_id: uuid.UUID,
    data: GitlabBindingPut,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage(db, current_user, agent_id)

    settings = get_settings()
    default_branch = data.default_branch or DEFAULT_BRANCH
    existing = await _load_binding(db, agent_id)

    # 首次绑定时 token 必填
    if existing is None and not data.token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="首次绑定必须提供 token",
        )

    plaintext_token: str | None = data.token
    if plaintext_token:
        app_secret = encrypt_data(plaintext_token, settings.SECRET_KEY)
    elif existing and existing.app_secret:
        app_secret = existing.app_secret
        try:
            plaintext_token = decrypt_data(app_secret, settings.SECRET_KEY)
        except Exception:
            plaintext_token = None
    else:
        app_secret = None

    if existing:
        existing.app_secret = app_secret or existing.app_secret
        existing.is_configured = True
        extra = dict(existing.extra_config or {})
        extra["project_path"] = data.project_path
        extra["default_branch"] = default_branch
        extra["init_status"] = "pending"
        extra["init_error"] = None
        existing.extra_config = extra
    else:
        existing = ChannelConfig(
            agent_id=agent_id,
            channel_type="gitlab",
            app_id="gitlab",
            app_secret=app_secret,
            is_configured=True,
            extra_config={
                "project_path": data.project_path,
                "default_branch": default_branch,
                "init_status": "pending",
            },
        )
        db.add(existing)
    await db.commit()

    if plaintext_token:
        from app.services.gitlab_workspace import run_gitlab_workspace_init

        asyncio.create_task(run_gitlab_workspace_init(agent_id, data.project_path, default_branch, plaintext_token))
        logger.info(
            "[GitLabBinding] init scheduled agent={} project={}",
            agent_id,
            data.project_path,
        )
    return {"ok": True}


@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage(db, current_user, agent_id)

    existing = await _load_binding(db, agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Binding not found")

    settings = get_settings()

    # 移除工作区仓库里的凭证重写（文件与 .git 保留）
    if existing.app_secret:
        try:
            pat = decrypt_data(existing.app_secret, settings.SECRET_KEY)
            from app.services.gitlab_workspace import (
                _credential_rewrite,
                repo_root,
            )

            root = repo_root(agent_id)
            if (root / ".git").exists():
                rewrite = _credential_rewrite(settings.GITLAB_BASE_URL, pat)
                from app.services.gitlab_workspace import _run_git

                await _run_git(
                    ["config", "--local", "--unset", f"url.{rewrite}.insteadOf"],
                    cwd=root,
                    pat=pat,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "[GitLabBinding] unset credential rewrite failed agent={}: {}",
                agent_id,
                str(exc)[:200],
            )

    existing.app_secret = None
    existing.is_configured = False
    extra = dict(existing.extra_config or {})
    extra["init_status"] = "unbound"
    extra["init_error"] = None
    extra["init_updated_at"] = datetime.now(timezone.utc).isoformat()
    existing.extra_config = extra
    await db.commit()
