"""Behavior tests for the Android builder's SDK provisioner (sdk-provision.sh).

The provisioner must: detect SDK components declared by a project
(compileSdk / buildToolsVersion / ndkVersion / cmake across kts, groovy,
version-catalog and gradle.properties styles), map package names to Tencent
mirror zip filenames, and install missing components idempotently — so builds
never trigger AGP's dl.google.com auto-download in fake-IP proxied
environments.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


_PROVISION = (
    Path(__file__).resolve().parents[1]
    / "docker"
    / "android-builder"
    / "sdk-provision.sh"
)


def _extract_function(name: str) -> str:
    """Extract one top-level shell function verbatim from sdk-provision.sh."""
    source = _PROVISION.read_text(encoding="utf-8")
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
    assert captured, f"{name}() not found in {_PROVISION}"
    return "\n".join(captured)


def _compose(*, sdk_root: Path, body: str) -> str:
    """Compose a bash script: define functions + SDK_ROOT + body."""
    return "\n".join(
        [
            "set -euo pipefail",
            f'SDK_ROOT="{sdk_root}"',
            f'SDK_MIRROR="https://mirrors.cloud.tencent.com/AndroidSDK"',
            _extract_function("detect_required_sdk_packages"),
            _extract_function("sdk_package_installed"),
            _extract_function("mirror_candidates_for_package"),
            _extract_function("ndk_zip_from_mirror_xml"),
            _extract_function("install_sdk_package"),
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


def _write_project(root: Path) -> None:
    """Sample project mixing declaration styles across files."""
    (root / "app").mkdir(parents=True)
    (root / "gradle").mkdir()
    (root / "app" / "build.gradle.kts").write_text(
        """
plugins { id("com.android.application") }
android {
    compileSdk = 34
    buildToolsVersion = "34.0.0"
    defaultConfig {
        targetSdk = 34
        ndkVersion = "26.1.10909125"
    }
    externalNativeBuild {
        cmake { version = "3.22.1" }
    }
}
""".strip(),
        encoding="utf-8",
    )
    (root / "build.gradle").write_text(
        "android {\n    compileSdkVersion 33\n}\n",
        encoding="utf-8",
    )
    (root / "gradle" / "libs.versions.toml").write_text(
        '[versions]\ncompileSdk = "35"\ntargetSdk = "35"\n',
        encoding="utf-8",
    )
    (root / "gradle.properties").write_text(
        "android.compileSdk=36\n",
        encoding="utf-8",
    )


def test_provisioner_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_PROVISION)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_detects_platforms_across_declaration_styles(tmp_path: Path) -> None:
    _write_project(tmp_path)

    body = "detect_required_sdk_packages | sort -u\n"
    result = _run_bash(_compose(sdk_root=tmp_path / "sdk", body=body), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    out = result.stdout
    # kts compileSdk/targetSdk → 34; groovy compileSdkVersion → 33;
    # version catalog → 35; gradle.properties → 36
    assert "platforms;android-34" in out
    assert "platforms;android-33" in out
    assert "platforms;android-35" in out
    assert "platforms;android-36" in out
    assert "build-tools;34.0.0" in out
    assert "ndk;26.1.10909125" in out
    assert "cmake;3.22.1" in out


def test_detects_nothing_in_plain_project(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "empty"\n')
    body = "detect_required_sdk_packages\n"
    result = _run_bash(_compose(sdk_root=tmp_path / "sdk", body=body), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_sdk_package_installed_checks_dirs(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    (sdk / "platforms" / "android-34").mkdir(parents=True)
    (sdk / "build-tools" / "34.0.0").mkdir(parents=True)

    body = (
        "sdk_package_installed 'platforms;android-34' && echo P34_YES || echo P34_NO\n"
        "sdk_package_installed 'platforms;android-35' && echo P35_YES || echo P35_NO\n"
        "sdk_package_installed 'build-tools;34.0.0' && echo BT_YES || echo BT_NO\n"
    )
    result = _run_bash(_compose(sdk_root=sdk, body=body), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "P34_YES" in result.stdout
    assert "P35_NO" in result.stdout
    assert "BT_YES" in result.stdout


def test_mirror_candidates_for_known_platforms(tmp_path: Path) -> None:
    body = "mirror_candidates_for_package 'platforms;android-36'\n"
    result = _run_bash(_compose(sdk_root=tmp_path / "sdk", body=body), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "platform-36_r02.zip" in result.stdout


def test_mirror_candidates_cover_build_tools_naming_quirk(tmp_path: Path) -> None:
    """≤34 连字符 / ≥35 下划线的镜像命名差异都要覆盖到候选列表。"""
    body = (
        "mirror_candidates_for_package 'build-tools;34.0.0'\n"
        "mirror_candidates_for_package 'build-tools;36.0.0'\n"
        "mirror_candidates_for_package 'build-tools;33.0.1'\n"
    )
    result = _run_bash(_compose(sdk_root=tmp_path / "sdk", body=body), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "build-tools_r34-linux.zip" in out
    assert "build-tools_r36_linux.zip" in out
    # 补丁版本保留完整版本号
    assert "build-tools_r33.0.1-linux.zip" in out


def test_mirror_candidates_guess_future_platform_revisions(tmp_path: Path) -> None:
    """未知 API 版本按 r03→r02→r01 顺序猜测候选。"""
    body = "mirror_candidates_for_package 'platforms;android-37'\n"
    result = _run_bash(_compose(sdk_root=tmp_path / "sdk", body=body), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines == [
        "platform-37_r03.zip",
        "platform-37_r02.zip",
        "platform-37_r01.zip",
    ]


def test_ndk_xml_lookup_parses_archive_url(tmp_path: Path) -> None:
    """ndk_zip_from_mirror_xml 应能从 repository XML 段提取 linux zip 名。"""
    xml = tmp_path / "repository2-3.xml"
    xml.write_text(
        '<remotePackage path="ndk;26.1.10909125">\n'
        "  <archives><archive><host-os>linux</host-os>\n"
        "    <url>android-ndk-r26d-linux.zip</url>\n"
        "  </archive></archives>\n"
        "</remotePackage>\n",
        encoding="utf-8",
    )
    body = (
        f'export SDK_MIRROR_INDEX_CACHE="{xml}"\n'
        "ndk_zip_from_mirror_xml '26.1.10909125'\n"
    )
    result = _run_bash(_compose(sdk_root=tmp_path / "sdk", body=body), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "android-ndk-r26d-linux.zip" in result.stdout


def test_entrypoint_wires_provisioner() -> None:
    entrypoint = _PROVISION.parent / "entrypoint.sh"
    source = entrypoint.read_text(encoding="utf-8")
    assert "sdk-provision.sh" in source
    assert "sync_sdk_components" in source
    # 自愈重试有界: 最多重试一次
    assert source.count('"$@" &') == 2
