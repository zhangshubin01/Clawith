"""Sandbox-internal helpers shared by the local backends (subprocess & docker).

These helpers own the parts of execute_code that are backend-agnostic:
workspace staging, gateway publication, the host-side pip proxy, and the
per-agent venv lifecycle.  The subprocess backend delegates to them through
thin methods kept for backward-compatible names; the docker backend imports
them directly.
"""

import asyncio
from dataclasses import dataclass
import os
import signal
import shutil
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

VENV_CREATION_TIMEOUT_SECONDS = 120
PROCESS_TERMINATION_GRACE_SECONDS = 5
MAX_PUBLISHED_FILES_PER_EXECUTION = 100
MAX_DELETED_FILES_PER_EXECUTION = 100
MAX_PUBLISHED_TOTAL_BYTES = 50 * 1024 * 1024
MAX_PUBLISHED_FILE_BYTES = 10 * 1024 * 1024


def _has_symlink_component(path: Path, root: Path) -> bool:
    """True when any component between root and path is a symlink, or the
    resolved path escapes root.

    Host-side post-run writes into the staging tree must refuse to traverse
    symlinks planted by sandbox code (GHSA-pxhw-h44j-8pfx class: a setup step
    following a symlink into attacker-controlled content). Components are
    checked on the raw (unresolved) path first; resolve() alone would erase
    the symlinks this function is meant to detect.
    """
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in rel_parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return False
    try:
        path.resolve().relative_to(root.resolve())
        return False
    except ValueError:
        return True


