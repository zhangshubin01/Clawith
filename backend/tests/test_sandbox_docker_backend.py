"""Docker session backend tests — driven by a fake docker client."""

import re
import threading
import time
from pathlib import Path

import pytest
from docker import errors

from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.local import docker_backend
from app.services.sandbox.local.docker_backend import (
    SANDBOX_VENV_PATH,
    DockerSessionBackend,
    close_docker_sandbox_run,
)
from app.services.sandbox.registry import get_sandbox_backend


class FakeExecResult:
    def __init__(self, output: bytes, exit_code: int):
        self.output = output
        self.exit_code = exit_code


class FakeContainer:
    """Stands in for docker.models.containers.Container."""

    def __init__(self, status: str = "running"):
        self.status = status
        self.exec_calls: list[tuple[list[str], dict]] = []
        self.killed = False
        self.removed = False
        self.host_volumes: dict[str, Path] = {}
        self._exit_code = 0
        self._stdout = b"hello\n"
        self._stderr = b""
        self._block_event: threading.Event | None = None
        self.omit_marker = False
        self.exec_delay: float = 0

    def reload(self):
        if self.status == "gone":
            raise errors.NotFound("container gone")

    def _host_path(self, guest: str) -> Path:
        for bind, host in self.host_volumes.items():
            if guest.startswith(bind):
                return host / guest[len(bind) :].lstrip("/")
        raise AssertionError(f"unmapped guest path {guest}")

    def exec_run(self, cmd, **kwargs):
        self.exec_calls.append((cmd, kwargs))
        shell = cmd[-1]
        token = re.search(r"_exec_stdout_([0-9a-f]+)", shell)
        assert token, f"shell line missing token: {shell}"
        tok = token.group(1)
        if self._block_event is not None:
            # Simulate a long-running command that only ends when killed.
            assert self._block_event.wait(timeout=5)
            raise errors.APIError("container killed")
        if self.exec_delay:
            # Simulate a slow command: output files exist while exec is still
            # running, so the streaming task can observe them mid-flight.
            time.sleep(self.exec_delay)
        m_out = re.search(r">(/[^ ]+)", shell)
        m_err = re.search(r"2>(/[^ ]+)", shell)
        if m_out:
            self._host_path(m_out.group(1)).write_bytes(self._stdout)
        if m_err:
            self._host_path(m_err.group(1)).write_bytes(self._stderr)
        marker = "" if self.omit_marker else f"__CLAWITH_EXEC_DONE__{tok}:{self._exit_code}"
        return FakeExecResult(marker.encode(), self._exit_code)

    def kill(self):
        self.killed = True
        self.status = "exited"
        if self._block_event is not None:
            self._block_event.set()

    def remove(self, force=False):
        self.removed = True
        self.status = "gone"


class FakeClient:
    """Stands in for docker.DockerClient; every run() yields a fresh container."""

    def __init__(self):
        self.container: FakeContainer | None = None
        self.run_kwargs: list[tuple[str, dict]] = []
        self.pulled: list[str] = []
        self._fail_run_with: Exception | None = None
        # Applied to every container this client creates.
        self._exec_delay: float = 0
        self._omit_marker = False

    @property
    def containers(self):
        return self

    @property
    def images(self):
        return self

    def ping(self):
        return True

    def get(self, image):
        raise errors.ImageNotFound("not present")

    def pull(self, image):
        self.pulled.append(image)

    def run(self, image, **kwargs):
        if self._fail_run_with is not None:
            raise self._fail_run_with
        self.run_kwargs.append((image, kwargs))
        self.container = FakeContainer()
        self.container.host_volumes = {spec["bind"]: Path(host) for host, spec in kwargs["volumes"].items()}
        self.container.exec_delay = self._exec_delay
        self.container.omit_marker = self._omit_marker
        return self.container


@pytest.fixture(autouse=True)
async def _clean_sessions():
    """DockerSessionBackend._run_sessions is class-level: isolate tests."""
    DockerSessionBackend._run_sessions = {}
    yield
    for run_id in list(DockerSessionBackend._run_sessions):
        await DockerSessionBackend.close_run(run_id)
    DockerSessionBackend._run_sessions = {}


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    # Also feeds _detect_host_agent_data_root (self-inspect): FakeClient.get
    # raises ImageNotFound → detection returns "" (passthrough).
    monkeypatch.setattr(docker_backend, "get_docker_client", lambda: client)

    async def _noop_venv(venv_path, **kwargs):
        (venv_path / "bin").mkdir(parents=True, exist_ok=True)
        (venv_path / "bin" / "python").write_text("", encoding="utf-8")

    monkeypatch.setattr(docker_backend, "ensure_workspace_venv", _noop_venv)
    return client


