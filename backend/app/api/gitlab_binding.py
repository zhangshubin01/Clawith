"""GitLab agent binding API — one token + one project per agent (pure git CLI).

See docs/technical-plans/20260820-gitlab-agent-binding for the full design.
Sensitive fields (the PAT) are encrypted at rest and NEVER returned by GET.
"""

import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import decrypt_data, encrypt_data, get_current_user
from app.database import get_db
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.gitlab_workspace import init_in_flight, schedule_gitlab_workspace_init

router = APIRouter(prefix="/agents/{agent_id}/gitlab-binding", tags=["gitlab-binding"])

DEFAULT_BRANCH = "f_android_ai"
_BRANCH_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


class GitlabBindingPut(BaseModel):
    token: str | None = None  # 首次绑定必填；已有绑定时 None = 保留旧 token
    project_path: str  # group/repo 形式，或完整 URL（http(s)://host/group/repo）
    default_branch: str | None = None
    base_url: str | None = None  # 派生字段：project_path 为完整 URL 时 = scheme://host[:port]

    @field_validator("project_path")
    @classmethod
    def _project_path_valid(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if not v or any(ch.isspace() for ch in v):
            raise ValueError("project_path 不能为空且不能含空白字符")
        if "://" in v and urlparse(v).scheme.lower() not in ("http", "https"):
            raise ValueError(
                "project_path 只需 group/repo 形式（例如 zhangshubin/my-repo）"
                "或 http(s) 完整 URL（例如 http://192.168.5.254/zhangshubin/my-repo）"
            )
        return v

    @model_validator(mode="after")
    def _split_full_url(self) -> "GitlabBindingPut":
        # base_url 是派生字段，客户端传入值一律丢弃（信任边界：只认 project_path 本体）
        self.base_url = None
        raw = self.project_path
        if raw.lower().startswith(("http://", "https://")):
            parsed = urlparse(raw)
            if parsed.username or parsed.password:
                raise ValueError("project_path 的 URL 不能内嵌账号密码")
            if parsed.query or parsed.fragment:
                raise ValueError("project_path 的 URL 不能带 query/fragment")
            if not parsed.netloc:
                raise ValueError("project_path 的 URL 缺少主机名")
            self.project_path = parsed.path.strip("/")
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"
        # 最后一段将作为工作区子目录名，必须文件系统安全（裸路径与 URL 路径共用）
        v = self.project_path
        last = v.rsplit("/", 1)[-1]
        if last.endswith(".git"):
            if last == ".git":
                last = ""  # 保留名，走下方拒绝逻辑
            else:
                v = v[:-4]  # 容忍用户粘贴 .git 后缀，规范化去掉
                last = last[:-4]
        if not last or not re.fullmatch(r"[\w.-]+", last, re.UNICODE) or last in {".", "..", ".tmp"}:
            raise ValueError("project_path 的最后一段（项目名）只能含字母/数字/_/./-，且不能是 . .. .git .tmp")
        self.project_path = v
        return self

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
    base_url: str | None = None  # 显式绑定的 GitLab 实例；None = 用全局 GITLAB_BASE_URL
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
        base_url=extra.get("base_url"),
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
    decrypt_failed = False
    if plaintext_token:
        app_secret = encrypt_data(plaintext_token, settings.SECRET_KEY)
    elif existing and existing.app_secret:
        app_secret = existing.app_secret
        try:
            plaintext_token = decrypt_data(app_secret, settings.SECRET_KEY)
        except Exception:
            plaintext_token = None
            decrypt_failed = True
    else:
        app_secret = None

    # 终态语义：解密失败/无 token 属配置错误，fail-early 置 failed（不得静默跳过 init）
    init_status = "pending"
    init_error: str | None = None
    if decrypt_failed:
        init_status, init_error = "failed", "Token 解密失败（SECRET_KEY 可能已变更），请重新填写 GitLab Token"
    elif not plaintext_token:
        init_status, init_error = "failed", "未配置 Token，无法初始化仓库"

    if existing:
        existing.app_secret = app_secret or existing.app_secret
        existing.is_configured = True
        extra = dict(existing.extra_config or {})
        extra["project_path"] = data.project_path
        extra["base_url"] = data.base_url
        extra["default_branch"] = default_branch
        # 配置错误（解密失败/无 token）必须立即落库压过一切；否则有在途 init 任务时不重置
        # 状态（在途任务的终态写回是权威），仅在无在途任务时写 pending。
        if init_status == "failed":
            extra["init_status"] = "failed"
            extra["init_error"] = init_error
        elif not init_in_flight(agent_id):
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
                "base_url": data.base_url,
                "default_branch": default_branch,
                "init_status": init_status,
                "init_error": init_error,
            },
        )
        db.add(existing)
    await db.commit()

    if plaintext_token:
        scheduled = schedule_gitlab_workspace_init(
            agent_id, data.project_path, default_branch, plaintext_token, data.base_url
        )
        logger.info(
            "[GitLabBinding] init scheduled={} agent={} project={}",
            scheduled,
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
                _repo_dir_name,
                repo_root,
            )

            extra = existing.extra_config or {}
            base_url = str(extra.get("base_url") or settings.GITLAB_BASE_URL)
            rewrite = _credential_rewrite(base_url, pat)
            project_path = str(extra.get("project_path") or "")
            candidates = [repo_root(agent_id)]
            if project_path:
                try:
                    candidates.insert(0, repo_root(agent_id) / _repo_dir_name(project_path))
                except ValueError:
                    pass  # 路径非法时兜底只清理旧布局根目录
            for cand in candidates:
                if (cand / ".git").exists():
                    from app.services.gitlab_workspace import _run_git

                    await _run_git(
                        ["config", "--local", "--unset", f"url.{rewrite}.insteadOf"],
                        cwd=cand,
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
