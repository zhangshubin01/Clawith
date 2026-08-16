"""Behavior tests for the Android builder entrypoint's intermediates link.

The entrypoint must never leave a dangling ``app/build/intermediates``
symlink on the persistent workspace: it may only create the link when the
path does not exist at all, must never nest it inside a real directory, and
must remove exactly the link it created on exit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


_ENTRYPOINT = (
    Path(__file__).resolve().parents[1]
    / "docker"
    / "android-builder"
    / "entrypoint.sh"
)


def _extract_function(name: str) -> str:
    """Extract one top-level shell function verbatim from entrypoint.sh."""
    source = _ENTRYPOINT.read_text(encoding="utf-8")
    lines = source.splitlines()
    captured: list[str] = []
    capturing = False
    for line in lines:
        if not capturing and line.startswith(f"{name}()"):
            capturing = True
        if capturing:
            captured.append(line)
            if line == "}":
                break
    assert captured, f"{name}() not found in {_ENTRYPOINT}"
    return "\n".join(captured)


def _compose(*, shm_dir: Path, body: str) -> str:
    """Compose a bash script around the real functions from entrypoint.sh."""
    return "\n".join(
        [
            "set -euo pipefail",
            f'TMPFS_INTERMEDIATES="{shm_dir}"',
            "CREATED_INTERMEDIATES_LINK=0",
            _extract_function("setup_intermediates_link"),
            _extract_function("cleanup_intermediates_link"),
            "trap cleanup_intermediates_link EXIT",
            body,
        ]
    )


def _run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_entrypoint_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_ENTRYPOINT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_intermediates_directory_is_never_nested(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    real = workdir / "app" / "build" / "intermediates" / "real"
    real.mkdir(parents=True)
    shm = tmp_path / "shm"

    script = _compose(shm_dir=shm, body="setup_intermediates_link")
    result = _run_bash(script, cwd=workdir)

    assert result.returncode == 0, result.stderr
    link = workdir / "app" / "build" / "intermediates"
    assert not link.is_symlink()
    assert (link / "real").is_dir()
    # 事故根因回归：不得在真实目录内部创建 intermediates/intermediates 嵌套链接
    assert not (link / "intermediates").exists()
    assert not (link / "intermediates").is_symlink()


def test_missing_intermediates_link_is_created_and_cleaned_on_exit(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "work"
    build = workdir / "app" / "build"
    build.mkdir(parents=True)
    shm = tmp_path / "shm"

    body = (
        "setup_intermediates_link\n"
        'test -L app/build/intermediates\n'
        'test "$(readlink app/build/intermediates)" = "$TMPFS_INTERMEDIATES"\n'
    )
    result = _run_bash(_compose(shm_dir=shm, body=body), cwd=workdir)

    assert result.returncode == 0, result.stderr
    # EXIT trap 移除本容器创建的链接；build 目录与 tmpfs 目标不受影响
    assert not (build / "intermediates").exists()
    assert not (build / "intermediates").is_symlink()
    assert build.is_dir()
    assert shm.is_dir()


def test_preexisting_dangling_link_is_not_replaced(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    build = workdir / "app" / "build"
    build.mkdir(parents=True)
    dangling = build / "intermediates"
    dangling.symlink_to("/nonexistent-target")
    shm = tmp_path / "shm"

    body = (
        "setup_intermediates_link\n"
        'test -L app/build/intermediates\n'
        'test "$(readlink app/build/intermediates)" = "/nonexistent-target"\n'
    )
    result = _run_bash(_compose(shm_dir=shm, body=body), cwd=workdir)

    assert result.returncode == 0, result.stderr
    assert dangling.is_symlink()
    assert not dangling.exists()  # 仍为断链，未被重建
    # 且未被本容器清理（不是本容器创建的）
    assert not (build / "intermediates" / "intermediates").exists()


def test_missing_build_dir_is_a_noop(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    shm = tmp_path / "shm"

    result = _run_bash(_compose(shm_dir=shm, body="setup_intermediates_link"), cwd=workdir)

    assert result.returncode == 0, result.stderr
    assert not (workdir / "app").exists()
    assert shm.is_dir()


def test_entrypoint_defines_required_functions() -> None:
    source = _ENTRYPOINT.read_text(encoding="utf-8")
    assert "setup_intermediates_link()" in source
    assert "cleanup_intermediates_link()" in source
    assert '${ANDROID_TMPFS_DIR:-/dev/shm/intermediates}' in source
    assert "cleanup_intermediates_link;" in source  # EXIT trap 链式注册