@pytest.fixture
def backend(fake_client, monkeypatch):
    monkeypatch.setattr(docker_backend, "_SESSION_START_TIMEOUT_SECONDS", 0.5)
    return DockerSessionBackend(SandboxConfig())


@pytest.mark.asyncio
async def test_registry_maps_docker_type_to_session_backend() -> None:
    backend = get_sandbox_backend(SandboxConfig(type=SandboxType.DOCKER))
    assert isinstance(backend, DockerSessionBackend)
    assert backend.name == "docker"


@pytest.mark.asyncio
async def test_persistent_session_reused_for_same_run(backend, fake_client, tmp_path: Path) -> None:
    kwargs = {
        "run_id": "run-abc",
        "agent_id": None,
        "session_id": None,
        "workspace_mode": "merge",
        "publish_paths": None,
    }
    r1 = await backend.execute("print('a')", "python", timeout=30, work_dir=str(tmp_path), **kwargs)
    assert r1.success, r1.error
    assert "hello" in r1.stdout
    r2 = await backend.execute("print('b')", "python", timeout=30, work_dir=str(tmp_path), **kwargs)
    assert r2.success, r2.error
    assert len(fake_client.run_kwargs) == 1
    assert len(fake_client.container.exec_calls) == 2
    await DockerSessionBackend.close_run("run-abc")


@pytest.mark.asyncio
async def test_dead_container_triggers_session_rebuild(backend, fake_client, tmp_path: Path) -> None:
    kwargs = {
        "run_id": "run-dead",
        "agent_id": None,
        "session_id": None,
        "workspace_mode": "merge",
        "publish_paths": None,
    }
    await backend.execute("print('a')", "python", timeout=30, work_dir=str(tmp_path), **kwargs)
    fake_client.container.status = "gone"  # auto-removed after exit
    r2 = await backend.execute("print('b')", "python", timeout=30, work_dir=str(tmp_path), **kwargs)
    assert r2.success, r2.error
    assert len(fake_client.run_kwargs) == 2
    await DockerSessionBackend.close_run("run-dead")


@pytest.mark.asyncio
async def test_container_isolation_defaults(backend, fake_client, tmp_path: Path) -> None:
    await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-iso")
    _, kwargs = fake_client.run_kwargs[0]
    assert fake_client.run_kwargs[0][0] == "clawith-code-sandbox:latest"
    assert kwargs["detach"] is True
    assert kwargs["user"] == "1000:1000"
    # "none" (string), not None: docker-py serializes None as the 'default'
    # bridge network, which would silently give the sandbox networking.
    assert kwargs["network_mode"] == "none"
    assert kwargs["labels"] == {"clawith.sandbox": "execute-code", "clawith.run_id": "run-iso"}
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["read_only"] is True
    assert kwargs["mem_limit"] == "256m"
    assert kwargs["pids_limit"] == 64
    assert kwargs["auto_remove"] is True
    assert kwargs["working_dir"] == "/workspace"
    assert kwargs["environment"]["HOME"] == "/workspace"
    assert kwargs["environment"]["VIRTUAL_ENV"] == SANDBOX_VENV_PATH
    workspace_binds = [spec for spec in kwargs["volumes"].values() if spec["bind"] == "/workspace"]
    assert len(workspace_binds) == 1
    assert workspace_binds[0]["mode"] == "rw"
    venv_binds = [v for v, spec in kwargs["volumes"].items() if spec["bind"] == SANDBOX_VENV_PATH]
    assert len(venv_binds) == 1
    assert kwargs["volumes"][venv_binds[0]]["mode"] == "ro"
    await DockerSessionBackend.close_run("run-iso")


@pytest.mark.asyncio
async def test_allow_network_uses_bridge(backend, fake_client, tmp_path: Path) -> None:
    backend.config = SandboxConfig(allow_network=True)
    await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-net")
    _, kwargs = fake_client.run_kwargs[0]
    assert kwargs["network_mode"] == "bridge"
    await DockerSessionBackend.close_run("run-net")


