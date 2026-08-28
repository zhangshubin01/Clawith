"""Local docker-based persistent sandbox backend (terminal state for execute_code).

Each Agent loop owns one long-lived sandbox container (``docker run -d sleep
infinity`` + ``docker exec`` per command), mirroring the subprocess backend's
per-Run bwrap session.  Workspace staging, gateway publication and the
host-side pip proxy are shared with the subprocess backend via
``app.services.sandbox.local.shared``.

Isolation: no network by default, 256m memory / 0.5 cpu, pid limit, all
capabilities dropped, no-new-privileges, read-only rootfs, uid 1000.
"""

import asyncio
from dataclasses import dataclass
import os
import shlex
import shutil
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from docker import errors
from loguru import logger

from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.docker_client import get_docker_client
from app.services.sandbox.security import check_code_safety
from app.services.sandbox.local.shared import (
    clone_workspace_to_staging,
    ensure_workspace_venv,
    resolve_proxy_env,
    verify_and_merge_outputs,
    watch_pip_requests,
)
from app.services.workspace_paths import WorkspacePathError, resolve_path_within_root

SANDBOX_VENV_PATH = "/opt/clawith/venv"
_EXEC_DONE_PREFIX = "__CLAWITH_EXEC_DONE__"
MAX_STDOUT_CAPTURE_BYTES = 1_000_000
MAX_STDERR_CAPTURE_BYTES = 500_000
_SESSION_START_TIMEOUT_SECONDS = 30
_CONTAINER_UID_GID = "1000:1000"
_CONTAINER_PIDS_LIMIT = 64
_TMPFS_TMP_SIZE = "size=64m,mode=1777"
# DooD: staging must live under /data/agents (shared bind mount) — see
# _detect_host_agent_data_root.  Fallback is a tempfile dir when /data/agents
# does not exist (running directly on the host in dev).
_STAGING_PARENT = Path("/data/agents/.sandbox-staging")


@dataclass
class _PersistentDockerSession:
    run_id: str
    agent_id: uuid.UUID | None
    session_id: str | None
    workspace_mode: str
    publish_paths: tuple[str, ...]
    work_path: Path
    # None when staging lives under /data/agents/.sandbox-staging (production).
    temp_dir: tempfile.TemporaryDirectory | None
    staging_path: Path
    venv_path: Path
    # docker-py container object; kept untyped because tests inject a fake
    # client whose containers are not real ``docker.models.containers.Container``.
    container: Any
    pip_stop_event: asyncio.Event
    pip_watcher_task: asyncio.Task
    lock: asyncio.Lock


def _remove_container_force(container: Any) -> None:
    """Blocking helper: remove the sandbox container, tolerating auto-removal."""
    try:
        container.remove(force=True)
    except (errors.NotFound, errors.APIError):
        pass


def _detect_host_agent_data_root() -> str:
    """Resolve the host-side path of /data/agents via the docker socket.

    DooD (Docker-on-Docker): the backend container starts sandbox containers
    through docker.sock, and bind-mount SOURCES are resolved by the host
    daemon — which cannot see this container's private filesystem.  Only
    paths on shared bind mounts (like /data/agents) are daemon-visible, via
    their Mounts[].Source host path.  Verified by socket-level probe
    2026-08-29: a container-private source silently becomes an EMPTY dir in
    the spawned container (no error anywhere in the chain).

    Returns "" when undetectable; the backend then passes container paths
    through unchanged (best-effort — e.g. running directly on the host in
    dev, or unit tests with a fake client).
    """
    hostname = os.environ.get("HOSTNAME") or socket.gethostname()
    try:
        client = get_docker_client()
        info = client.containers.get(hostname)
    except errors.NotFound:
        logger.warning(f"[DockerSession] container {hostname} not found via docker.sock")
        return ""
    except Exception as exc:
        logger.warning(f"[DockerSession] docker.sock unavailable: {exc}")
        return ""
    for mount in info.attrs.get("Mounts", []):
        dest = (mount.get("Destination") or "").rstrip("/")
        if dest == "/data/agents":
            host_path = mount["Source"]
            logger.debug(f"[DockerSession] detected host path for /data/agents: {host_path}")
            return host_path
    logger.warning("[DockerSession] /data/agents mount not found in container info")
    return ""


