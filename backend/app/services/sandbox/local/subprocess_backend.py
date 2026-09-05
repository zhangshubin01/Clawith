"""Local subprocess-based sandbox backend."""

import asyncio
from dataclasses import dataclass
import os
import shlex
import shutil
import signal
import tempfile
import time
import uuid
from pathlib import Path

from loguru import logger

from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.security import check_code_safety
from app.services.sandbox.local.run_workspace import close_run_workspace
from app.services.sandbox.local.shared import (
    VENV_CREATION_TIMEOUT_SECONDS,
    build_safe_env,
    clone_workspace_to_staging,
    ensure_workspace_venv,
    fix_pip_shebangs,
    register_sandbox_staging,
    terminate_and_reap_process,
    unregister_sandbox_staging,
    verify_and_merge_outputs,
    watch_pip_requests,
)
from app.services.workspace_paths import WorkspacePathError, resolve_path_within_root

MAX_STDOUT_CAPTURE_BYTES = 1_000_000
MAX_STDERR_CAPTURE_BYTES = 500_000
SANDBOX_VENV_PATH = "/opt/clawith/venv"
_BWRAP_DONE_PREFIX = "__CLAWITH_BWRAP_DONE__"


@dataclass
class _PersistentBwrapSession:
    run_id: str
    agent_id: uuid.UUID | None
    session_id: str | None
    workspace_mode: str
    publish_paths: tuple[str, ...]
    work_path: Path
    temp_dir: tempfile.TemporaryDirectory
    staging_path: Path
    venv_path: Path
    process: asyncio.subprocess.Process
    pip_stop_event: asyncio.Event
    pip_watcher_task: asyncio.Task
    lock: asyncio.Lock


