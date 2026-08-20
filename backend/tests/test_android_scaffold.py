"""B2 — pinned Gradle wrapper provisioning (android_scaffold)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from app.services.android_scaffold import (
    _ASSETS_DIR,
    _GRADLE_WRAPPER_JAR_SHA256,
    provision_gradle_wrapper,
)


def _make_project(tmp_path: Path, *, markers: tuple[str, ...] = ("settings.gradle.kts",)) -> Path:
    p = tmp_path / "app"
    p.mkdir()
    for m in markers:
        (p / m).write_text("// marker", encoding="utf-8")
    return p


def test_provisions_missing_wrapper_into_gradle_project(tmp_path: Path) -> None:
    p = _make_project(tmp_path)
    note = provision_gradle_wrapper(p)
    assert note is not None
    assert "[自动补 wrapper]" in note
    assert (p / "gradlew").exists()
    assert (p / "gradle" / "wrapper" / "gradle-wrapper.jar").exists()
    assert (p / "gradle" / "wrapper" / "gradle-wrapper.properties").exists()
    assert os.access(p / "gradlew", os.X_OK), "gradlew 应为可执行"


def test_returns_none_for_non_gradle_dir(tmp_path: Path) -> None:
    p = tmp_path / "empty"
    p.mkdir()
    assert provision_gradle_wrapper(p) is None
    assert not (p / "gradlew").exists()


def test_returns_none_when_wrapper_already_present(tmp_path: Path) -> None:
    p = _make_project(tmp_path)
    assert provision_gradle_wrapper(p) is not None
    # 第二次调用：无缺失文件 → None（幂等，不重写）
    assert provision_gradle_wrapper(p) is None


def test_does_not_overwrite_existing_gradlew(tmp_path: Path) -> None:
    p = _make_project(tmp_path)
    (p / "gradlew").write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    note = provision_gradle_wrapper(p)
    # 已有 gradlew → 不覆盖；jar/props 仍补
    assert (p / "gradlew").read_text(encoding="utf-8").startswith("#!/bin/sh\necho custom")
    assert note is not None
    assert (p / "gradle" / "wrapper" / "gradle-wrapper.jar").exists()


def test_shipped_jar_matches_pinned_sha256() -> None:
    """仓库内随附的 gradle-wrapper.jar 必须与 pinned SHA256 一致（供应链守卫）。"""
    jar = _ASSETS_DIR / "gradle" / "wrapper" / "gradle-wrapper.jar"
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    assert digest == _GRADLE_WRAPPER_JAR_SHA256
