"""GitLab workspace initialization for per-agent GitLab bindings.

Pure git-CLI integration (docs/technical-plans/20260820-gitlab-agent-binding):
each agent binds one GitLab token + one project; on binding save this module
initializes the agent's repo directory — `workspace/<project-name>/` under the
workspace root (/data/agents/<aid>/workspace), which may also hold files that
are NOT part of the repo — in one of three modes:

- clone:  repo dir empty → git clone + work-branch init
- adopt:  repo dir has code without .git → git init + first commit + push
          (never touches user files)
- inject: repo dir has .git → rewrite credentials + committer identity and
          self-heal a drifted origin URL

Legacy layout (repo rooted at the workspace root itself, v2 design) is
migrated on save: local clone into the repo dir, then the stray root .git is
dropped; untracked files at the root stay untouched.

All git subprocesses use argv arrays (no shell) and redact the PAT from any
text that ends up in logs or DB state.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from app.config import get_settings

_AGENT_INIT_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}

_SANDBOX_JUNK = {".tmp"}

_BRANCH_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")

_REPO_DIR_RE = re.compile(r"^[\w.-]+$", re.UNICODE)

_CLONE_TIMEOUT_S = 600
_PUSH_TIMEOUT_S = 300
_DEFAULT_GIT_TIMEOUT_S = 60


def _workspace_base() -> Path:
    settings = get_settings()
    return Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR)


def repo_root(agent_id: uuid.UUID) -> Path:
    """The agent workspace root (may hold the repo dir plus other files)."""
    return _workspace_base() / str(agent_id) / "workspace"


def _repo_dir_name(project_path: str) -> str:
    """Filesystem-safe directory name = the last segment of the project path."""
    name = project_path.rstrip("/").rsplit("/", 1)[-1]
    if not name or not _REPO_DIR_RE.match(name) or name in {".", "..", ".git"} or name in _SANDBOX_JUNK:
        raise ValueError(f"project_path 的最后一段（目录名）非法: {name!r}")
    return name


def repo_path(agent_id: uuid.UUID, project_path: str) -> Path:
    """The bound repository directory: workspace/<project-name>/."""
    return repo_root(agent_id) / _repo_dir_name(project_path)


def _credential_rewrite(base_url: str, pat: str) -> str:
    """insteadOf rewrite key: keeps the original scheme (intranet is http)."""
    parsed = urlparse(base_url)
    host = parsed.netloc or parsed.path
    return f"{parsed.scheme}://oauth2:{pat}@{host}/"


def _base_prefix(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = parsed.netloc or parsed.path
    return f"{parsed.scheme}://{host}/"


def _redact(text: str, pat: str | None) -> str:
    if pat:
        return text.replace(pat, "glpat-****")
    return text


async def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    pat: str | None = None,
    timeout: int = _DEFAULT_GIT_TIMEOUT_S,
) -> tuple[int, str, str]:
    """Run git with an argv array; returns (returncode, stdout, stderr)."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": "/tmp",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    }
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"git timed out after {timeout}s"
    out = _redact(stdout.decode(errors="replace")[:4096], pat)
    err = _redact(stderr.decode(errors="replace")[:4096], pat)
    return proc.returncode, out, err


def _detect_mode(root: Path) -> str:
    """clone / adopt / inject based on the workspace root content."""
    if not root.exists():
        return "clone"
    entries = [p for p in root.iterdir() if p.name not in _SANDBOX_JUNK]
    if not entries:
        return "clone"
    if (root / ".git").exists():
        return "inject"
    return "adopt"


async def _apply_repo_config(
    root: Path,
    rewrite: str,
    base_prefix: str,
    agent_name: str,
    agent_email: str,
    pat: str,
) -> None:
    """Write repo-local credential rewrite + committer identity (argv, no shell)."""
    rc, out, err = await _run_git(
        ["config", "--local", f"url.{rewrite}.insteadOf", base_prefix],
        cwd=root,
        pat=pat,
    )
    if rc != 0:
        raise RuntimeError(f"git config insteadOf failed: {err.strip()}")
    # 清掉指向同一 host 的旧 PAT 残留键（token 轮换后不留在 .git/config 里）
    rc, out, _ = await _run_git(
        ["config", "--local", "--get-regexp", r"^url\..*\.insteadOf$"],
        cwd=root,
        pat=pat,
    )
    if rc == 0:
        current = f"url.{rewrite}.insteadOf"
        for line in out.splitlines():
            key, _, value = line.partition(" ")
            key, value = key.strip(), value.strip()
            if value == base_prefix and key != current:
                await _run_git(["config", "--local", "--unset", key], cwd=root, pat=pat)
    for key, value in (("user.name", agent_name), ("user.email", agent_email)):
        rc, _, err = await _run_git(["config", "--local", key, value], cwd=root, pat=pat)
        if rc != 0:
            raise RuntimeError(f"git config {key} failed: {err.strip()}")