class SubprocessBackend(BaseSandboxBackend):
    """Local subprocess-based sandbox backend.

    This backend executes code in a subprocess within the agent's workspace.
    It requires bubblewrap-based filesystem isolation for execute_code.
    When bubblewrap is unavailable, code execution fails closed.
    """

    name = "subprocess"
    _bwrap_missing_warned = False
    _run_sessions: dict[str, _PersistentBwrapSession] = {}

    def __init__(self, config: SandboxConfig):
        self.config = config

    @classmethod
    async def close_run(cls, run_id: str) -> None:
        """Stop and remove the bubblewrap process owned by one Agent loop."""
        session = cls._run_sessions.pop(run_id, None)
        unregister_sandbox_staging(run_id)
        if session is None:
            return
        session.pip_stop_event.set()
        try:
            await session.pip_watcher_task
        except (asyncio.CancelledError, Exception):
            pass
        if session.process.returncode is None:
            try:
                if session.process.stdin is not None:
                    session.process.stdin.write(b"exit\n")
                    await session.process.stdin.drain()
                await asyncio.wait_for(session.process.wait(), timeout=2)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                backend = cls(SandboxConfig())
                await backend._terminate_and_reap_process(session.process)
        session.temp_dir.cleanup()

    def _venv_python(self, venv_path: Path) -> str:
        return f"{SANDBOX_VENV_PATH}/bin/python"

    def _host_venv_python(self, work_path: Path) -> str:
        return str(work_path / ".venv" / "bin" / "python")

    def _build_command(self, language: str, script_path: str) -> list[str]:
        if language == "python":
            return [f"{SANDBOX_VENV_PATH}/bin/python", "-I", "-B", str(script_path)]
        if language == "bash":
            return ["bash", "--noprofile", "--norc", "-o", "pipefail", str(script_path)]
        return ["node", str(script_path)]

    def _build_host_command(self, language: str, script_path: Path, work_path: Path) -> list[str]:
        if language == "python":
            return [self._host_venv_python(work_path), "-I", "-B", str(script_path)]
        if language == "bash":
            return ["bash", "--noprofile", "--norc", "-o", "pipefail", str(script_path)]
        return ["node", str(script_path)]

    def _build_safe_env(self, work_path: Path) -> dict[str, str]:
        return build_safe_env(self.config, work_path)

    def _bind_if_exists(self, host_path: str, guest_path: str | None = None, *, read_only: bool = True) -> list[str]:
        host = Path(host_path)
        if not host.exists():
            return []
        target = guest_path or host_path
        bind_flag = "--ro-bind" if read_only else "--bind"
        return [bind_flag, str(host), target]

    async def _terminate_and_reap_process(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate a subprocess group and wait until its direct child is reaped."""
        await terminate_and_reap_process(proc)

    async def _ensure_workspace_venv(self, venv_path: Path) -> None:
        await ensure_workspace_venv(venv_path, timeout=VENV_CREATION_TIMEOUT_SECONDS)

    def _fix_pip_shebangs(self, venv_path: Path) -> None:
        """Replace pip with a bash wrapper that proxies execution to the host if in a sandbox, else delegates to uv pip."""
        fix_pip_shebangs(venv_path)

    def _build_exec_kwargs(self, work_path: Path, timeout: int, use_preexec: bool = False) -> dict:
        kwargs = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": self._build_safe_env(work_path),
            "start_new_session": True,
        }
        if use_preexec:
            kwargs["preexec_fn"] = self._build_preexec_fn(work_path, timeout)
        return kwargs

    def _build_preexec_fn(self, work_path: Path, timeout: int):
        def _preexec():
            os.chdir(work_path)
            os.umask(0o077)

            try:
                import resource

                memory_bytes = int(self.config.memory_limit.rstrip("mM")) * 1024 * 1024
                cpu_limit = max(1, min(timeout, self.config.max_timeout))
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
                resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
                if hasattr(resource, "RLIMIT_CORE"):
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except Exception as exc:
                logger.warning(f"[Subprocess] Failed to apply resource limits: {exc}")

            if hasattr(os, "setgid"):
                try:
                    os.setgid(os.getgid())
                except Exception:
                    pass
            if hasattr(os, "setuid"):
                try:
                    os.setuid(os.getuid())
                except Exception:
                    pass

            if hasattr(os, "chroot") and os.geteuid() == 0:
                try:
                    os.chroot(work_path)
                    os.chdir("/")
                except Exception as exc:
                    logger.warning(f"[Subprocess] Failed to chroot into workspace: {exc}")

        return _preexec

    def _build_bwrap_command(
        self,
        command: list[str],
        work_path: Path,
        venv_path: Path,
        staging_path: Path | None = None,
        writable_path: str | None = None,
    ) -> list[str] | None:
        bwrap = shutil.which("bwrap")
        if not bwrap:
            if not SubprocessBackend._bwrap_missing_warned:
                logger.warning(
                    "[Subprocess] bubblewrap (bwrap) is not available. "
                    "execute_code will be rejected until bubblewrap is installed."
                )
                SubprocessBackend._bwrap_missing_warned = True
            return None

        base_binds = (
            self._bind_if_exists("/usr")
            + self._bind_if_exists("/usr/local")
            + self._bind_if_exists("/bin")
            + self._bind_if_exists("/lib")
            + self._bind_if_exists("/lib64")
            + self._bind_if_exists("/etc")
        )

        if staging_path is not None:
            for directory in ("workspace", "memory", "skills"):
                (staging_path / directory).mkdir(parents=True, exist_ok=True)
            (staging_path / "workspace" / ".tmp").mkdir(parents=True, exist_ok=True)

        cmd = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-cgroup-try",
            *base_binds,
            # Conditional: /data/agents/.uv-cache only exists in the production
            # container. Binding unconditionally would make bwrap fail to start
            # on any other host (Linux dev machines, CI).
            *self._bind_if_exists("/data/agents/.uv-cache", "/uv-cache", read_only=False),
        ]
        if staging_path is not None:
            cmd.extend(
                [
                    "--bind",
                    str(staging_path / "workspace"),
                    "/workspace",
                    "--bind",
                    str(staging_path / "memory"),
                    "/memory",
                    "--bind",
                    str(staging_path / "skills"),
                    "/skills",
                ]
            )
            for root_file in ("focus.md", "soul.md", "HEARTBEAT.md"):
                source = staging_path / root_file
                if source.exists():
                    cmd.extend(["--bind", str(source), f"/{root_file}"])
        else:
            cmd.extend(["--bind", str(work_path), "/workspace"])
        if staging_path is not None and writable_path is not None:
            writable_host = (staging_path / writable_path).resolve()
            if not writable_host.is_relative_to(staging_path.resolve()):
                raise ValueError("Sandbox writable path escapes staging root")
            writable_host.mkdir(parents=True, exist_ok=True)
            cmd.extend(
                [
                    "--setenv",
                    "CLAWITH_SESSION_OUTPUT_DIR",
                    writable_path,
                ]
            )
        cmd.extend(
            [
                "--ro-bind",
                str(venv_path),
                SANDBOX_VENV_PATH,
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--dir",
                "/tmp",
                "--setenv",
                "HOME",
                "/workspace",
                "--setenv",
                "PATH",
                f"{SANDBOX_VENV_PATH}/bin:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "--setenv",
                "TMPDIR",
                "/workspace/.tmp",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
                "--setenv",
                "PYTHONNOUSERSITE",
                "1",
                "--setenv",
                "NODE_PATH",
                "",
                "--setenv",
                "BASH_ENV",
                "",
                "--setenv",
                "ENV",
                "",
                "--setenv",
                "VIRTUAL_ENV",
                SANDBOX_VENV_PATH,
                "--setenv",
                "PIP_CACHE_DIR",
                "/workspace/.tmp/pip-cache",
                "--setenv",
                "PIP_DISABLE_PIP_VERSION_CHECK",
                "1",
                "--setenv",
                "UV_CACHE_DIR",
                "/uv-cache",
            ]
        )
        http_proxy = self.config.http_proxy or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
        https_proxy = self.config.https_proxy or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
        no_proxy = self.config.no_proxy or os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
        if http_proxy:
            cmd.extend(["--setenv", "http_proxy", http_proxy, "--setenv", "HTTP_PROXY", http_proxy])
        if https_proxy:
            cmd.extend(["--setenv", "https_proxy", https_proxy, "--setenv", "HTTPS_PROXY", https_proxy])
        if no_proxy:
            cmd.extend(["--setenv", "no_proxy", no_proxy, "--setenv", "NO_PROXY", no_proxy])

        cmd.append("--chdir")
        cmd.append("/")
        if not self.config.allow_network:
            cmd.append("--unshare-net")
        cmd.extend(command)
        return cmd

    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=256,
            network_available=self.config.allow_network,
            filesystem_available=True,
        )

    async def health_check(self) -> bool:
        """Check if basic system commands are available."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def _terminate_process_group(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the sandbox process group, then SIGKILL stragglers and reap.

        Reaping is mandatory on every path: asyncio only waitpid()s a child
        while a ``proc.wait()`` waiter is active, so a child that exits with
        no waiter stays a zombie.  The parent here is uvicorn (PID 1 in the
        container), which never reaps children it did not explicitly wait on.
        """
        if proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

    async def _watch_pip_requests(self, staging_path: Path, venv_path: Path, stop_event: asyncio.Event) -> None:
        """Watch for pip request files in the staging directory's .tmp and execute them using uv on the host."""
        await watch_pip_requests(staging_path, venv_path, stop_event)

    async def _verify_and_merge_outputs(
        self,
        staging_path: Path,
        target_workspace: Path,
        agent_id: uuid.UUID | None = None,
        session_id: str | None = None,
        publish_paths: list[str] | None = None,
        workspace_mode: str = "merge",
        record_revisions: bool = False,
    ) -> None:
        """Scan staging directory, enforce safety checks, sanitize HTML/SVG, and merge to workspace with DB revisions."""
        await verify_and_merge_outputs(
            staging_path,
            target_workspace,
            agent_id=agent_id,
            session_id=session_id,
            publish_paths=publish_paths,
            workspace_mode=workspace_mode,
            record_revisions=record_revisions,
        )

    def _clone_workspace_to_staging(self, source: Path, dest: Path) -> None:
        """Clone all workspace files to staging area, ignoring virtualenv and tmp folders."""
        clone_workspace_to_staging(source, dest)

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
    ) -> _PersistentBwrapSession | None:
        temp_dir = tempfile.TemporaryDirectory(prefix=f"clawith-bwrap-{run_id[:8]}-")
        staging_path = Path(temp_dir.name)
        self._clone_workspace_to_staging(work_path, staging_path)
        (staging_path / "workspace" / ".tmp" / "pip-cache").mkdir(
            parents=True,
            exist_ok=True,
        )
        writable_path = publish_paths[0] if workspace_mode == "isolated_output" and publish_paths else None
        bwrap_command = self._build_bwrap_command(
            ["bash", "--noprofile", "--norc"],
            work_path,
            venv_path,
            staging_path=staging_path,
            writable_path=writable_path,
        )
        if bwrap_command is None:
            temp_dir.cleanup()
            return None
        process = await asyncio.create_subprocess_exec(
            *bwrap_command,
            cwd=str(work_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_safe_env(work_path),
            start_new_session=True,
        )
        pip_stop_event = asyncio.Event()
        pip_watcher_task = asyncio.create_task(self._watch_pip_requests(staging_path, venv_path, pip_stop_event))
        persistent = _PersistentBwrapSession(
            run_id=run_id,
            agent_id=agent_id,
            session_id=session_id,
            workspace_mode=workspace_mode,
            publish_paths=tuple(publish_paths or ()),
            work_path=work_path.resolve(),
            temp_dir=temp_dir,
            staging_path=staging_path,
            venv_path=venv_path,
            process=process,
            pip_stop_event=pip_stop_event,
            pip_watcher_task=pip_watcher_task,
            lock=asyncio.Lock(),
        )
        SubprocessBackend._run_sessions[run_id] = persistent
        register_sandbox_staging(
            run_id,
            persistent.staging_path,
            persistent.lock,
            workspace_mode=persistent.workspace_mode,
            publish_paths=persistent.publish_paths,
        )
        return persistent

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
    ) -> _PersistentBwrapSession | None:
        existing = SubprocessBackend._run_sessions.get(run_id)
        expected_paths = tuple(publish_paths or ())
        if existing is not None and (
            existing.process.returncode is not None
            or existing.agent_id != agent_id
            or existing.session_id != session_id
            or existing.workspace_mode != workspace_mode
            or existing.publish_paths != expected_paths
            or existing.work_path != work_path.resolve()
        ):
            await SubprocessBackend.close_run(run_id)
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
        session: _PersistentBwrapSession,
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
        marker = f"{_BWRAP_DONE_PREFIX}{token}:"
        shell_line = (
            f"{shlex.join(command)} >{shlex.quote('/workspace/.tmp/' + stdout_path.name)} "
            f"2>{shlex.quote('/workspace/.tmp/' + stderr_path.name)}; "
            f"__clawith_rc=$?; printf '{marker}%s\\n' \"$__clawith_rc\"\n"
        )
        process = session.process
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Persistent bubblewrap control pipes are unavailable")
        process.stdin.write(shell_line.encode("utf-8"))
        await process.stdin.drain()

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
            async with asyncio.timeout(timeout):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        detail = ""
                        if process.stderr is not None:
                            detail = (await process.stderr.read()).decode(
                                "utf-8",
                                errors="replace",
                            )[:500]
                        raise RuntimeError(
                            "Persistent bubblewrap exited before command settlement" + (f": {detail}" if detail else "")
                        )
                    decoded = line.decode("utf-8", errors="replace").strip()
                    if decoded.startswith(marker):
                        exit_code = int(decoded.removeprefix(marker))
                        break
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate_and_reap_process(process)
            exit_code = 124
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

    async def execute(
        self, code: str, language: str, timeout: int = 30, work_dir: str | None = None, **kwargs
    ) -> ExecutionResult:
        """Execute code in a subprocess."""
        import uuid

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
        proc: asyncio.subprocess.Process | None = None

        # Validate language
        if language not in ("python", "bash", "node"):
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}. Use: python, bash, or node",
            )

        # Security check - pass allow_network config
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

        # Determine work directory and ensure it cannot escape its own root.
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

        # Determine persistent venv path if possible
        if agent_id:
            venv_path = Path("/data/agents").resolve() / str(agent_id) / ".venv"
            venv_path.parent.mkdir(parents=True, exist_ok=True)
            uv_cache = Path("/data/agents/.uv-cache")
            uv_cache.mkdir(parents=True, exist_ok=True)
        else:
            venv_path = work_path / ".venv"

        try:
            await self._ensure_workspace_venv(venv_path)
            if isinstance(run_id, str) and run_id:
                persistent = await self._persistent_session(
                    run_id=run_id,
                    work_path=work_path,
                    venv_path=venv_path,
                    agent_id=agent_id,
                    session_id=session_id,
                    workspace_mode=workspace_mode,
                    publish_paths=publish_paths,
                )
                if persistent is not None:
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
                            await self._verify_and_merge_outputs(
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
                                error=(f"sandbox_publication_unknown: {type(exc).__name__}"),
                            )
                        finally:
                            if is_timeout:
                                await SubprocessBackend.close_run(run_id)
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
                if workspace_mode == "isolated_output" or not self.config.allow_unsafe_fallback_when_bwrap_missing:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error=("bubblewrap (bwrap) is required for execute_code but is not available."),
                    )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=(f"sandbox_persistent_execution_failed: {type(exc).__name__}: {str(exc)[:300]}"),
            )

        # Legacy calls without a Runtime Run retain one-shot isolation.
        staging_id = str(uuid.uuid4())
        staging_path = work_path / ".tmp" / f"staging_{staging_id}"
        self._clone_workspace_to_staging(work_path, staging_path)
        (staging_path / "workspace" / ".tmp").mkdir(parents=True, exist_ok=True)

        # Determine command and file extension
        if language == "python":
            ext = ".py"
        elif language == "bash":
            ext = ".sh"
        elif language == "node":
            ext = ".js"

        # Write code to temp file inside real work_path (read-only bound to guest /workspace via staging copy)
        # Note: script_path must be written inside staging_path so sandbox can see and run it!
        script_path = staging_path / "workspace" / ".tmp" / f"_exec_tmp{ext}"

        try:
            script_path.write_text(code, encoding="utf-8")

            # Start background task to watch for pip requests
            pip_stop_event = asyncio.Event()
            pip_watcher_task = asyncio.create_task(self._watch_pip_requests(staging_path, venv_path, pip_stop_event))

            sandbox_command = self._build_command(language, f"/workspace/.tmp/{script_path.name}")
            writable_path = publish_paths[0] if workspace_mode == "isolated_output" and publish_paths else None
            bwrap_command = self._build_bwrap_command(
                sandbox_command,
                work_path,
                venv_path,
                staging_path=staging_path,
                writable_path=writable_path,
            )
            if not bwrap_command:
                if workspace_mode == "isolated_output" or not self.config.allow_unsafe_fallback_when_bwrap_missing:
                    duration_ms = int((time.time() - start_time) * 1000)
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=duration_ms,
                        error=(
                            "bubblewrap (bwrap) is required for execute_code but is not available. "
                            "Install bwrap in the runtime environment or enable "
                            "allow_unsafe_fallback_when_bwrap_missing for local development."
                        ),
                    )

                # Fallback path runs on host script inside staging_path to prevent polluting real workspace
                host_command = self._build_host_command(language, script_path, staging_path)
                logger.warning("[Subprocess] bubblewrap missing; using local fallback without filesystem isolation")
                proc = await asyncio.create_subprocess_exec(
                    *host_command,
                    cwd=str(staging_path),
                    **self._build_exec_kwargs(staging_path, timeout, use_preexec=True),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *bwrap_command,
                    cwd=str(work_path),
                    **self._build_exec_kwargs(work_path, timeout),
                )

            stdout_data = bytearray()
            stderr_data = bytearray()

            async def read_stream(stream, out, label="stdout"):
                capture_limit = MAX_STDERR_CAPTURE_BYTES if label == "stderr" else MAX_STDOUT_CAPTURE_BYTES
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    remaining = capture_limit - len(out)
                    if remaining > 0:
                        out.extend(chunk[:remaining])
                    if on_output:
                        try:
                            text = chunk.decode("utf-8", errors="replace")
                            await on_output(text, label)
                        except Exception:
                            pass

            task1 = asyncio.create_task(read_stream(proc.stdout, stdout_data, "stdout"))
            task2 = asyncio.create_task(read_stream(proc.stderr, stderr_data, "stderr"))

            is_timeout = False
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=timeout)
            except asyncio.TimeoutError:
                is_timeout = True
                await self._terminate_and_reap_process(proc)

            await asyncio.gather(task1, task2)
            stdout = bytes(stdout_data)
            stderr = bytes(stderr_data)

            stdout_str = stdout.decode("utf-8", errors="replace")[:10000] if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace")[:5000] if stderr else ""

            duration_ms = int((time.time() - start_time) * 1000)

            # Stop pip watcher before verification
            try:
                pip_stop_event.set()
                await pip_watcher_task
            except Exception:
                pass

            # Safe verification and merge of output files (run for both bwrap and fallback execution)
            try:
                if publication_owner == "gateway" and before_gateway_publish is not None:
                    if not await before_gateway_publish():
                        raise RuntimeError("Sandbox publication ownership could not be verified")
                await self._verify_and_merge_outputs(
                    staging_path,
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

            if is_timeout:
                return ExecutionResult(
                    success=False,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    exit_code=124,
                    duration_ms=duration_ms,
                    error=f"Code execution timed out after {timeout}s. If you expect this code to take longer, try calling the tool again with a higher 'timeout' parameter (up to 3600s).",
                )

            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                error=None if proc.returncode == 0 else f"Exit code: {proc.returncode}",
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.exception("[Subprocess] Execution error")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"Execution error: {str(e)[:200]}",
            )

        finally:
            if proc is not None and proc.returncode is None:
                try:
                    await self._terminate_and_reap_process(proc)
                except Exception:
                    logger.exception("[Subprocess] Failed to reap sandbox process during cleanup")

            # Stop the pip watcher task
            if "pip_stop_event" in locals() and "pip_watcher_task" in locals():
                try:
                    pip_stop_event.set()
                    await pip_watcher_task
                except Exception:
                    pass
            # Clean up temp script inside staging if not done
            if "script_path" in locals():
                try:
                    script_path.unlink(missing_ok=True)
                except Exception:
                    pass
            # Clean up staging folder
            if "staging_path" in locals():
                try:
                    if staging_path.exists():
                        shutil.rmtree(staging_path)
                except Exception:
                    pass


async def close_subprocess_sandbox_run(run_id: str) -> None:
    """Release all local sandbox resources associated with one Agent loop."""
    try:
        await SubprocessBackend.close_run(run_id)
    except Exception:
        logger.exception(
            "[Subprocess] Failed to close Agent-loop sandbox for run {}",
            run_id,
        )
    finally:
        try:
            await close_run_workspace(run_id)
        except Exception:
            logger.exception(
                "[Subprocess] Failed to discard Agent-loop workspace for run {}",
                run_id,
            )