def _write_exclusive(path: Path, data: bytes) -> bool:
    """Create path with O_EXCL+O_NOFOLLOW, writing data; False on failure.

    A planted file or symlink at path is unlinked first (unlink never follows
    symlinks) and the write retried once; if it still fails the caller keeps
    the request for a later round instead of reporting success.
    """
    for _ in range(2):
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except (FileExistsError, OSError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return False
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return True
    return False


def resolve_proxy_env(config) -> dict[str, str]:
    """Resolve proxy / PyPI-mirror env vars for sandbox execution.

    Single authority for the precedence chain (per-agent config → CLAWITH_*
    runtime env → standard proxy vars) shared by the subprocess and docker
    backends.
    """
    env: dict[str, str] = {}
    http_proxy = config.http_proxy or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    https_proxy = config.https_proxy or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    no_proxy = config.no_proxy or os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
    if http_proxy:
        env["http_proxy"] = http_proxy
        env["HTTP_PROXY"] = http_proxy
    if https_proxy:
        env["https_proxy"] = https_proxy
        env["HTTPS_PROXY"] = https_proxy
    if no_proxy:
        env["no_proxy"] = no_proxy
        env["NO_PROXY"] = no_proxy
    pip_index_url = config.pip_index_url or os.environ.get("CLAWITH_PIP_INDEX_URL") or os.environ.get("PIP_INDEX_URL")
    if pip_index_url:
        env["PIP_INDEX_URL"] = pip_index_url
        host = urlparse(pip_index_url).hostname
        if host:
            env["PIP_TRUSTED_HOST"] = host
    return env


async def terminate_and_reap_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess group and wait until its direct child is reaped."""
    if proc.returncode is not None:
        await proc.wait()
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.kill()

    try:
        await asyncio.wait_for(
            asyncio.shield(proc.wait()),
            timeout=PROCESS_TERMINATION_GRACE_SECONDS,
        )
        return
    except asyncio.TimeoutError:
        pass

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    await proc.wait()


async def ensure_workspace_venv(
    venv_path: Path,
    *,
    timeout: float = VENV_CREATION_TIMEOUT_SECONDS,
) -> None:
    """Create the per-agent venv with uv if missing, then fix pip shebangs."""
    venv_python = venv_path / "bin" / "python"
    if not venv_python.exists():
        # Use uv to create the virtual environment for extreme speed
        # --seed ensures pip is still present in the venv
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "venv",
            "--seed",
            str(venv_path),
            cwd=str(venv_path.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if proc.returncode is None:
                await terminate_and_reap_process(proc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError("Timed out while creating the execute_code virtual environment") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500]
            raise RuntimeError(
                "Failed to create the execute_code virtual environment" + (f": {detail}" if detail else "")
            )

    # Fix shebang lines in pip scripts to use bwrap-visible path
    # venv creates scripts with absolute paths to the host Python,
    # but bwrap only mounts /workspace, so those paths don't exist inside the sandbox
    fix_pip_shebangs(venv_path)


def fix_pip_shebangs(venv_path: Path) -> None:
    """Replace pip with a bash wrapper that proxies execution to the host if in a sandbox, else delegates to uv pip."""
    venv_bin = venv_path / "bin"
    wrapper_script = (
        "#!/bin/bash\n"
        "if [ -d /workspace/.tmp ]; then\n"
        "    REQ_ID=$RANDOM\n"
        '    REQ_FILE="/workspace/.tmp/.pip_request_${REQ_ID}"\n'
        '    RES_FILE="/workspace/.tmp/.pip_response_${REQ_ID}"\n'
        '    OUT_FILE="/workspace/.tmp/.pip_output_${REQ_ID}"\n'
        '    echo "$@" > "$REQ_FILE"\n'
        "    N=0\n"
        '    while [ ! -f "$RES_FILE" ]; do\n'
        "        N=$((N+1))\n"
        '        if [ "$N" -gt 300 ]; then\n'
        '            echo "pip proxy timeout (no response from host watcher)" >&2\n'
        "            exit 124\n"
        "        fi\n"
        "        sleep 0.2\n"
        "    done\n"
        '    if [ -f "$OUT_FILE" ]; then\n'
        '        cat "$OUT_FILE"\n'
        '        rm -f "$OUT_FILE"\n'
        "    fi\n"
        '    EXIT_CODE=$(cat "$RES_FILE")\n'
        '    rm -f "$RES_FILE"\n'
        "    exit $EXIT_CODE\n"
        "else\n"
        '    exec uv pip "$@"\n'
        "fi\n"
    )

    for pip_cmd in ["pip", "pip3", "pip3.12"]:
        pip_path = venv_bin / pip_cmd
        if pip_path.parent.exists():
            pip_path.write_text(wrapper_script, encoding="utf-8")
            pip_path.chmod(0o755)


def build_safe_env(config, work_path: Path) -> dict[str, str]:
    """Build the sanitized environment for sandbox execution."""
    venv_bin = work_path / ".venv" / "bin"
    workspace_tmp = work_path / ".tmp"
    env = {
        "HOME": str(work_path),
        "PATH": f"{venv_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(workspace_tmp),
        "NODE_PATH": "",
        "BASH_ENV": "",
        "ENV": "",
        "VIRTUAL_ENV": str(work_path / ".venv"),
        "PIP_CACHE_DIR": str(workspace_tmp / "pip-cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    env.update(resolve_proxy_env(config))
    return env


async def watch_pip_requests(staging_path: Path, venv_path: Path, stop_event: asyncio.Event) -> None:
    """Watch for pip request files in the staging directory's .tmp and execute them using uv on the host."""
    while not stop_event.is_set():
        try:
            tmp_dir = staging_path / "workspace" / ".tmp"
            if tmp_dir.exists():
                for request_file in tmp_dir.glob(".pip_request_*"):
                    if not request_file.exists():
                        continue
                    try:
                        args_str = request_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        continue

                    req_id = request_file.name.split("_")[-1]
                    response_file = tmp_dir / f".pip_response_{req_id}"
                    output_file = tmp_dir / f".pip_output_{req_id}"
                    if response_file.exists():
                        continue

                    args = args_str.split()
                    if not args:
                        raise ValueError("Empty pip proxy request")
                    cmd = [
                        "uv",
                        "pip",
                        args[0],
                        "--python",
                        str(venv_path / "bin" / "python"),
                        *args[1:],
                    ]
                    logger.info(f"[Sandbox Host] Proxying pip command: {' '.join(cmd)}")

                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await proc.communicate()
                        exit_code = proc.returncode
                        output = (stdout + stderr).decode("utf-8", errors="replace")
                    except Exception as exc:
                        logger.error(f"[Sandbox Host] Failed to run proxy pip: {exc}")
                        exit_code = 1
                        output = f"pip proxy failed: {exc}\n"

                    try:
                        _write_exclusive(output_file, output[-20000:].encode("utf-8"))
                        if _write_exclusive(
                            response_file,
                            str(exit_code).encode("utf-8"),
                        ):
                            request_file.unlink(missing_ok=True)
                        # else: the response could not be created (planted
                        # entry at the response path); keep the request so a
                        # later round retries instead of hanging the caller.
                    except Exception as exc:
                        logger.error(f"[Sandbox Host] Failed to write pip response: {exc}")
        except Exception as exc:
            logger.error(f"[Sandbox Host] Error in pip watcher loop: {exc}")
        await asyncio.sleep(0.2)


async def verify_and_merge_outputs(
    staging_path: Path,
    target_workspace: Path,
    agent_id=None,
    session_id=None,
    publish_paths: list[str] | None = None,
    workspace_mode: str = "merge",
    record_revisions: bool = False,
) -> None:
    """Scan staging directory, enforce safety checks, sanitize HTML/SVG, and merge to workspace with DB revisions."""
    try:
        from lxml.html.clean import Cleaner
        import lxml.html

        cleaner = Cleaner(
            scripts=True,
            javascript=True,
            comments=True,
            style=False,
            links=False,
            meta=True,
            page_structure=False,
            processing_instructions=True,
            embedded=True,
            frames=True,
            forms=True,
            kill_tags=["script", "iframe", "object", "embed", "applet"],
            remove_unknown_tags=False,
            safe_attrs_only=True,
        )
    except ImportError:
        cleaner = None

    banned_suffixes = {".py", ".sh", ".js", ".elf", ".exe", ".so", ".dylib", ".dll", ".bat", ".cmd"}
    protected_files = {"soul.md", "tasks.json", "tasks.json.bak", "enterprise_info"}

    allowed_roots = tuple(Path(path) for path in (publish_paths or [""]))

    def is_allowed(relative_path: Path) -> bool:
        return any(root == Path("") or relative_path == root or root in relative_path.parents for root in allowed_roots)

    # Collect files in staging
    staging_files: dict[Path, Path] = {}
    for root, dirs, files in os.walk(staging_path):
        dirs[:] = [d for d in dirs if d not in (".venv", ".tmp")]
        for file in files:
            file_path = Path(root) / file
            relative_path = file_path.relative_to(staging_path)
            if (
                file.startswith("_exec_tmp")
                or file.startswith(".pip_")
                or ".tmp" in relative_path.parts
                or not is_allowed(relative_path)
                or file_path.is_symlink()
            ):
                continue
            staging_files[relative_path] = file_path

    # Collect files in target_workspace
    target_files: dict[Path, Path] = {}
    for root, dirs, files in os.walk(target_workspace):
        dirs[:] = [d for d in dirs if d not in (".venv", ".tmp")]
        for file in files:
            file_path = Path(root) / file
            relative_path = file_path.relative_to(target_workspace)
            if (
                file.startswith("_exec_tmp")
                or file.startswith(".pip_")
                or ".tmp" in relative_path.parts
                or not is_allowed(relative_path)
                or file_path.is_symlink()
            ):
                continue
            target_files[relative_path] = file_path

    publication_candidates: dict[Path, Path] = {}
    for rel_path, file_path in staging_files.items():
        rel_path_str = str(rel_path)
        target_file = target_files.get(rel_path)
        if rel_path_str in protected_files:
            if target_file is None:
                logger.warning(f"[Sandbox Gateway] Blocked attempt to create protected file: {rel_path}")
                continue
            try:
                if file_path.read_bytes() != target_file.read_bytes():
                    logger.warning(f"[Sandbox Gateway] Blocked attempt to modify protected file: {rel_path}")
                continue
            except OSError:
                continue
        if target_file is not None:
            try:
                if file_path.read_bytes() == target_file.read_bytes():
                    continue
            except OSError:
                pass
        if file_path.suffix.lower() in banned_suffixes:
            logger.warning(f"[Sandbox Gateway] Blocked banned file extension: {rel_path}")
            continue
        publication_candidates[rel_path] = file_path

    deletion_candidates = {
        rel_path: target_path
        for rel_path, target_path in target_files.items()
        if rel_path not in staging_files and str(rel_path) not in protected_files
    }
    for rel_path, target_path in target_files.items():
        if rel_path in staging_files or str(rel_path) not in protected_files:
            continue
        logger.warning(f"[Sandbox Gateway] Blocked attempt to delete protected file: {rel_path}")
        try:
            restored_path = staging_path / rel_path
            if _has_symlink_component(restored_path, staging_path):
                logger.warning(
                    f"[Sandbox Gateway] Refusing protected-file restore through a symlinked path: {rel_path}"
                )
                continue
            restored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, restored_path)
        except OSError:
            pass

    # Session-isolated output has one serialized writer and cannot mutate the
    # shared Workspace tree, so shared-workspace change-count limits do not
    # apply. Content safety and byte-size limits remain enforced below.
    if workspace_mode != "isolated_output":
        if len(publication_candidates) > MAX_PUBLISHED_FILES_PER_EXECUTION:
            raise RuntimeError(f"Sandbox generated too many changed files (limit: {MAX_PUBLISHED_FILES_PER_EXECUTION})")
        if len(deletion_candidates) > MAX_DELETED_FILES_PER_EXECUTION:
            raise RuntimeError(f"Sandbox deleted too many files (limit: {MAX_DELETED_FILES_PER_EXECUTION})")

    total_size = 0
    for rel_path, file_path in publication_candidates.items():
        try:
            file_size = file_path.stat().st_size
        except FileNotFoundError:
            continue
        total_size += file_size
        if total_size > MAX_PUBLISHED_TOTAL_BYTES:
            raise RuntimeError(
                f"Sandbox generated changed files exceeding total size limit (limit: {MAX_PUBLISHED_TOTAL_BYTES} bytes)"
            )
        if file_size > MAX_PUBLISHED_FILE_BYTES:
            raise RuntimeError(f"File '{rel_path}' exceeds single file size limit ({MAX_PUBLISHED_FILE_BYTES} bytes)")

    # Dynamic imports for database revisions
    write_workspace_file = None
    delete_workspace_file = None
    async_session = None
    if agent_id and record_revisions:
        try:
            from app.database import async_session
            from app.services.workspace_collaboration import write_workspace_file, delete_workspace_file
        except ImportError:
            pass

    # 1. Process Created and Modified Files
    for rel_path, file_path in publication_candidates.items():
        rel_path_str = str(rel_path)

        # Sanitize HTML/SVG if cleaner is available
        if file_path.suffix.lower() in (".html", ".svg"):
            try:
                content = file_path.read_text(encoding="utf-8")
                if cleaner:
                    try:
                        doc = lxml.html.fragment_fromstring(content, create_parent="div")
                        clean_doc = cleaner.clean_html(doc)
                        cleaned = lxml.html.tostring(clean_doc, encoding="utf-8").decode("utf-8")
                        if cleaned.startswith("<div>") and cleaned.endswith("</div>"):
                            cleaned = cleaned[5:-6]
                    except Exception:
                        cleaned = cleaner.clean_html(content)
                else:
                    import re

                    cleaned = re.sub(
                        r"<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>", "", content, flags=re.IGNORECASE
                    )
                    cleaned = re.sub(r"\bon[a-z]+\s*=\s*\"[^\"]*\"", "", cleaned, flags=re.IGNORECASE)
                    cleaned = re.sub(r"\bon[a-z]+\s*=\s*'[^']*'", "", cleaned, flags=re.IGNORECASE)

                file_path.write_text(cleaned, encoding="utf-8")
            except Exception as e:
                logger.error(f"[Sandbox Gateway] Failed to sanitize file '{rel_path}': {e}")
                continue

        # Read content for revision
        try:
            file_content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            file_content = None

        # Copy verified file to workspace
        dest_path = target_workspace / rel_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(file_path, dest_path)
        except Exception as e:
            logger.error(f"[Sandbox Gateway] Failed to copy '{rel_path}' to workspace: {e}")
            continue

        # Record DB revision
        if agent_id and write_workspace_file and async_session and file_content is not None:
            try:
                async with async_session() as db:
                    await write_workspace_file(
                        db,
                        agent_id=agent_id,
                        base_dir=target_workspace,
                        path=rel_path_str,
                        content=file_content,
                        actor_type="agent",
                        actor_id=agent_id,
                        session_id=session_id,
                        enforce_human_lock=True,
                    )
                    await db.commit()
            except Exception as e:
                raise RuntimeError(f"Gateway publication failed for '{rel_path}'") from e

    # 2. Process Deleted Files
    for rel_path, target_path in deletion_candidates.items():
        rel_path_str = str(rel_path)

        try:
            target_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"[Sandbox Gateway] Failed to delete local file '{rel_path}': {e}")
            continue

        # Record DB deletion
        if agent_id and delete_workspace_file and async_session:
            try:
                async with async_session() as db:
                    await delete_workspace_file(
                        db,
                        agent_id=agent_id,
                        base_dir=target_workspace,
                        path=rel_path_str,
                        actor_type="agent",
                        actor_id=agent_id,
                        session_id=session_id,
                        enforce_human_lock=True,
                    )
                    await db.commit()
            except Exception as e:
                raise RuntimeError(f"Gateway deletion failed for '{rel_path}'") from e