async def _remote_default_branch(root: Path, pat: str) -> str | None:
    """Resolve origin's default branch, if the local clone knows it."""
    rc, out, _ = await _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=root, pat=pat)
    if rc == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    # Fallback: ask the remote directly (--symref on HEAD).
    rc, out, _ = await _run_git(["ls-remote", "--symref", "origin", "HEAD"], cwd=root, pat=pat, timeout=120)
    if rc == 0:
        m = re.search(r"ref:\s+refs/heads/(\S+)\s+HEAD", out)
        if m:
            return m.group(1)
    return None


async def _remote_has_branch(root: Path, branch: str, pat: str) -> bool:
    rc, out, _ = await _run_git(
        ["ls-remote", "--heads", "origin", branch],
        cwd=root,
        pat=pat,
        timeout=120,
    )
    return rc == 0 and bool(out.strip())


async def _clone_mode(
    root: Path,
    clone_url: str,
    default_branch: str,
    rewrite: str,
    base_prefix: str,
    agent_name: str,
    agent_email: str,
    pat: str,
) -> str | None:
    """Clone into the workspace root and set up the work branch. Returns init commit."""
    rc, _, err = await _run_git(
        [
            "-c",
            f"url.{rewrite}.insteadOf={base_prefix}",
            "clone",
            clone_url,
            str(root),
        ],
        pat=pat,
        timeout=_CLONE_TIMEOUT_S,
    )
    if rc != 0:
        raise RuntimeError(f"git clone failed: {err.strip()}")
    await _apply_repo_config(root, rewrite, base_prefix, agent_name, agent_email, pat)

    remote_default = await _remote_default_branch(root, pat) or "main"
    if await _remote_has_branch(root, default_branch, pat):
        rc, _, err = await _run_git(
            ["checkout", "-b", default_branch, f"origin/{default_branch}"],
            cwd=root,
            pat=pat,
        )
        if rc != 0:
            raise RuntimeError(f"checkout {default_branch} failed: {err.strip()}")
    else:
        rc, _, err = await _run_git(
            ["checkout", "-b", default_branch, f"origin/{remote_default}"],
            cwd=root,
            pat=pat,
        )
        if rc != 0:
            raise RuntimeError(f"create {default_branch} failed: {err.strip()}")
        rc, _, err = await _run_git(
            ["push", "-u", "origin", default_branch], cwd=root, pat=pat, timeout=_PUSH_TIMEOUT_S
        )
        if rc != 0:
            raise RuntimeError(f"push {default_branch} failed: {err.strip()}")
    rc, out, _ = await _run_git(["rev-parse", "HEAD"], cwd=root, pat=pat)
    return out.strip() if rc == 0 else None


async def _adopt_mode(
    root: Path,
    remote_url: str,
    default_branch: str,
    rewrite: str,
    base_prefix: str,
    agent_name: str,
    agent_email: str,
    pat: str,
) -> tuple[str | None, bool]:
    """git init in place, first commit, push. Returns (commit, main_missing)."""
    rc, _, err = await _run_git(["init", "-b", default_branch], cwd=root, pat=pat)
    if rc != 0:
        raise RuntimeError(f"git init failed: {err.strip()}")
    rc, _, err = await _run_git(["remote", "add", "origin", remote_url], cwd=root, pat=pat)
    if rc != 0:
        raise RuntimeError(f"remote add failed: {err.strip()}")
    await _apply_repo_config(root, rewrite, base_prefix, agent_name, agent_email, pat)

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".tmp/\n__pycache__/\n*.pyc\n.DS_Store\n", encoding="utf-8")

    rc, _, err = await _run_git(["add", "-A"], cwd=root, pat=pat)
    if rc != 0:
        raise RuntimeError(f"git add failed: {err.strip()}")
    rc, _, err = await _run_git(["commit", "-m", "Initial commit"], cwd=root, pat=pat)
    if rc != 0:
        raise RuntimeError(f"git commit failed: {err.strip()}")
    rc, _, err = await _run_git(["push", "-u", "origin", default_branch], cwd=root, pat=pat, timeout=_PUSH_TIMEOUT_S)
    if rc != 0:
        raise RuntimeError(f"git push failed: {err.strip()}")

    main_missing = not await _remote_has_branch(root, "main", pat)
    rc, out, _ = await _run_git(["rev-parse", "HEAD"], cwd=root, pat=pat)
    return (out.strip() if rc == 0 else None), main_missing