@pytest.mark.asyncio
async def test_timeout_kills_container_and_resets_session(backend, fake_client, tmp_path: Path) -> None:
    # Wire the block into the container factory: containers created while
    # _block_next is set get a blocking exec.
    original_run = fake_client.run

    def blocking_run(image, **kwargs):
        if getattr(fake_client, "_block_next", False):
            fake_client._block_next = False
            result = original_run(image, **kwargs)
            result._block_event = threading.Event()
            return result
        return original_run(image, **kwargs)

    fake_client.run = blocking_run
    fake_client._block_next = True

    r = await backend.execute("while True: pass", "python", timeout=0.2, work_dir=str(tmp_path), run_id="run-tmo")
    assert r.success is False
    assert r.exit_code == 124
    assert fake_client.container.killed is True
    assert DockerSessionBackend._run_sessions.get("run-tmo") is None
    r2 = await backend.execute("print('ok')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-tmo")
    assert r2.success, r2.error
    assert len(fake_client.run_kwargs) == 2
    await DockerSessionBackend.close_run("run-tmo")


@pytest.mark.asyncio
async def test_close_docker_sandbox_run_removes_container_and_staging(backend, fake_client, tmp_path: Path) -> None:
    await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-close")
    session = DockerSessionBackend._run_sessions["run-close"]
    staging = session.staging_path
    assert staging.exists()
    await close_docker_sandbox_run("run-close")
    assert fake_client.container.removed is True
    assert not staging.exists()


@pytest.mark.asyncio
async def test_one_shot_without_run_id_closes_ephemeral_session(backend, fake_client, tmp_path: Path) -> None:
    r = await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path))
    assert r.success, r.error
    assert fake_client.container.removed is True
    assert not DockerSessionBackend._run_sessions


@pytest.mark.asyncio
async def test_container_start_failure_reports_detail(backend, fake_client, tmp_path: Path) -> None:
    fake_client._fail_run_with = RuntimeError("boom: image not found")
    r = await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-fail")
    assert r.success is False
    assert "RuntimeError" in r.error
    assert "boom: image not found" in r.error


@pytest.mark.asyncio
async def test_isolated_output_sets_session_output_dir(backend, fake_client, tmp_path: Path) -> None:
    backend.config = SandboxConfig(workspace_mode="isolated_output")
    (tmp_path / "session-out").mkdir()
    r = await backend.execute(
        "print('x')",
        "python",
        timeout=30,
        work_dir=str(tmp_path),
        run_id="run-io",
        workspace_mode="isolated_output",
        publish_paths=["session-out"],
    )
    assert r.success, r.error
    _, kwargs = fake_client.run_kwargs[0]
    assert kwargs["environment"]["CLAWITH_SESSION_OUTPUT_DIR"] == "session-out"
    session = DockerSessionBackend._run_sessions["run-io"]
    assert (session.staging_path / "session-out").is_dir()
    await DockerSessionBackend.close_run("run-io")


@pytest.mark.asyncio
async def test_on_output_streams_stdout_during_execution(backend, fake_client, tmp_path: Path) -> None:
    fake_client._exec_delay = 0.3
    chunks: list[tuple[str, str]] = []

    async def on_output(text: str, stream: str) -> None:
        chunks.append((text, stream))

    r = await backend.execute(
        "print('hi')",
        "python",
        timeout=30,
        work_dir=str(tmp_path),
        run_id="run-stream",
        on_output=on_output,
    )
    assert r.success, r.error
    assert chunks, "streaming callback never fired"
    assert "hello" in "".join(text for text, _ in chunks)
    assert any(stream == "stdout" for _, stream in chunks)
    await DockerSessionBackend.close_run("run-stream")


@pytest.mark.asyncio
async def test_missing_marker_falls_back_to_transport_exit_code(backend, fake_client, tmp_path: Path) -> None:
    fake_client._omit_marker = True
    r = await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-marker")
    assert r.exit_code == 0, "healthy 0 must not be coerced to 1 when the marker is missing"
    assert r.success, r.error

    fake_client.container._exit_code = None
    r2 = await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-marker")
    assert r2.exit_code == 1, "only an unavailable transport exit code falls back to 1"
    assert r2.success is False
    await DockerSessionBackend.close_run("run-marker")


@pytest.mark.asyncio
async def test_language_command_mapping(backend, fake_client, tmp_path: Path) -> None:
    expected = (
        ("python", f"{SANDBOX_VENV_PATH}/bin/python -I -B"),
        ("bash", "bash --noprofile --norc -o pipefail"),
        ("node", "node"),
    )
    for language, prefix in expected:
        r = await backend.execute(
            "x = 1",
            language,
            timeout=30,
            work_dir=str(tmp_path),
            run_id=f"run-lang-{language}",
        )
        assert r.success, r.error
        shell = fake_client.container.exec_calls[-1][0][-1]
        assert shell.startswith(prefix), (language, shell)
    for language, _ in expected:
        await DockerSessionBackend.close_run(f"run-lang-{language}")