def clone_workspace_to_staging(source: Path, dest: Path) -> None:
    """Clone all workspace files to staging area, ignoring virtualenv and tmp folders."""
    dest.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in source.iterdir():
        if item.name in (".venv", ".tmp"):
            continue
        if item.is_file():
            if item.name.startswith("_exec_tmp"):
                continue
            shutil.copy2(item, dest / item.name)
        elif item.is_dir():
            shutil.copytree(item, dest / item.name, symlinks=True, dirs_exist_ok=True)


# ── Run-scoped staging sync (direction 3a) ──────────────────────────────────
#
# The sandbox staging tree (B) is a one-shot clone of the run workspace (A) at
# the first execute_code; direct-write tools (edit_file / write_file / ...)
# update storage + A but never B, so the sandbox's git view stays stale-clean.
# This registry lets the local backends expose their live staging tree so a
# direct write can be reflected into B synchronously, keeping B a live derived
# view of storage (single authority).


@dataclass(frozen=True, slots=True)
class _StagingRegistryEntry:
    """One run's live staging tree as exposed for direct-write refresh.

    Lifecycle is tied 1:1 to the owning backend session: registered at
    ``_start_persistent_session`` and forgotten at ``close_run`` (both keep it
    in lockstep with ``_run_sessions``). The registry is in-memory, so it dies
    with the process — an orphaned entry from a crashed process never outlives
    it, and within a live process an entry is only ever removed by the same
    owner that added it. ``workspace_mode`` + ``publish_paths`` carry the
    sandbox's write scope so isolated_output runs sync only their single
    publish path (never the read-only remainder of the staging tree).
    """

    staging_path: Path
    lock: asyncio.Lock
    workspace_mode: str
    publish_paths: tuple[str, ...]


