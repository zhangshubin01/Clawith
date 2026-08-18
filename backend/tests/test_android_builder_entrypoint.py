"""Behavior tests for the Android builder entrypoint.

Covers the intermediates tmpfs link, the aliyun mirror init-script injection
(google/mavenCentral redirect for fake-IP proxied environments), and the
Gradle download timeout hardening — each as isolated shell functions.
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

_ALIYUN_MIRROR_URLS = (
    "https://maven.aliyun.com/repository/google",
    "https://maven.aliyun.com/repository/public",
    "https://maven.aliyun.com/repository/gradle-plugin",
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


# ─────────────────────────────────────────────────────────
# 阿里云镜像注入 + 下载超时硬化
# ─────────────────────────────────────────────────────────


def _compose_gradle(*, gradle_home: Path, body: str) -> str:
    """Compose a bash script with GRADLE_USER_HOME 指向临时目录。"""
    return "\n".join(
        [
            "set -euo pipefail",
            f'GRADLE_USER_HOME="{gradle_home}"',
            _extract_function("setup_gradle_mirrors"),
            _extract_function("remove_gradle_mirrors"),
            _extract_function("setup_gradle_download_timeouts"),
            body,
        ]
    )


def test_mirror_init_script_written_with_aliyun_repos(tmp_path: Path) -> None:
    gradle_home = tmp_path / "gradle"
    body = "setup_gradle_mirrors\n"
    result = _run_bash(_compose_gradle(gradle_home=gradle_home, body=body), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    script = gradle_home / "init.d" / "aliyun-mirrors.gradle"
    assert script.is_file()
    content = script.read_text(encoding="utf-8")
    for url in _ALIYUN_MIRROR_URLS:
        assert url in content, f"镜像地址缺失: {url}"
    # 镜像必须先于 settings.gradle 注入（beforeSettings 钩子）
    assert "beforeSettings" in content
    assert "pluginManagement" in content
    # 旧 wrapper（Gradle < 6.8）项目跳过 dependencyResolutionManagement，不炸初始化脚本
    assert "getDependencyResolutionManagement" in content


def test_mirror_off_removes_stale_init_script(tmp_path: Path) -> None:
    """关闭开关时必须清理持久卷里的残留脚本，否则旧注入继续生效。"""
    gradle_home = tmp_path / "gradle"
    stale = gradle_home / "init.d" / "aliyun-mirrors.gradle"
    stale.parent.mkdir(parents=True)
    stale.write_text("// stale from a previous container", encoding="utf-8")

    body = "remove_gradle_mirrors\n"
    result = _run_bash(_compose_gradle(gradle_home=gradle_home, body=body), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()


def test_download_timeouts_merged_idempotently(tmp_path: Path) -> None:
    gradle_home = tmp_path / "gradle"
    gradle_home.mkdir()
    props = gradle_home / "gradle.properties"
    props.write_text("org.gradle.caching=true\n", encoding="utf-8")

    body = "setup_gradle_download_timeouts\nsetup_gradle_download_timeouts\n"
    result = _run_bash(_compose_gradle(gradle_home=gradle_home, body=body), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    content = props.read_text(encoding="utf-8")
    # 既有配置不被破坏
    assert "org.gradle.caching=true" in content
    # 幂等：两次调用后每项只出现一次，且值正确
    assert content.count("connectionTimeout=") == 1
    assert content.count("socketTimeout=") == 1
    assert "systemProp.org.gradle.internal.http.connectionTimeout=30000" in content
    assert "systemProp.org.gradle.internal.http.socketTimeout=60000" in content


def test_mirror_gate_defaults_to_on() -> None:
    """镜像注入默认开启，ANDROID_GRADLE_MIRRORS=off 才关闭。"""
    source = _ENTRYPOINT.read_text(encoding="utf-8")
    assert '${ANDROID_GRADLE_MIRRORS:-on}' in source
    assert "setup_gradle_mirrors\n" in source
    assert "remove_gradle_mirrors\n" in source