async def _inject_mode(
    root: Path,
    clone_url: str,
    rewrite: str,
    base_prefix: str,
    agent_name: str,
    agent_email: str,
    pat: str,
) -> str | None:
    """Existing repo: self-heal a drifted origin, rewrite credentials + identity.

    Covers both a user-entered bad URL from an earlier save and a legacy
    relocation whose origin points at the old local path.
    """
    rc, out, _ = await _run_git(["remote", "get-url", "origin"], cwd=root, pat=pat)
    if rc != 0:
        rc, _, err = await _run_git(["remote", "add", "origin", clone_url], cwd=root, pat=pat)
        if rc != 0:
            raise RuntimeError(f"git remote add failed: {err.strip()}")
    elif out.strip() != clone_url:
        rc, _, err = await _run_git(["remote", "set-url", "origin", clone_url], cwd=root, pat=pat)
        if rc != 0:
            raise RuntimeError(f"git remote set-url failed: {err.strip()}")
    await _apply_repo_config(root, rewrite, base_prefix, agent_name, agent_email, pat)
    rc, out, _ = await _run_git(["rev-parse", "HEAD"], cwd=root, pat=pat)
    return out.strip() if rc == 0 else None


async def _relocate_legacy(root: Path, repo: Path, pat: str) -> None:
    """Migrate the v2 layout (repo at the workspace root) into the repo dir.

    Local clone keeps every ref; untracked files stay at the root (out of the
    repo), then the stray root .git is dropped. On failure nothing is removed.
    """
    rc, _, err = await _run_git(
        ["clone", "--local", "--no-hardlinks", str(root), str(repo)],
        pat=pat,
        timeout=_CLONE_TIMEOUT_S,
    )
    if rc != 0:
        shutil.rmtree(repo, ignore_errors=True)
        raise RuntimeError(f"legacy layout relocate failed: {err.strip()}")
    shutil.rmtree(root / ".git", ignore_errors=True)


def _write_guide(root: Path, repo_dir_name: str, project_path: str, default_branch: str, adopt_note: bool) -> None:
    guide = root / "GITLAB_GUIDE.md"
    lines = [
        "# GitLab 使用指南（本 agent 专属）",
        "",
        f"- **仓库位置**：`workspace/{repo_dir_name}/`（沙箱内 `/workspace/{repo_dir_name}/`），git 操作先 `cd {repo_dir_name}`（或 `git -C {repo_dir_name}`）。",
        f"- 绑定项目：`{project_path}`；工作分支：`{default_branch}`。",
        "- workspace 根目录下的其他文件**不属于仓库**，git 不会跟踪它们。",
        '- 日常：`git pull` 同步；改动后 `git add -A && git commit -m "..."`；推送到工作分支。',
        f'- 提 MR：`git push origin {default_branch} -o merge_request.create -o merge_request.target=main -o merge_request.title="..."`（已存在 MR 则更新）。',
        "- 分支：本地开发分支随意建/切/合（`git merge` 合进工作分支）；**main 只能经 MR 进入，绝不直接 push**。",
        "- 禁止：`push --force`、`git push origin main`、reset 远程共享分支。",
        "- **提交身份固定为本 agent**：不得用 `--author` 或 `-c user.name=` 覆盖提交人。",
        f"- 项目身份：本 agent 只操作 `{project_path}`，token 权限仅限该项目。",
    ]
    if adopt_note:
        lines.append(
            "- ⚠️ 远端尚未发现 `main` 分支：请管理员在 GitLab 初始化 README 或补推 main，再提 MR（MR 目标依赖它）。"
        )
    guide.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _update_state(
    session,
    agent_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    commit: str | None = None,
    repo_dir: str | None = None,
) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.models.channel_config import ChannelConfig

    result = await session.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "gitlab",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        return
    extra = dict(config.extra_config or {})
    extra["init_status"] = status
    extra["init_error"] = error
    extra["init_updated_at"] = datetime.now(timezone.utc).isoformat()
    if commit:
        extra["init_commit"] = commit
    if repo_dir:
        extra["repo_dir"] = repo_dir
    config.extra_config = extra
    await session.commit()