_sandbox_staging_registry: dict[str, _StagingRegistryEntry] = {}


def register_sandbox_staging(
    run_id: str,
    staging_path: Path,
    lock: asyncio.Lock,
    *,
    workspace_mode: str = "merge",
    publish_paths: tuple[str, ...] = (),
) -> None:
    """Expose one run's live staging tree for direct-write refresh."""
    _sandbox_staging_registry[run_id] = _StagingRegistryEntry(
        staging_path=staging_path,
        lock=lock,
        workspace_mode=workspace_mode,
        publish_paths=tuple(publish_paths),
    )


def unregister_sandbox_staging(run_id: str) -> None:
    """Forget a run's staging tree (close_run teardown)."""
    _sandbox_staging_registry.pop(run_id, None)


def _rel_path_within_any(rel_path: str, roots: tuple[str, ...]) -> bool:
    """Segment-level prefix match against the publish paths (no restriction
    when ``roots`` is empty, mirroring ``classify_publish_path``'s normalization)."""
    if not roots:
        return True
    norm = [p for p in str(rel_path).replace("\\", "/").split("/") if p not in {"", "."}]
    for root in roots:
        root_parts = [p for p in str(root).replace("\\", "/").split("/") if p not in {"", "."}]
        if norm[: len(root_parts)] == root_parts:
            return True
    return False