@pytest.mark.asyncio
async def test_invalid_cpu_limit_falls_back_to_half_cpu(backend, fake_client, tmp_path: Path) -> None:
    backend.config = SandboxConfig(cpu_limit="bogus")
    await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-cpu")
    _, kwargs = fake_client.run_kwargs[0]
    assert kwargs["cpu_quota"] == 50000
    await DockerSessionBackend.close_run("run-cpu")


def test_build_container_kwargs_translates_agent_data_paths(tmp_path: Path, monkeypatch) -> None:
    """DooD: /data/agents mount sources must be rewritten to the host path."""
    monkeypatch.setattr(docker_backend, "get_docker_client", lambda: FakeClient())
    backend = DockerSessionBackend(SandboxConfig())
    backend._host_agent_data_root = "/HOSTAGENTS"
    staging = tmp_path / "staging"
    staging.mkdir()
    kwargs = backend._build_container_kwargs(
        name="c",
        run_id="r",
        staging_path=staging,
        venv_path=Path("/data/agents/agent-x/.venv"),
        writable_path=None,
    )
    host_venv = [src for src, spec in kwargs["volumes"].items() if spec["bind"] == SANDBOX_VENV_PATH]
    assert host_venv == ["/HOSTAGENTS/agent-x/.venv"]
    # Staging under a non-/data/agents path is passed through unchanged.
    workspace_src = [src for src, spec in kwargs["volumes"].items() if spec["bind"] == "/workspace"]
    assert workspace_src == [str(staging / "workspace")]


def test_build_container_kwargs_passthrough_without_host_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(docker_backend, "get_docker_client", lambda: FakeClient())
    backend = DockerSessionBackend(SandboxConfig())
    backend._host_agent_data_root = ""
    staging = tmp_path / "staging"
    staging.mkdir()
    kwargs = backend._build_container_kwargs(
        name="c",
        run_id="r",
        staging_path=staging,
        venv_path=Path("/data/agents/agent-x/.venv"),
        writable_path=None,
    )
    host_venv = [src for src, spec in kwargs["volumes"].items() if spec["bind"] == SANDBOX_VENV_PATH]
    assert host_venv == ["/data/agents/agent-x/.venv"]


@pytest.mark.asyncio
async def test_staging_lives_under_agent_data_when_present(backend, fake_client, tmp_path: Path, monkeypatch) -> None:
    # Sibling of the workspace (production geometry: /data/agents/.sandbox-staging
    # vs /data/agents/<agent>/...); a staging dir inside the work dir would make
    # clone_workspace_to_staging copy itself recursively.
    base = tmp_path.parent / (tmp_path.name + "-staging")
    base.mkdir()
    monkeypatch.setattr(docker_backend, "_staging_parent", lambda: base)
    await backend.execute("print('x')", "python", timeout=30, work_dir=str(tmp_path), run_id="run-stg")
    session = DockerSessionBackend._run_sessions["run-stg"]
    assert session.temp_dir is None
    assert session.staging_path.is_relative_to(base)
    assert (session.staging_path / "workspace").is_dir()
    # Mount sources come from the staging base (still untranslated: no host root).
    _, kwargs = fake_client.run_kwargs[0]
    assert any(str(session.staging_path / "workspace") == src for src in kwargs["volumes"])
    await DockerSessionBackend.close_run("run-stg")
    assert not session.staging_path.exists()


def test_detect_host_agent_data_root_resolves_mount(monkeypatch) -> None:
    class FakeInspectClient:
        def __init__(self):
            self.containers = self

        def get(self, name):
            assert name  # non-empty hostname
            container = type("C", (), {})()
            container.attrs = {"Mounts": [{"Destination": "/data/agents", "Source": "/HOSTROOT/agents"}]}
            return container

    monkeypatch.setattr(docker_backend, "get_docker_client", lambda: FakeInspectClient())
    assert docker_backend._detect_host_agent_data_root() == "/HOSTROOT/agents"


def test_detect_host_agent_data_root_missing_mount_returns_empty(monkeypatch) -> None:
    class FakeNoMountClient:
        def __init__(self):
            self.containers = self

        def get(self, name):
            container = type("C", (), {})()
            container.attrs = {"Mounts": [{"Destination": "/other", "Source": "/x"}]}
            return container

    monkeypatch.setattr(docker_backend, "get_docker_client", lambda: FakeNoMountClient())
    assert docker_backend._detect_host_agent_data_root() == ""
