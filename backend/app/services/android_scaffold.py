"""Pinned Gradle wrapper provisioning for hand-scaffolded Android projects.

Background: agents often scaffold Android projects by writing files one at a
time, which cannot produce the binary ``gradle-wrapper.jar`` and therefore omit
the Gradle wrapper entirely — the build then fails with "gradlew not found" and
the agent improvises by downloading a jar from the network (supply-chain risk).

This module ships an official Gradle wrapper as in-image static assets
(``android_scaffold_assets/``) and copies it into a project only when the
wrapper is missing, so a missing build entry point is repaired deterministically
instead of left to the model. See
``docs/technical-plans/20260820-p0-execution-verification-hard-layer.md``.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

# SHA256 of the official ``gradle-wrapper.jar`` shipped in
# ``android_scaffold_assets/gradle/wrapper/``. This is the consensus hash shared
# by 12 independent, known-good projects in this platform — i.e. the untouched
# official wrapper, not a model-modified one.
_GRADLE_WRAPPER_JAR_SHA256 = "d3b261c2820e9e3d8d639ed084900f11f4a86050a8f83342ade7b6bc9b0d2bdd"

_ASSETS_DIR = Path(__file__).parent / "android_scaffold_assets"

# A directory counts as a Gradle project if it carries one of these markers.
_BUILD_MARKERS = (
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
)


def _is_gradle_project(project_dir: Path) -> bool:
    return any((project_dir / name).is_file() for name in _BUILD_MARKERS)


def _has_wrapper(project_dir: Path) -> bool:
    has_script = (project_dir / "gradlew").exists() or (project_dir / "gradlew.bat").exists()
    has_jar = (project_dir / "gradle" / "wrapper" / "gradle-wrapper.jar").exists()
    return has_script and has_jar


def provision_gradle_wrapper(project_dir: Path) -> str | None:
    """Fill in the missing Gradle wrapper files, never overwriting existing ones.

    Returns a human-readable note when anything was provisioned, otherwise
    ``None`` (nothing missing, or ``project_dir`` is not a Gradle project).
    """
    if not project_dir.is_dir() or not _is_gradle_project(project_dir):
        return None

    # Fail closed on a tampered/corrupt in-image asset before copying anything.
    jar_src = _ASSETS_DIR / "gradle" / "wrapper" / "gradle-wrapper.jar"
    digest = hashlib.sha256(jar_src.read_bytes()).hexdigest()
    if digest != _GRADLE_WRAPPER_JAR_SHA256:
        return (
            "[自动补 wrapper] 失败：平台 wrapper 资产校验不通过"
            f"（sha256={digest[:12]}…，期望 {_GRADLE_WRAPPER_JAR_SHA256[:12]}…），"
            "已停止自动补全，请人工补齐 gradlew 与 gradle/wrapper/gradle-wrapper.jar。"
        )

    provisioned: list[str] = []

    gradlew_src = _ASSETS_DIR / "gradlew"
    if not (project_dir / "gradlew").exists() and not (project_dir / "gradlew.bat").exists():
        gradlew_dst = project_dir / "gradlew"
        shutil.copyfile(gradlew_src, gradlew_dst)
        gradlew_dst.chmod(0o755)
        provisioned.append("gradlew")

    wrapper_dir = project_dir / "gradle" / "wrapper"
    jar_dst = wrapper_dir / "gradle-wrapper.jar"
    props_dst = wrapper_dir / "gradle-wrapper.properties"
    if not jar_dst.exists() or not props_dst.exists():
        wrapper_dir.mkdir(parents=True, exist_ok=True)
    if not jar_dst.exists():
        shutil.copyfile(jar_src, jar_dst)
        provisioned.append("gradle/wrapper/gradle-wrapper.jar")
    if not props_dst.exists():
        shutil.copyfile(_ASSETS_DIR / "gradle" / "wrapper" / "gradle-wrapper.properties", props_dst)
        provisioned.append("gradle/wrapper/gradle-wrapper.properties")

    if not provisioned:
        return None

    return (
        "[自动补 wrapper] 项目缺少 Gradle wrapper，已写入官方 Gradle 8.10.2 wrapper："
        + ", ".join(provisioned)
        + f"（gradle-wrapper.jar sha256={_GRADLE_WRAPPER_JAR_SHA256[:12]}…）"
    )