async def refresh_sandbox_staging_path(run_id: str, rel_path: str, data: bytes | None) -> None:
    """Reflect one direct storage write into the run's sandbox staging tree.

    Best-effort by design (mirrors ``refresh_run_workspace_path``): a missing
    run (sandbox not started, or already closed) is a no-op — the next
    ``clone_workspace_to_staging`` re-materializes A, which already carries the
    write. ``data=None`` marks a deletion. Paths that escape the staging root
    (``..`` or a sandbox-planted symlink) are refused. In ``isolated_output``
    mode only the run's single publish path is written — the read-only
    remainder of the staging tree is left untouched.
    """
    entry = _sandbox_staging_registry.get(run_id)
    if entry is None:
        logger.info("[SandboxStagingRefresh] skipped run_id={} path={}", run_id, rel_path)
        return
    if entry.workspace_mode == "isolated_output" and not _rel_path_within_any(
        rel_path, entry.publish_paths
    ):
        logger.info(
            "[SandboxStagingRefresh] skipped non-publish path run_id={} path={} mode={}",
            run_id,
            rel_path,
            entry.workspace_mode,
        )
        return
    async with entry.lock:
        target = entry.staging_path / rel_path
        if _has_symlink_component(target, entry.staging_path):
            logger.warning(
                "[SandboxStagingRefresh] traversal rejected run_id={} path={}",
                run_id,
                rel_path,
            )
            return
        if data is None:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