async def run_gitlab_workspace_init(
    agent_id: uuid.UUID,
    project_path: str,
    default_branch: str,
    pat: str,
) -> None:
    """Initialize (or refresh credentials of) the agent's bound repo. Best-effort
    background task: all failures are recorded in the binding's init state."""
    from sqlalchemy import select

    from app.dao import query_dao
    from app.models.agent import Agent as AgentModel

    settings = get_settings()
    base_url = settings.GITLAB_BASE_URL.rstrip("/")
    root = repo_root(agent_id)
    try:
        repo = repo_path(agent_id, project_path)
    except ValueError as exc:
        async with query_dao.session() as session:
            await _update_state(session, agent_id, status="failed", error=str(exc))
        logger.warning("[GitLabBinding] bad repo dir name agent={}: {}", agent_id, exc)
        return
    repo_name = repo.name
    rewrite = _credential_rewrite(base_url, pat)
    prefix = _base_prefix(base_url)
    clone_url = f"{base_url}/{project_path}.git"

    lock = _AGENT_INIT_LOCKS.setdefault(agent_id, asyncio.Lock())
    async with lock:
        async with query_dao.session() as session:
            await _update_state(session, agent_id, status="initializing")

            agent_result = await session.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            agent_name = (agent.name if agent else "") or str(agent_id)
            agent_email = f"agent-{agent_id.hex[:8]}@clawith.local"

            mode = _detect_mode(repo)
            legacy = False
            created_repo = False
            try:
                if mode == "clone" and (root / ".git").exists():
                    # v2 旧布局（仓库根=工作区根）→ 迁移到子目录
                    await _relocate_legacy(root, repo, pat)
                    mode = "inject"
                    legacy = True
                if mode == "clone":
                    if not root.exists():
                        root.mkdir(parents=True, exist_ok=True)
                    if not repo.exists():
                        repo.mkdir(parents=True, exist_ok=True)
                        created_repo = True
                    commit = await _clone_mode(
                        repo,
                        clone_url,
                        default_branch,
                        rewrite,
                        prefix,
                        agent_name,
                        agent_email,
                        pat,
                    )
                    adopt_note = False
                elif mode == "adopt":
                    commit, main_missing = await _adopt_mode(
                        repo,
                        clone_url,
                        default_branch,
                        rewrite,
                        prefix,
                        agent_name,
                        agent_email,
                        pat,
                    )
                    adopt_note = main_missing
                else:  # inject
                    commit = await _inject_mode(repo, clone_url, rewrite, prefix, agent_name, agent_email, pat)
                    adopt_note = False

                _write_guide(root, repo_name, project_path, default_branch, adopt_note)
                await _update_state(session, agent_id, status="done", commit=commit, repo_dir=repo_name)
                logger.info(
                    "[GitLabBinding] init done mode={}{} agent={} repo={}",
                    mode,
                    " (legacy relocated)" if legacy else "",
                    agent_id,
                    repo_name,
                )
            except Exception as exc:  # noqa: BLE001 — background task boundary
                err = _redact(str(exc)[:500], pat)
                logger.warning(
                    "[GitLabBinding] init failed mode={} agent={}: {}",
                    mode,
                    agent_id,
                    err,
                )
                if mode == "clone" and created_repo:
                    shutil.rmtree(repo, ignore_errors=True)
                elif mode == "adopt" and (repo / ".git").exists():
                    shutil.rmtree(repo / ".git", ignore_errors=True)
                await _update_state(session, agent_id, status="failed", error=err)
