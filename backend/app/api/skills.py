"""Skills API — global skill registry CRUD."""

import asyncio
import base64
import io
import os
import re
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.core.security import get_current_admin, get_current_user, require_role
from app.models.user import User

router = APIRouter(prefix="/skills", tags=["skills"])

CLAWHUB_BASE = os.getenv("CLAWHUB_BASE", "https://clawhub.ai/api").rstrip("/")
CLAWHUB_MIRROR_BASE = os.getenv("CLAWHUB_MIRROR_BASE", "https://cn.clawhub-mirror.com/api").rstrip("/")
GITHUB_API = "https://api.github.com"

MAX_SKILL_SIZE = 5_242_880  # 5 MB total limit per import (supports multi-skill repos)


async def _get_user_setting(user_id: str | None, key: str) -> str:
    """Resolve a user setting value: user_settings DB > empty."""
    if user_id:
        try:
            from app.models.user_setting import UserSetting
            import uuid as _uid
            async with async_session() as db:
                result = await db.execute(
                    select(UserSetting).where(
                        UserSetting.user_id == _uid.UUID(user_id),
                        UserSetting.key == key,
                    )
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value:
                    v = setting.value
                    if key.endswith("_password"):
                        return v.get("password", "")
                    if key.endswith("_username"):
                        return v.get("username", "")
                    return v.get("token", "")
        except Exception:
            logger.debug("[Skills] Failed to get user setting")
    return ""


async def _get_tenant_setting(tenant_id: str | None, key: str) -> str:
    """Resolve a tenant setting value: tenant_settings DB > empty."""
    if tenant_id:
        try:
            from app.models.tenant_setting import TenantSetting
            import uuid as _uid
            async with async_session() as db:
                result = await db.execute(
                    select(TenantSetting).where(
                        TenantSetting.tenant_id == _uid.UUID(tenant_id),
                        TenantSetting.key == key,
                    )
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value:
                    v = setting.value
                    if key.endswith("_password"):
                        return v.get("password", "")
                    if key.endswith("_username"):
                        return v.get("username", "")
                    return v.get("token", "")
        except Exception:
            logger.debug("[Skills] Failed to get tenant setting")
    return ""


async def _get_github_token(tenant_id: str | None = None) -> str:
    """Resolve GitHub token from tenant settings DB."""
    return await _get_tenant_setting(tenant_id, "github_token")


async def _get_clawhub_key(tenant_id: str | None = None) -> str:
    """Resolve ClawHub API key from tenant settings DB."""
    return await _get_tenant_setting(tenant_id, "clawhub_key")


async def _resolve_gitlab_auth(user_id: str | None, tenant_id: str | None) -> dict:
    """Resolve GitLab credentials with user-level priority over tenant-level.
    Returns {"type": "pat"|"basic", ...} or {} for anonymous."""
    # User-level PAT
    token = await _get_user_setting(user_id, "gitlab_token")
    if token:
        return {"type": "pat", "token": token}
    # User-level basic auth
    u = await _get_user_setting(user_id, "gitlab_username")
    p = await _get_user_setting(user_id, "gitlab_password")
    if u and p:
        return {"type": "basic", "username": u, "password": p}
    # Tenant-level PAT
    token = await _get_tenant_setting(tenant_id, "gitlab_token")
    if token:
        return {"type": "pat", "token": token}
    # Tenant-level basic auth
    u = await _get_tenant_setting(tenant_id, "gitlab_username")
    p = await _get_tenant_setting(tenant_id, "gitlab_password")
    if u and p:
        return {"type": "basic", "username": u, "password": p}
    return {}


def _build_gitlab_auth_headers(auth: dict) -> dict:
    """Build HTTP headers from resolved GitLab credentials."""
    if not auth:
        return {}
    if auth["type"] == "pat":
        return {"PRIVATE-TOKEN": auth["token"]}
    if auth["type"] == "basic":
        creds = base64.b64encode(f"{auth['username']}:{auth['password']}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {}


def _clawhub_headers(api_key: str) -> dict:
    """Build request headers for ClawHub API calls."""
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _clawhub_headers_for_base(api_key: str, base_url: str) -> dict:
    """Only send the official ClawHub API key to official ClawHub endpoints."""
    if "clawhub-mirror.com" in base_url:
        return {}
    return _clawhub_headers(api_key)


def _candidate_clawhub_bases(preferred: str | None = None) -> list[str]:
    """Return ClawHub API bases in fallback order without duplicates."""
    bases = [preferred, CLAWHUB_BASE, CLAWHUB_MIRROR_BASE]
    result: list[str] = []
    for base in bases:
        if not base:
            continue
        normalized = base.rstrip("/")
        if normalized not in result:
            result.append(normalized)
    return result


def _clawhub_search_endpoint(base_url: str) -> str:
    """The China mirror serves search under /v1/search, while clawhub.ai keeps /search."""
    if "clawhub-mirror.com" in base_url:
        return f"{base_url}/v1/search"
    return f"{base_url}/search"


def _clawhub_skill_url(base_url: str, slug: str) -> str:
    return f"{base_url}/v1/skills/{slug}"


def _clawhub_download_url(base_url: str) -> str:
    return f"{base_url}/v1/download"


def _public_clawhub_url(base_url: str, slug: str) -> str:
    if "clawhub-mirror.com" in base_url:
        return f"https://cn.clawhub-mirror.com/skills/{slug}"
    return f"https://clawhub.ai/skills/{slug}"


def _extract_clawhub_zip_files(data: bytes) -> list[dict]:
    """Convert a ClawHub skill zip into [{"path", "content"}] records."""
    files: list[dict] = []
    total_size = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(502, "ClawHub download did not return a valid skill archive") from exc

    entries = [info for info in archive.infolist() if not info.is_dir()]
    raw_paths = [Path(info.filename) for info in entries]
    strip_prefix = ""
    if raw_paths:
        first_parts = raw_paths[0].parts
        if len(first_parts) > 1:
            candidate = first_parts[0]
            has_root_skill = any(p.name.upper() == "SKILL.MD" and len(p.parts) == 1 for p in raw_paths)
            has_prefixed_skill = any(
                len(p.parts) > 1 and p.parts[0] == candidate and p.parts[-1].upper() == "SKILL.MD"
                for p in raw_paths
            )
            all_share_prefix = all(len(p.parts) > 1 and p.parts[0] == candidate for p in raw_paths)
            if not has_root_skill and has_prefixed_skill and all_share_prefix:
                strip_prefix = f"{candidate}/"

    for info in entries:
        rel = info.filename.lstrip("/")
        if strip_prefix and rel.startswith(strip_prefix):
            rel = rel[len(strip_prefix):]
        path = Path(rel)
        if not rel or path.is_absolute() or ".." in path.parts:
            continue

        total_size += info.file_size
        if total_size > MAX_SKILL_SIZE:
            raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")

        content = archive.read(info).decode("utf-8", errors="replace")
        files.append({"path": rel, "content": content})

    if not any(f["path"].upper() == "SKILL.MD" for f in files):
        raise HTTPException(400, "No SKILL.md found in ClawHub archive — not a valid skill package")
    return files


async def _fetch_clawhub_json(
    path_builder,
    api_key: str = "",
    preferred_base: str | None = None,
    params: dict | None = None,
) -> tuple[dict, str]:
    """Fetch JSON from ClawHub, falling back to the mirror when available."""
    last_error = ""
    for base_url in _candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    path_builder(base_url),
                    params=params,
                    headers=_clawhub_headers_for_base(api_key, base_url),
                )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 404:
                last_error = f"ClawHub not found at {base_url}"
                continue
            if resp.status_code == 429:
                last_error = f"ClawHub rate limit exceeded at {base_url}"
                continue
            if resp.status_code == 200 and "json" in content_type:
                return resp.json(), base_url
            last_error = f"ClawHub API error from {base_url}: HTTP {resp.status_code}"
        except HTTPException:
            raise
        except Exception as exc:
            last_error = f"Failed to connect to ClawHub at {base_url}: {exc}"
    if "rate limit" in last_error:
        raise HTTPException(429, "ClawHub rate limit exceeded. Please wait a moment and try again.")
    raise HTTPException(502, last_error or "Failed to connect to ClawHub")


async def _fetch_clawhub_skill_meta(
    slug: str,
    api_key: str = "",
    preferred_base: str | None = None,
) -> tuple[dict, str]:
    return await _fetch_clawhub_json(
        lambda base_url: _clawhub_skill_url(base_url, slug),
        api_key=api_key,
        preferred_base=preferred_base,
    )


async def _fetch_clawhub_skill_archive(
    slug: str,
    api_key: str = "",
    preferred_base: str | None = None,
    version: str | None = None,
    tag: str | None = None,
) -> tuple[list[dict], str]:
    """Download a ClawHub skill archive from official API or mirror."""
    params = {"slug": slug}
    if version:
        params["version"] = version
    if tag:
        params["tag"] = tag

    last_error = ""
    for base_url in _candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                resp = await client.get(
                    _clawhub_download_url(base_url),
                    params=params,
                    headers=_clawhub_headers_for_base(api_key, base_url),
                )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 404:
                last_error = f"Skill '{slug}' not found on ClawHub at {base_url}"
                continue
            if resp.status_code == 429:
                last_error = f"ClawHub rate limit exceeded at {base_url}"
                continue
            if resp.status_code == 200 and ("zip" in content_type or resp.content.startswith(b"PK")):
                return _extract_clawhub_zip_files(resp.content), base_url
            last_error = f"ClawHub download failed from {base_url}: HTTP {resp.status_code}"
        except HTTPException:
            raise
        except Exception as exc:
            last_error = f"Failed to download ClawHub skill from {base_url}: {exc}"
    if "rate limit" in last_error:
        raise HTTPException(429, "ClawHub rate limit exceeded. Please wait a moment and try again.")
    raise HTTPException(502, last_error or f"Failed to download skill '{slug}' from ClawHub")


class SkillFileIn(BaseModel):
    path: str
    content: str


class SkillCreateIn(BaseModel):
    name: str
    description: str = ""
    category: str = "custom"
    icon: str = "📋"
    folder_name: str
    files: list[SkillFileIn] = []


class ClawhubInstallIn(BaseModel):
    slug: str


class UrlImportIn(BaseModel):
    url: str


# ─── Helpers ──────────────────────────────────────────


def classify_portability(content: str) -> int:
    """Classify skill portability: 1=pure prompt, 2=CLI/API, 3=OpenClaw native."""
    openclaw_markers = [
        "bash pty:", "process action:", "Clawdbot", "exec tool",
        "openclaw.json", "imessage tool", "slack tool",
    ]
    cli_markers = [
        "requires:", "bins:", 'env:', "OPENAI_API_KEY", "GITHUB_TOKEN",
        "python3", "brew ", "pip install", "npm install", "curl ",
    ]
    lower = content.lower()
    for kw in openclaw_markers:
        if kw.lower() in lower:
            return 3
    for kw in cli_markers:
        if kw.lower() in lower:
            return 2
    return 1


def _parse_skill_md_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from SKILL.md content."""
    import yaml
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception:
        return {}


def _parse_github_url(url: str) -> dict | None:
    """Parse a GitHub URL into owner/repo/branch/path components."""
    # https://github.com/{owner}/{repo}/tree/{branch}/{path}
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*?)/?$", url
    )
    if m:
        return {"owner": m.group(1), "repo": m.group(2), "branch": m.group(3), "path": m.group(4)}
    # https://github.com/{owner}/{repo}/{path} (assume main branch)
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url
    )
    if m:
        return {"owner": m.group(1), "repo": m.group(2), "branch": "main", "path": ""}
    return None


def _parse_repo_url(url: str) -> dict | None:
    """Parse a GitHub or GitLab URL into provider/owner/repo/branch/path components."""
    # ── GitLab: https://{host}/{owner}/{repo}/-/tree/{branch}/{path} ──
    m = re.match(
        r"(https?)://([^/]+)/([^/]+)/([^/]+)/-/tree/([^/]+)/(.*?)/?$", url
    )
    if m:
        return {
            "provider": "gitlab",
            "base_url": f"{m.group(1)}://{m.group(2)}",
            "owner": m.group(3), "repo": m.group(4),
            "branch": m.group(5), "path": m.group(6),
        }
    # ── GitLab root / GitHub root ──
    m = re.match(
        r"(https?)://([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$", url
    )
    if m:
        scheme, host, owner, repo = m.group(1), m.group(2), m.group(3), m.group(4)
        if "github.com" in host:
            return {"provider": "github", "owner": owner, "repo": repo, "branch": "main", "path": ""}
        return {"provider": "gitlab", "base_url": f"{scheme}://{host}", "owner": owner, "repo": repo, "branch": "main", "path": ""}
    # ── GitHub tree ──
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*?)/?$", url
    )
    if m:
        return {"provider": "github", "owner": m.group(1), "repo": m.group(2), "branch": m.group(3), "path": m.group(4)}
    return None


def _apply_skill_scope(query, current_user: User):
    """Scope skill queries for tenant admins while leaving platform admins unrestricted."""
    from sqlalchemy import or_ as _or

    if current_user.role == "platform_admin" or not current_user.tenant_id:
        return query
    return query.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == current_user.tenant_id))


def _ensure_skill_write_access(skill: Skill, current_user: User):
    """Allow platform admins to edit everything; tenant admins can edit
    tenant-owned skills AND builtin (preset) skills visible to their tenant.
    Builtin skills are treated as presets -- placed during company init,
    but fully manageable by org_admin afterwards.
    """
    if current_user.role == "platform_admin":
        return
    if not current_user.tenant_id:
        raise HTTPException(403, "Cannot modify skills without a tenant")
    # Allow org_admin to manage: their own tenant skills OR builtin (preset) skills
    if skill.tenant_id is not None and skill.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Cannot modify other-tenant skills")


async def _fetch_github_directory(
    owner: str, repo: str, path: str, branch: str = "main",
    token: str = "",
) -> list[dict]:
    """Recursively fetch all files from a GitHub directory via API.
    Returns [{"path": relative_path, "content": text}].
    """
    _token = token
    files: list[dict] = []
    total_size = 0
    max_depth = 3  # Prevent runaway recursion
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}

    async def _recurse(dir_path: str, rel_prefix: str, depth: int = 0):
        nonlocal total_size
        if depth > max_depth:
            return
        api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{dir_path}?ref={branch}"
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(api_url)
            if resp.status_code == 404:
                raise HTTPException(404, f"GitHub path not found: {dir_path}")
            if resp.status_code == 403:
                raise HTTPException(429, "GitHub API rate limit exceeded. Try again later.")
            if resp.status_code != 200:
                raise HTTPException(502, f"GitHub API error: {resp.status_code}")
            items = resp.json()

        if isinstance(items, dict):
            # Single file (not a directory)
            items = [items]

        # Early guard: if at top level, scan subdirs for SKILL.md
        if depth == 0:
            has_skill_md = any(
                i["name"].upper() == "SKILL.MD" and i["type"] == "file"
                for i in items
            )
            if not has_skill_md:
                # Discover which subdirectories contain SKILL.md (like GitLab logic)
                skill_dirs: set[str] = set()
                for item in items:
                    if item["type"] == "dir":
                        try:
                            sub_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{item['path']}?ref={branch}"
                            _sub_headers = {"Authorization": f"Bearer {_token}"} if _token else {}
                            async with httpx.AsyncClient(timeout=10, headers=_sub_headers) as _client:
                                _resp = await _client.get(sub_url)
                                if _resp.status_code == 200:
                                    _sub_items = _resp.json()
                                    if isinstance(_sub_items, list) and any(
                                        i["name"].upper() == "SKILL.MD" and i["type"] == "file"
                                        for i in _sub_items
                                    ):
                                        skill_dirs.add(item["name"])
                        except Exception:
                            pass
                if skill_dirs:
                    # Only recurse into skill directories
                    for item in items:
                        if item["type"] == "dir" and item["name"] in skill_dirs:
                            await _recurse(item["path"], f"{rel_prefix}{item['name']}/" if rel_prefix else f"{item['name']}/", depth + 1)
                        elif item["type"] == "file":
                            size = item.get("size", 0)
                            total_size += size
                            if total_size > MAX_SKILL_SIZE:
                                raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")
                            async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                                dl_resp = await client.get(item["url"])
                                if dl_resp.status_code == 200:
                                    data = dl_resp.json()
                                    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
                                    files.append({"path": item['name'], "content": content})
                    return
                # If no token and all probes failed (likely rate-limited), error out clearly
                raise HTTPException(400, "No SKILL.md found. Configure a GitHub token in Enterprise Settings → Skills for private repos, or use a URL pointing directly to a skill directory.")

        for item in items:
            name = item["name"]
            rel = f"{rel_prefix}{name}" if rel_prefix else name

            if item["type"] == "dir":
                await _recurse(item["path"], f"{rel}/", depth + 1)
            elif item["type"] == "file":
                size = item.get("size", 0)
                total_size += size
                if total_size > MAX_SKILL_SIZE:
                    raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")
                # Download file content
                async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                    dl_resp = await client.get(item["url"])
                    if dl_resp.status_code == 200:
                        data = dl_resp.json()
                        content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
                        files.append({"path": rel, "content": content})

    try:
        await _recurse(path, "")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch files from GitHub: {e}")
    return files


async def _fetch_gitlab_directory(
    owner: str, repo: str, path: str, branch: str = "main",
    auth: dict | None = None, base_url: str = "https://gitlab.com",
    on_progress: callable = None,
) -> list[dict]:
    """Fetch all files from a GitLab repository directory via API.
    Uses recursive tree listing (paginated). Calls on_progress(msg, current, total) for status.
    Returns [{"path": relative_path, "content": text}].
    """
    from urllib.parse import quote
    api_base = f"{base_url}/api/v4"
    project_id = quote(f"{owner}/{repo}", safe="")
    headers = _build_gitlab_auth_headers(auth or {})
    _report = on_progress or (lambda msg, cur=0, tot=0: None)

    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        # Step 1: recursive tree (paginated — loop all pages)
        tree_url = f"{api_base}/projects/{project_id}/repository/tree"
        items: list[dict] = []
        page = 1
        while True:
            params: dict = {"ref": branch, "recursive": "true", "per_page": 100, "page": page}
            if path:
                params["path"] = path
            resp = await client.get(tree_url, params=params)
            if resp.status_code == 404:
                raise HTTPException(404, f"GitLab project or path not found: {owner}/{repo}")
            if resp.status_code in (401, 403):
                raise HTTPException(401, "GitLab authentication failed. Check your credentials.")
            if resp.status_code != 200:
                raise HTTPException(502, f"GitLab API error: {resp.status_code}")
            page_items = resp.json()
            if not page_items:
                break
            items.extend(page_items)
            _report(f"Fetching tree: {len(items)} entries found so far", 0, 0)
            if len(page_items) < 100:
                break
            page += 1

    # Filter blobs
    blobs = [i for i in items if i.get("type") == "blob"]
    if not blobs:
        raise HTTPException(404, "No files found at the specified path")

    # Validate SKILL.md exists at root level, or discover skill subdirectories
    path_prefix = (path + "/") if path else ""
    has_skill_md = any(
        b["name"].upper() == "SKILL.MD" and b.get("path", "").upper() == f"{path_prefix}SKILL.MD".upper()
        for b in blobs
    )
    if not has_skill_md:
        # No SKILL.md at root — discover all skill subdirectories and import only those
        skill_dirs: set[str] = set()
        for b in blobs:
            if b["name"].upper() == "SKILL.MD" and "/" in b.get("path", ""):
                rel = b["path"]
                if path_prefix and rel.startswith(path_prefix):
                    rel = rel[len(path_prefix):]
                parts = rel.rsplit("/", 1)
                if parts and len(parts) > 1:
                    skill_dirs.add(parts[0])
        if not skill_dirs:
            raise HTTPException(400, "No SKILL.md found — not a valid skill package.")
        # Filter blobs: only keep files inside discovered skill directories
        blobs = [b for b in blobs if any(
            b.get("path", "").startswith(d + "/") or b.get("path", "") == d
            for d in skill_dirs
        )]
        path_prefix = ""

    # Step 2: download each file
    files: list[dict] = []
    total_size = 0
    total_blobs = len(blobs)
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        for i, blob in enumerate(blobs):
            raw_path = blob["path"]
            rel = raw_path[len(path_prefix):] if path_prefix and raw_path.startswith(path_prefix) else raw_path

            encoded = quote(raw_path, safe="")
            raw_url = f"{api_base}/projects/{project_id}/repository/files/{encoded}/raw?ref={branch}"
            dl_resp = await client.get(raw_url)
            if dl_resp.status_code != 200:
                continue
            content = dl_resp.text

            total_size += len(content.encode("utf-8"))
            if total_size > MAX_SKILL_SIZE:
                raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")
            files.append({"path": rel, "content": content})
            if (i + 1) % 20 == 0 or i == total_blobs - 1:
                _report(f"Downloading {i + 1}/{total_blobs} files", i + 1, total_blobs)

    return files


async def _fetch_repo_directory(parsed: dict, user_id: str | None = None, tenant_id: str | None = None,
                               on_progress: callable = None) -> list[dict]:
    """Fetch files from a repo directory (GitHub or GitLab), dispatching by provider."""
    if parsed["provider"] == "gitlab":
        auth = await _resolve_gitlab_auth(user_id, tenant_id)
        return await _fetch_gitlab_directory(
            parsed["owner"], parsed["repo"], parsed["path"],
            parsed["branch"], auth, parsed.get("base_url", "https://gitlab.com"),
            on_progress=on_progress,
        )
    else:
        token = await _get_github_token(tenant_id)
        return await _fetch_github_directory(
            parsed["owner"], parsed["repo"], parsed["path"],
            parsed["branch"], token,
        )


async def _save_skill_to_db(
    folder_name: str, name: str, description: str,
    category: str, icon: str, files: list[dict],
    source_url: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Create a Skill + SkillFile records in the database."""
    import uuid as _uuid
    async with async_session() as db:
        # Check for folder_name conflict (scoped by tenant)
        conflict_q = select(Skill).where(Skill.folder_name == folder_name)
        if tenant_id:
            conflict_q = conflict_q.where(Skill.tenant_id == _uuid.UUID(tenant_id))
        else:
            conflict_q = conflict_q.where(Skill.tenant_id.is_(None))
        existing = await db.execute(conflict_q)
        if existing.scalar_one_or_none():
            raise HTTPException(
                409, f"A skill with folder name '{folder_name}' already exists. "
                     "Delete it first or use a different name."
            )

        skill = Skill(
            name=name,
            description=description,
            category=category,
            icon=icon,
            folder_name=folder_name,
            is_builtin=False,
            tenant_id=_uuid.UUID(tenant_id) if tenant_id else None,
        )
        db.add(skill)
        await db.flush()

        for f in files:
            # PostgreSQL text columns cannot store null bytes
            content = f["content"].replace("\x00", "") if f.get("content") else ""
            db.add(SkillFile(skill_id=skill.id, path=f["path"], content=content))

        await db.commit()
        return {"id": str(skill.id), "name": skill.name, "folder_name": skill.folder_name}


# ─── ClawHub Integration ─────────────────────────────


@router.get("/clawhub/search")
async def search_clawhub(q: str, current_user: User = Depends(get_current_user)):
    """Proxy search requests to the ClawHub API."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    data, _ = await _fetch_clawhub_json(
        _clawhub_search_endpoint,
        api_key=api_key,
        params={"q": q},
    )
    results = data.get("results", [])
    return [
        {
            "slug": r.get("slug"),
            "displayName": r.get("displayName"),
            "summary": r.get("summary"),
            "score": r.get("score"),
            "version": r.get("version"),
            "updatedAt": r.get("updatedAt"),
        }
        for r in results
    ]


@router.get("/clawhub/detail/{slug}")
async def clawhub_detail(slug: str, current_user: User = Depends(get_current_user)):
    """Fetch full metadata for a skill from ClawHub."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    try:
        data, _ = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to connect to ClawHub: {e}")


@router.post("/clawhub/install")
async def install_from_clawhub(body: ClawhubInstallIn, current_user: User = Depends(get_current_user)):
    """Install a skill from ClawHub into the global registry."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    slug = body.slug

    # 1. Fetch metadata from ClawHub (with retry for rate limits)
    api_key = await _get_clawhub_key(tenant_id)
    meta = None
    meta_base = None
    for attempt in range(3):
        try:
            meta, meta_base = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
            break
        except HTTPException:
            raise
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise HTTPException(502, f"Failed to connect to ClawHub: {e}")

    skill_info = meta.get("skill", {})
    owner_info = meta.get("owner", {})
    moderation = meta.get("moderation") or {}

    handle = owner_info.get("handle", "").lower()
    if not handle:
        raise HTTPException(400, "Could not determine skill owner handle from ClawHub")

    # 2. Build result with moderation warning
    is_suspicious = moderation.get("isSuspicious", False)
    moderation_summary = moderation.get("summary", "")

    # 3. Fetch files from the ClawHub archive
    files, archive_base = await _fetch_clawhub_skill_archive(slug, api_key=api_key, preferred_base=meta_base)

    if not files:
        raise HTTPException(404, "No files found in the ClawHub skill archive")

    # 4. Extract name/description from SKILL.md
    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    name = frontmatter.get("name", skill_info.get("displayName", slug))
    description = frontmatter.get("description", skill_info.get("summary", ""))

    # 5. Classify portability tier
    tier = classify_portability(skill_md["content"])
    tier_labels = {1: "clawhub-tier1", 2: "clawhub-tier2", 3: "clawhub-tier3"}
    has_scripts = any("/" in f["path"] for f in files if f["path"] != "SKILL.md")

    # 6. Save to DB
    result = await _save_skill_to_db(
        folder_name=slug,
        name=name,
        description=description,
        category=tier_labels.get(tier, "clawhub"),
        icon="",
        files=files,
        source_url=_public_clawhub_url(archive_base, slug),
        tenant_id=tenant_id,
    )

    result["tier"] = tier
    result["is_suspicious"] = is_suspicious
    result["moderation_summary"] = moderation_summary
    result["has_scripts"] = has_scripts
    result["file_count"] = len(files)
    result["source"] = "clawhub"
    result["archive_source"] = archive_base
    return result


@router.post("/import-from-url")
async def import_from_url(body: UrlImportIn, current_user: User = Depends(get_current_user)):
    """Import a skill from a GitHub or GitLab URL into the global registry."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    parsed = _parse_repo_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid URL. Expected GitHub or GitLab repository URL.")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]

    # Fetch files (GitHub or GitLab)
    files = await _fetch_repo_directory(parsed, str(current_user.id), tenant_id)
    if not files:
        raise HTTPException(404, "No files found at the specified path")

    # Validate SKILL.md exists
    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found at this URL — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    name = frontmatter.get("name", path.rstrip("/").split("/")[-1] if path else repo)
    description = frontmatter.get("description", "")

    # Derive folder_name from the last path segment
    folder_name = path.rstrip("/").split("/")[-1] if path else repo

    tier = classify_portability(skill_md["content"])
    tier_labels = {1: "url-import-tier1", 2: "url-import-tier2", 3: "url-import-tier3"}

    result = await _save_skill_to_db(
        folder_name=folder_name,
        name=name,
        description=description,
        category=tier_labels.get(tier, "url-import"),
        icon="",
        files=files,
        source_url=body.url,
        tenant_id=tenant_id,
    )

    result["tier"] = tier
    result["file_count"] = len(files)
    result["source"] = "url"
    return result


