"""L2 结构化路径诊断 helper 的单元测试。"""

from pathlib import Path

from app.services.workspace_paths import describe_path_failure


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    return p


def test_missing_path_with_workspace_candidate(tmp_path: Path) -> None:
    root = tmp_path / "agent-root"
    _touch(root / "workspace" / "my-app" / "gradlew")
    _touch(root / "memory" / "memory.md")

    text = describe_path_failure(root, "my-app")

    assert "Not found: path 'my-app'." in text
    assert "Resolved against agent workspace root" in text
    assert "Did you mean: 'workspace/my-app'?" in text


def test_missing_nested_path_lists_deepest_ancestor_entries(tmp_path: Path) -> None:
    root = tmp_path / "agent-root"
    _touch(root / "workspace" / "existing" / "file.md")
    _touch(root / "workspace" / "another-entry.md")

    text = describe_path_failure(root, "workspace/nope/deeper")

    assert "Deepest existing directory:" in text
    assert "missing below it: nope/deeper" in text
    assert "Entries under it:" in text
    assert "existing" in text
    assert "another-entry.md" in text


def test_over_prefixed_path_suggests_stripping(tmp_path: Path) -> None:
    root = tmp_path / "agent-root"
    _touch(root / "my-app" / "gradlew")

    text = describe_path_failure(root, "workspace/my-app")

    assert "Did you mean: 'my-app'?" in text


def test_no_candidate_when_nothing_matches(tmp_path: Path) -> None:
    root = tmp_path / "agent-root"
    root.mkdir()

    text = describe_path_failure(root, "ghost")

    assert "Did you mean" not in text
    assert "Not found: path 'ghost'." in text


def test_absolute_path_reports_invalid(tmp_path: Path) -> None:
    root = tmp_path / "agent-root"
    root.mkdir()

    text = describe_path_failure(root, "/etc/passwd")

    assert "is not a valid workspace-relative path" in text