def _staging_parent() -> Path | None:
    """Return the staging parent directory, or None to use a tempfile dir.

    DooD: bind-mount sources are resolved by the host daemon, which cannot
    see this container's private filesystem (/tmp).  Staging therefore lives
    under /data/agents — a shared bind mount — so the daemon can mount it;
    the actual host path is obtained via _detect_host_agent_data_root().
    """
    root = Path("/data/agents")
    if not root.is_dir():
        return None
    base = _STAGING_PARENT
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


def _cleanup_staging_dir(temp_dir: tempfile.TemporaryDirectory | None, staging_path: Path) -> None:
    """Best-effort removal of one session's staging tree."""
    if temp_dir is not None:
        temp_dir.cleanup()
    else:
        shutil.rmtree(staging_path, ignore_errors=True)


class DockerSessionBackend(BaseSandboxBackend):
    """Docker-based sandbox backend with one long-lived container per Run."""

    name = "docker"
    # Single-event-loop registry keyed by run_id. No lock: every mutation
    # (pop/set) happens synchronously between awaits, and per-Run execution
    # is serialized upstream by the Agent loop, so two coroutines never
    # interleave a check-then-set on the same run_id.
    _run_sessions: dict[str, _PersistentDockerSession] = {}

    def __init__(self, config: SandboxConfig):
        self.config = config
        # DooD: host-side path of /data/agents, used to translate bind-mount
        # sources so the host daemon can resolve them (empty = passthrough,
        # e.g. unit tests or running directly on the host).
        self._host_agent_data_root = _detect_host_agent_data_root()

    def _host_path(self, container_path: str) -> str:
        """Translate a /data/agents container path to a daemon-visible host path.

        The host daemon resolves bind-mount sources in its own namespace: a
        container-private path silently becomes an EMPTY dir in the spawned
        container (probe-verified 2026-08-29).  Everything we mount lives on
        the shared /data/agents bind mount, so a prefix substitution is
        sufficient.  Non-/data/agents paths are returned unchanged.
        """
        if not self._host_agent_data_root:
            return container_path
        prefix = "/data/agents"
        if not container_path.startswith(prefix):
            return container_path
        return self._host_agent_data_root + container_path[len(prefix) :]

    @classmethod
    async def close_run(cls, run_id: str) -> None:
        """Stop and remove the sandbox container owned by one Agent loop.

        Best-effort teardown: callers invoke this from ``finally`` blocks and
        timeout handlers, so it must never raise and must not leave a live
        container behind when the surrounding task is cancelled.
        """
        session = cls._run_sessions.pop(run_id, None)
        if session is None:
            return
        session.pip_stop_event.set()
        try:
            # shield: if this teardown task itself gets cancelled, the watcher
            # still drains instead of being abandoned with the container.
            await asyncio.shield(session.pip_watcher_task)
        except BaseException:
            # CancelledError (BaseException, not Exception) or a crashed
            # watcher — teardown continues regardless.
            pass
        try:
            await asyncio.shield(asyncio.to_thread(_remove_container_force, session.container))
        except BaseException as exc:
            # Container may already be gone (auto_remove): removal is
            # best-effort, so even a failed or cancelled removal is non-fatal.
            logger.warning(f"[DockerSession] Failed to remove sandbox container: {exc}")
        try:
            _cleanup_staging_dir(session.temp_dir, session.staging_path)
        except BaseException as exc:
            # Staging cleanup is cosmetic; never let it mask the caller's own
            # exception path.
            logger.warning(f"[DockerSession] Failed to clean staging dir: {exc}")

    def _build_command(self, language: str, script_path: str) -> list[str]:
        if language == "python":
            return [f"{SANDBOX_VENV_PATH}/bin/python", "-I", "-B", str(script_path)]
        if language == "bash":
            return ["bash", "--noprofile", "--norc", "-o", "pipefail", str(script_path)]
        return ["node", str(script_path)]

    def _build_container_kwargs(
        self,
        *,
        name: str,
        run_id: str,
        staging_path: Path,
        venv_path: Path,
        writable_path: str | None,
    ) -> dict:
        """Assemble ``containers.run`` kwargs: mounts, env, resource limits, isolation.

        Every bind-mount source goes through ``_host_path``: the host daemon
        cannot see this container's private filesystem, so /data/agents paths
        must be rewritten to their daemon-visible host path (DooD).
        """
        volumes = {
            self._host_path(str(staging_path / "workspace")): {"bind": "/workspace", "mode": "rw"},
            self._host_path(str(staging_path / "memory")): {"bind": "/memory", "mode": "rw"},
            self._host_path(str(staging_path / "skills")): {"bind": "/skills", "mode": "rw"},
            self._host_path(str(venv_path)): {"bind": SANDBOX_VENV_PATH, "mode": "ro"},
        }
        for root_file in ("focus.md", "soul.md", "HEARTBEAT.md"):
            source = staging_path / root_file
            if source.exists():
                volumes[self._host_path(str(source))] = {"bind": f"/{root_file}", "mode": "rw"}
        uv_cache = Path("/data/agents/.uv-cache")
        if uv_cache.exists():
            volumes[self._host_path(str(uv_cache))] = {"bind": "/uv-cache", "mode": "ro"}

        env = {
            "HOME": "/workspace",
            "PATH": (f"{SANDBOX_VENV_PATH}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "TMPDIR": "/workspace/.tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "NODE_PATH": "",
            "BASH_ENV": "",
            "ENV": "",
            "VIRTUAL_ENV": SANDBOX_VENV_PATH,
            "PIP_CACHE_DIR": "/workspace/.tmp/pip-cache",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_CACHE_DIR": "/uv-cache",
        }
        # Proxy / PyPI-mirror precedence is owned by shared.resolve_proxy_env.
        env.update(resolve_proxy_env(self.config))
        if writable_path:
            env["CLAWITH_SESSION_OUTPUT_DIR"] = writable_path

        try:
            cpu_quota = int(float(self.config.cpu_limit) * 100000)
        except (TypeError, ValueError):
            logger.warning(f"[DockerSession] Invalid cpu_limit {self.config.cpu_limit!r}; falling back to 0.5 cpu")
            cpu_quota = 50000

        return {
            "command": ["sleep", "infinity"],
            "detach": True,
            "name": name,
            "labels": {"clawith.sandbox": "execute-code", "clawith.run_id": run_id},
            "user": _CONTAINER_UID_GID,
            "volumes": volumes,
            "environment": env,
            "working_dir": "/workspace",
            "mem_limit": self.config.memory_limit,
            "cpu_period": 100000,
            "cpu_quota": cpu_quota,
            "pids_limit": _CONTAINER_PIDS_LIMIT,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "read_only": True,
            "tmpfs": {"/tmp": _TMPFS_TMP_SIZE},
            # "none" (not None): docker-py serializes None as 'default'
            # (bridge), which would give the sandbox network by default.
            "network_mode": "none" if not self.config.allow_network else "bridge",
            "auto_remove": True,
        }

    async def _ensure_image(self, image: str) -> None:
        client = get_docker_client()

        def _pull_if_missing() -> None:
            try:
                client.images.get(image)
            except errors.ImageNotFound:
                logger.info(f"[DockerSession] Pulling sandbox image: {image}")
                client.images.pull(image)

        await asyncio.to_thread(_pull_if_missing)

    async def _start_persistent_session(
        self,
        *,
        run_id: str,
        work_path: Path,
        venv_path: Path,
        agent_id: uuid.UUID | None,
        session_id: str | None,
        workspace_mode: str,
        publish_paths: list[str] | None,
    ) -> _PersistentDockerSession:
        staging_base = _staging_parent()
        if staging_base is not None:
            # Under /data/agents (shared bind mount) so the host daemon can
            # resolve the mount sources; see _detect_host_agent_data_root.
            staging_path = staging_base / f"{run_id[:8]}-{uuid.uuid4().hex[:8]}"
            staging_path.mkdir(mode=0o700)
            temp_dir = None
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix=f"clawith-docker-{run_id[:8]}-")
            staging_path = Path(temp_dir.name)
        clone_workspace_to_staging(work_path, staging_path)
        (staging_path / "workspace" / ".tmp" / "pip-cache").mkdir(
            parents=True,
            exist_ok=True,
        )
        writable_path = publish_paths[0] if workspace_mode == "isolated_output" and publish_paths else None
        if writable_path:
            writable_host = (staging_path / writable_path).resolve()
            if not writable_host.is_relative_to(staging_path.resolve()):
                raise ValueError("Sandbox writable path escapes staging root")
            writable_host.mkdir(parents=True, exist_ok=True)

        await self._ensure_image(self.config.sandbox_image)
        name = f"clawith-exec-{run_id[:8]}-{uuid.uuid4().hex[:6]}"
        client = get_docker_client()
        try:
            container = await asyncio.to_thread(
                client.containers.run,
                image=self.config.sandbox_image,
                **self._build_container_kwargs(
                    name=name,
                    run_id=run_id,
                    staging_path=staging_path,
                    venv_path=venv_path,
                    writable_path=writable_path,
                ),
            )
        except Exception as exc:
            _cleanup_staging_dir(temp_dir, staging_path)
            raise RuntimeError(f"Failed to start sandbox container: {type(exc).__name__}: {str(exc)[:300]}") from exc

        for _ in range(int(_SESSION_START_TIMEOUT_SECONDS * 10)):
            await asyncio.sleep(0.1)
            try:
                await asyncio.to_thread(container.reload)
                if container.status == "running":
                    break
            except errors.NotFound:
                break
        else:
            await asyncio.to_thread(_remove_container_force, container)
            _cleanup_staging_dir(temp_dir, staging_path)
            raise RuntimeError("Sandbox container failed to reach running state")
        if getattr(container, "status", None) != "running":
            await asyncio.to_thread(_remove_container_force, container)
            _cleanup_staging_dir(temp_dir, staging_path)
            raise RuntimeError(
                f"Sandbox container exited during startup (status={getattr(container, 'status', 'gone')})"
            )

        pip_stop_event = asyncio.Event()
        pip_watcher_task = asyncio.create_task(watch_pip_requests(staging_path, venv_path, pip_stop_event))
        persistent = _PersistentDockerSession(
            run_id=run_id,
            agent_id=agent_id,
            session_id=session_id,
            workspace_mode=workspace_mode,
            publish_paths=tuple(publish_paths or ()),
            work_path=work_path.resolve(),
            temp_dir=temp_dir,
            staging_path=staging_path,
            venv_path=venv_path,
            container=container,
            pip_stop_event=pip_stop_event,
            pip_watcher_task=pip_watcher_task,
            lock=asyncio.Lock(),
        )
        DockerSessionBackend._run_sessions[run_id] = persistent
        return persistent

    async def _container_alive(self, session: _PersistentDockerSession) -> bool:
        try:
            await asyncio.to_thread(session.container.reload)
            return session.container.status == "running"
        except errors.NotFound:
            return False
        except errors.APIError:
            return False

    async def _persistent_session(
        self,
        *,
        run_id: str,
        work_path: Path,
        venv_path: Path,
        agent_id: uuid.UUID | None,
        session_id: str | None,
        workspace_mode: str,
        publish_paths: list[str] | None,
    ) -> _PersistentDockerSession:
        existing = DockerSessionBackend._run_sessions.get(run_id)
        expected_paths = tuple(publish_paths or ())
        if existing is not None and (
            existing.agent_id != agent_id
            or existing.session_id != session_id
            or existing.workspace_mode != workspace_mode
            or existing.publish_paths != expected_paths
            or existing.work_path != work_path.resolve()
            or not await self._container_alive(existing)
        ):
            await DockerSessionBackend.close_run(run_id)
            existing = None
        if existing is None:
            return await self._start_persistent_session(
                run_id=run_id,
                work_path=work_path,
                venv_path=venv_path,
                agent_id=agent_id,
                session_id=session_id,
                workspace_mode=workspace_mode,
                publish_paths=publish_paths,
            )
        return existing

    async def _run_in_persistent_session(
        self,
        session: _PersistentDockerSession,
        *,
        code: str,
        language: str,
        timeout: int,
        on_output,
    ) -> tuple[int, str, str, bool]:
        token = uuid.uuid4().hex
        extension = {"python": ".py", "bash": ".sh", "node": ".js"}[language]
        temp_path = session.staging_path / "workspace" / ".tmp"
        script_path = temp_path / f"_exec_tmp_{token}{extension}"
        stdout_path = temp_path / f"_exec_stdout_{token}"
        stderr_path = temp_path / f"_exec_stderr_{token}"
        script_path.write_text(code, encoding="utf-8")
        command = self._build_command(
            language,
            f"/workspace/.tmp/{script_path.name}",
        )
        marker = f"{_EXEC_DONE_PREFIX}{token}:"
        shell_line = (
            f"{shlex.join(command)} >{shlex.quote('/workspace/.tmp/' + stdout_path.name)} "
            f"2>{shlex.quote('/workspace/.tmp/' + stderr_path.name)}; "
            f"__clawith_rc=$?; printf '{marker}%s\\n' \"$__clawith_rc\"\n"
        )

        stream_stop = asyncio.Event()

        async def stream_output_files() -> None:
            offsets = {stdout_path: 0, stderr_path: 0}
            labels = {stdout_path: "stdout", stderr_path: "stderr"}
            while not stream_stop.is_set():
                if on_output:
                    for path, offset in tuple(offsets.items()):
                        if not path.exists():
                            continue
                        with path.open("rb") as stream:
                            stream.seek(offset)
                            chunk = stream.read()
                        if chunk:
                            offsets[path] += len(chunk)
                            try:
                                await on_output(
                                    chunk.decode("utf-8", errors="replace"),
                                    labels[path],
                                )
                            except Exception:
                                pass
                try:
                    await asyncio.wait_for(stream_stop.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass

            if on_output:
                for path, offset in tuple(offsets.items()):
                    if not path.exists():
                        continue
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        chunk = stream.read()
                    if chunk:
                        try:
                            await on_output(
                                chunk.decode("utf-8", errors="replace"),
                                labels[path],
                            )
                        except Exception:
                            pass

        stream_task = asyncio.create_task(stream_output_files())

        timed_out = False
        exit_code = 1
        try:
            try:
                exec_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        session.container.exec_run,
                        ["bash", "-c", shell_line],
                        user=_CONTAINER_UID_GID,
                        workdir="/workspace",
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                # Kill the container so no code keeps running; the session
                # object and staging survive so partial output still publishes,
                # then execute() closes the run (mirrors the bwrap reset).
                try:
                    await asyncio.to_thread(session.container.kill)
                except (errors.NotFound, errors.APIError):
                    pass
                exit_code = 124
            except (errors.NotFound, errors.APIError) as exc:
                await DockerSessionBackend.close_run(session.run_id)
                raise RuntimeError(
                    "Persistent docker sandbox exited before command settlement"
                    + (f": {type(exc).__name__}: {str(exc)[:300]}" if str(exc) else "")
                ) from exc
            else:
                output = (exec_result.output or b"").decode("utf-8", errors="replace")
                exit_code = None
                for line in output.strip().splitlines():
                    if line.startswith(marker):
                        try:
                            exit_code = int(line.removeprefix(marker))
                        except ValueError:
                            pass
                if exit_code is None:
                    # Marker missing (exec stream torn, image without the
                    # wrapper semantics): fall back to the transport exit
                    # code, and to 1 only when even that is unavailable.
                    # `or` is wrong here — it would coerce a healthy 0 to 1.
                    exit_code = exec_result.exit_code if exec_result.exit_code is not None else 1
        finally:
            stream_stop.set()
            await stream_task

        stdout = stdout_path.read_bytes()[:MAX_STDOUT_CAPTURE_BYTES] if stdout_path.exists() else b""
        stderr = stderr_path.read_bytes()[:MAX_STDERR_CAPTURE_BYTES] if stderr_path.exists() else b""
        stdout_text = stdout.decode("utf-8", errors="replace")[:10000]
        stderr_text = stderr.decode("utf-8", errors="replace")[:5000]
        for path in (script_path, stdout_path, stderr_path):
            path.unlink(missing_ok=True)
        return exit_code, stdout_text, stderr_text, timed_out

    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=256,
            network_available=self.config.allow_network,
            filesystem_available=True,
        )

    async def health_check(self) -> bool:
        """Check if docker is available and running."""
        try:
            client = get_docker_client()
            await asyncio.to_thread(client.ping)
            return True
        except Exception:
            return False

    async def execute(
        self, code: str, language: str, timeout: int = 30, work_dir: str | None = None, **kwargs
    ) -> ExecutionResult:
        """Execute code inside the per-Run sandbox container."""
        on_output = kwargs.get("on_output")
        agent_id = kwargs.get("agent_id")
        session_id = kwargs.get("session_id")
        run_id = kwargs.get("run_id")
        workspace_mode = kwargs.get("workspace_mode", "merge")
        publication_owner = kwargs.get("publication_owner", "workspace_cas")
        publish_paths = kwargs.get("publish_paths")
        before_gateway_publish = kwargs.get("before_gateway_publish")
        gateway_publish = kwargs.get("gateway_publish")
        start_time = time.time()

        if language not in ("python", "bash", "node"):
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}. Use: python, bash, or node",
            )

        safety_error = check_code_safety(language, code, self.config.allow_network)
        if safety_error:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"❌ {safety_error}",
            )

        if work_dir:
            work_path = Path(work_dir).resolve()
        else:
            work_path = (Path.cwd() / "workspace").resolve()
        try:
            work_path = resolve_path_within_root(work_path, "", label="work_dir")
        except WorkspacePathError as exc:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=str(exc),
            )
        work_path.mkdir(parents=True, exist_ok=True)
        (work_path / ".tmp" / "pip-cache").mkdir(parents=True, exist_ok=True)

        if agent_id:
            venv_path = Path("/data/agents").resolve() / str(agent_id) / ".venv"
            venv_path.parent.mkdir(parents=True, exist_ok=True)
            uv_cache = Path("/data/agents/.uv-cache")
            uv_cache.mkdir(parents=True, exist_ok=True)
        else:
            venv_path = work_path / ".venv"

        # Legacy one-shot calls (no Run scope) get an ephemeral session with
        # the same staging/publication semantics.
        effective_run_id = run_id if (isinstance(run_id, str) and run_id) else f"oneshot-{uuid.uuid4().hex}"

        try:
            await ensure_workspace_venv(venv_path)
            persistent = await self._persistent_session(
                run_id=effective_run_id,
                work_path=work_path,
                venv_path=venv_path,
                agent_id=agent_id,
                session_id=session_id,
                workspace_mode=workspace_mode,
                publish_paths=publish_paths,
            )
            async with persistent.lock:
                exit_code, stdout_str, stderr_str, is_timeout = await self._run_in_persistent_session(
                    persistent,
                    code=code,
                    language=language,
                    timeout=timeout,
                    on_output=on_output,
                )
                duration_ms = int((time.time() - start_time) * 1000)
                try:
                    if (
                        publication_owner == "gateway"
                        and before_gateway_publish is not None
                        and not await before_gateway_publish()
                    ):
                        raise RuntimeError("Sandbox publication ownership could not be verified")
                    await verify_and_merge_outputs(
                        persistent.staging_path,
                        work_path,
                        agent_id=agent_id,
                        session_id=session_id,
                        publish_paths=publish_paths,
                        workspace_mode=workspace_mode,
                        record_revisions=False,
                    )
                    if publication_owner == "gateway":
                        if gateway_publish is None:
                            raise RuntimeError("Gateway publication callback is missing")
                        await gateway_publish()
                except Exception as exc:
                    return ExecutionResult(
                        success=False,
                        stdout=stdout_str,
                        stderr=stderr_str,
                        exit_code=1,
                        duration_ms=duration_ms,
                        error=f"sandbox_publication_unknown: {type(exc).__name__}",
                    )
                finally:
                    if is_timeout:
                        await DockerSessionBackend.close_run(effective_run_id)
                if is_timeout:
                    return ExecutionResult(
                        success=False,
                        stdout=stdout_str,
                        stderr=stderr_str,
                        exit_code=124,
                        duration_ms=duration_ms,
                        error=(f"Code execution timed out after {timeout}s. The Agent-loop sandbox was reset."),
                    )
                return ExecutionResult(
                    success=exit_code == 0,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    error=None if exit_code == 0 else f"Exit code: {exit_code}",
                )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"sandbox_persistent_execution_failed: {type(exc).__name__}: {str(exc)[:300]}",
            )
        finally:
            # Ephemeral one-shot sessions do not outlive this call.
            if effective_run_id != run_id:
                await DockerSessionBackend.close_run(effective_run_id)


async def close_docker_sandbox_run(run_id: str) -> None:
    """Release all docker sandbox resources associated with one Agent loop."""
    try:
        await DockerSessionBackend.close_run(run_id)
    except Exception:
        logger.exception(
            "[DockerSession] Failed to close Agent-loop sandbox for run {}",
            run_id,
        )