@router.post("/import-from-url/preview")
async def preview_url_import(body: UrlImportIn, current_user: User = Depends(get_current_user)):
    """Preview what will be imported from a GitHub or GitLab URL without saving."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    parsed = _parse_repo_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid URL. Expected GitHub or GitLab repository URL.")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]

    files = await _fetch_repo_directory(parsed, str(current_user.id), tenant_id)
    if not files:
        raise HTTPException(404, "No files found at the specified path")

    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    tier = classify_portability(skill_md["content"])

    return {
        "name": frontmatter.get("name", path.rstrip("/").split("/")[-1] if path else repo),
        "description": frontmatter.get("description", ""),
        "tier": tier,
        "files": [{"path": f["path"], "size": len(f["content"])} for f in files],
        "total_size": sum(len(f["content"]) for f in files),
        "has_scripts": any("/" in f["path"] for f in files if f["path"] != "SKILL.md"),
    }


# ─── Standard CRUD ────────────────────────────────────


@router.get("/")
async def list_skills(current_user: User = Depends(get_current_user)):
    """List global skills scoped by tenant (builtin + tenant-specific)."""
    import uuid as _uuid
    from sqlalchemy import or_ as _or
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    async with async_session() as db:
        query = select(Skill).order_by(Skill.name)
        # Scope by tenant: show builtin (tenant_id is NULL) + tenant-specific skills
        if tenant_id:
            query = query.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
        result = await db.execute(query)
        skills = result.scalars().all()
        return [
            {
                "id": str(s.id),
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "icon": s.icon,
                "folder_name": s.folder_name,
                "is_builtin": s.is_builtin,
                "is_default": s.is_default,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in skills
        ]


@router.get("/{skill_id}")
async def get_skill(skill_id: str, current_user: User = Depends(get_current_user)):
    """Get a skill with its files."""
    async with async_session() as db:
        query = select(Skill).where(Skill.id == skill_id).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_scope(query, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        return {
            "id": str(skill.id),
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "icon": skill.icon,
            "folder_name": skill.folder_name,
            "is_builtin": skill.is_builtin,
            "files": [
                {"path": f.path, "content": f.content}
                for f in skill.files
            ],
        }


@router.post("/")
async def create_skill(body: SkillCreateIn, current_user: User = Depends(get_current_admin)):
    """Create a custom skill."""
    async with async_session() as db:
        skill = Skill(
            name=body.name,
            description=body.description,
            category=body.category,
            icon=body.icon,
            folder_name=body.folder_name,
            is_builtin=False,
            tenant_id=current_user.tenant_id,
        )
        db.add(skill)
        await db.flush()

        if not body.files:
            # Auto-create a SKILL.md template
            db.add(SkillFile(
                skill_id=skill.id,
                path="SKILL.md",
                content=f"---\nname: {body.name}\ndescription: {body.description}\n---\n\n# {body.name}\n\n## Overview\n{body.description}\n",
            ))
        else:
            for f in body.files:
                db.add(SkillFile(skill_id=skill.id, path=f.path, content=f.content))

        await db.commit()
        return {"id": str(skill.id), "name": skill.name}


class SkillUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    icon: str | None = None
    files: list[SkillFileIn] | None = None


@router.put("/{skill_id}")
async def update_skill(skill_id: str, body: SkillUpdateIn, current_user: User = Depends(get_current_admin)):
    """Update a skill's metadata and/or files."""
    async with async_session() as db:
        query = select(Skill).where(Skill.id == skill_id).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_scope(query, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        _ensure_skill_write_access(skill, current_user)

        if body.name is not None:
            skill.name = body.name
        if body.description is not None:
            skill.description = body.description
        if body.category is not None:
            skill.category = body.category
        if body.icon is not None:
            skill.icon = body.icon

        # Replace files if provided
        if body.files is not None:
            for f in skill.files:
                await db.delete(f)
            await db.flush()
            for f in body.files:
                db.add(SkillFile(skill_id=skill.id, path=f.path, content=f.content))

        await db.commit()
        return {"id": str(skill.id), "name": skill.name}


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, current_user: User = Depends(get_current_admin)):
    """Delete a skill (not builtin)."""
    async with async_session() as db:
        query = select(Skill).where(Skill.id == skill_id)
        result = await db.execute(_apply_skill_scope(query, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        _ensure_skill_write_access(skill, current_user)
        await db.delete(skill)
        await db.commit()
        return {"ok": True}


# ─── Tenant GitHub Token Settings ───────────────────────────


class SkillSettingsIn(BaseModel):
    github_token: str | None = None
    clawhub_key: str | None = None
    gitlab_token: str | None = None
    gitlab_username: str | None = None
    gitlab_password: str | None = None


class MyGitLabSettingsIn(BaseModel):
    gitlab_token: str | None = None
    gitlab_username: str | None = None
    gitlab_password: str | None = None


def _setting_value(key: str, raw: str) -> dict:
    """Build the JSONB value dict for a setting key."""
    if key.endswith("_password"):
        return {"password": raw}
    if key.endswith("_username"):
        return {"username": raw}
    return {"token": raw}


async def _upsert_tenant_setting(tenant_id, key: str, value: str):
    """Helper to upsert a tenant setting."""
    from app.models.tenant_setting import TenantSetting
    async with async_session() as db:
        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = _setting_value(key, value)
        else:
            db.add(TenantSetting(
                tenant_id=tenant_id,
                key=key,
                value=_setting_value(key, value),
            ))
        await db.commit()


async def _upsert_user_setting(user_id, key: str, value: str):
    """Helper to upsert a user setting."""
    from app.models.user_setting import UserSetting
    async with async_session() as db:
        result = await db.execute(
            select(UserSetting).where(
                UserSetting.user_id == user_id,
                UserSetting.key == key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = _setting_value(key, value)
        else:
            db.add(UserSetting(
                user_id=user_id,
                key=key,
                value=_setting_value(key, value),
            ))
        await db.commit()


def _mask_token(token: str) -> str:
    if token and len(token) > 8:
        return f"{token[:4]}...{token[-4:]}"
    return "****" if token else ""


@router.get("/settings/token")
async def get_skill_token_status(
    current_user=Depends(require_role("org_admin", "platform_admin")),
):
    """Check if GitHub/GitLab tokens and ClawHub key are configured for this tenant."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    gh_token = await _get_github_token(tenant_id)
    ch_key = await _get_clawhub_key(tenant_id)
    gl_token = await _get_tenant_setting(tenant_id, "gitlab_token")
    gl_user = await _get_tenant_setting(tenant_id, "gitlab_username")
    gl_pass = await _get_tenant_setting(tenant_id, "gitlab_password")
    return {
        "configured": bool(gh_token),
        "source": "tenant" if tenant_id else "env",
        "masked": _mask_token(gh_token),
        "clawhub_configured": bool(ch_key),
        "clawhub_masked": _mask_token(ch_key),
        "gitlab_configured": bool(gl_token or (gl_user and gl_pass)),
        "gitlab_masked": _mask_token(gl_token),
        "gitlab_username": gl_user or "",
    }


@router.put("/settings/token")
async def set_skill_token(
    body: SkillSettingsIn,
    current_user=Depends(require_role("org_admin", "platform_admin")),
):
    """Save token settings for this tenant."""
    if not current_user.tenant_id:
        raise HTTPException(400, "No tenant associated")

    if body.github_token is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "github_token", body.github_token)
    if body.clawhub_key is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "clawhub_key", body.clawhub_key)
    if body.gitlab_token is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "gitlab_token", body.gitlab_token)
    if body.gitlab_username is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "gitlab_username", body.gitlab_username)
    if body.gitlab_password is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "gitlab_password", body.gitlab_password)
    return {"ok": True}


@router.get("/settings/my-gitlab")
async def get_my_gitlab_settings(
    current_user=Depends(get_current_user),
):
    """Get the current user's personal GitLab credentials."""
    user_id = str(current_user.id)
    token = await _get_user_setting(user_id, "gitlab_token")
    username = await _get_user_setting(user_id, "gitlab_username")
    return {
        "configured": bool(token or username),
        "masked": _mask_token(token),
        "username": username or "",
    }


@router.put("/settings/my-gitlab")
async def set_my_gitlab_settings(
    body: MyGitLabSettingsIn,
    current_user=Depends(get_current_user),
):
    """Save the current user's personal GitLab credentials."""
    user_id = str(current_user.id)
    if body.gitlab_token is not None:
        await _upsert_user_setting(user_id, "gitlab_token", body.gitlab_token)
    if body.gitlab_username is not None:
        await _upsert_user_setting(user_id, "gitlab_username", body.gitlab_username)
    if body.gitlab_password is not None:
        await _upsert_user_setting(user_id, "gitlab_password", body.gitlab_password)
    return {"ok": True}


# ─── Path-based browse endpoints for FileBrowser ───────────


@router.get("/browse/list")
async def browse_list(path: str = "", current_user: User = Depends(get_current_user)):
    """List skill folders (root) or files/subdirs within a skill folder."""
    import uuid as _uuid
    from sqlalchemy import or_ as _or
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    async with async_session() as db:
        if not path or path == "/":
            # Root: list all skill folders (scoped by tenant)
            query = select(Skill).order_by(Skill.name)
            if tenant_id:
                query = query.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
            result = await db.execute(query)
            skills = result.scalars().all()
            return [
                {"name": s.folder_name, "path": s.folder_name, "is_dir": True, "size": 0}
                for s in skills
            ]

        # Inside a skill folder — resolve the skill and relative subpath
        clean = path.strip("/")
        folder = clean.split("/")[0]
        # Resolve skill folder scoped by tenant
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        if tenant_id:
            skill_q = skill_q.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
        result = await db.execute(skill_q)
        skill = result.scalar_one_or_none()
        if not skill:
            return []

        # Calculate the relative prefix within the skill (empty = skill root)
        sub = clean[len(folder):].strip("/")  # e.g. "" or "scripts" or "scripts/sub"

        items = []
        seen_dirs: set[str] = set()
        for f in skill.files:
            if sub:
                # Only files that start with this sub prefix
                if not f.path.startswith(sub + "/"):
                    continue
                remainder = f.path[len(sub) + 1:]  # strip "scripts/" prefix
            else:
                remainder = f.path

            if "/" in remainder:
                # This file is in a subdirectory — show the directory
                dir_name = remainder.split("/")[0]
                if dir_name not in seen_dirs:
                    seen_dirs.add(dir_name)
                    dir_path = f"{folder}/{sub}/{dir_name}" if sub else f"{folder}/{dir_name}"
                    items.append({"name": dir_name, "path": dir_path, "is_dir": True, "size": 0})
            else:
                # Direct child file
                file_path = f"{folder}/{f.path}"
                items.append({"name": remainder, "path": file_path, "is_dir": False, "size": len(f.content.encode())})

        return items


@router.get("/browse/read")
async def browse_read(path: str, current_user: User = Depends(get_current_user)):
    """Read a file from a skill folder."""
    import uuid as _uuid
    from sqlalchemy import or_ as _or
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    parts = path.strip("/").split("/", 1)
    if len(parts) < 2:
        raise HTTPException(400, "Path must include folder and file")
    folder, file_path = parts
    async with async_session() as db:
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        if tenant_id:
            skill_q = skill_q.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
        result = await db.execute(skill_q)
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        for f in skill.files:
            if f.path == file_path:
                return {"content": f.content}
        raise HTTPException(404, "File not found")


class BrowseWriteIn(BaseModel):
    path: str
    content: str


@router.put("/browse/write")
async def browse_write(body: BrowseWriteIn, current_user: User = Depends(get_current_admin)):
    """Write a file in a skill folder. Creates the skill if the folder doesn't exist."""
    parts = body.path.strip("/").split("/", 1)
    if len(parts) < 2:
        raise HTTPException(400, "Path must include folder and file")
    folder, file_path = parts
    async with async_session() as db:
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_scope(skill_q, current_user))
        skill = result.scalar_one_or_none()
        created_new_skill = False
        if not skill:
            # Auto-create skill from folder name, scoped to tenant
            skill = Skill(
                name=folder.replace("-", " ").title(),
                description="",
                category="custom",
                icon="--",
                folder_name=folder,
                is_builtin=False,
                tenant_id=current_user.tenant_id,
            )
            db.add(skill)
            await db.flush()
            created_new_skill = True
        else:
            _ensure_skill_write_access(skill, current_user)

        # Upsert file
        existing = None
        if not created_new_skill:
            for f in skill.files:
                if f.path == file_path:
                    existing = f
                    break
        if existing:
            existing.content = body.content
        else:
            db.add(SkillFile(skill_id=skill.id, path=file_path, content=body.content))
        await db.commit()
        return {"ok": True}


@router.delete("/browse/delete")
async def browse_delete(path: str, current_user: User = Depends(get_current_admin)):
    """Delete a file or an entire skill folder."""
    parts = path.strip("/").split("/", 1)
    folder = parts[0]
    async with async_session() as db:
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_scope(skill_q, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        _ensure_skill_write_access(skill, current_user)

        if len(parts) == 1:
            # Delete entire skill
            await db.delete(skill)
        else:
            # Delete specific file
            file_path = parts[1]
            for f in skill.files:
                if f.path == file_path:
                    await db.delete(f)
                    break
        await db.commit()
        return {"ok": True}
