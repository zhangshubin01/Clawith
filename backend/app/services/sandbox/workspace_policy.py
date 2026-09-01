"""Trusted workspace policy for local code execution."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from app.services.workspace_collaboration import normalize_workspace_path

WorkspaceMode = Literal["merge", "isolated_output"]
PublicationOwner = Literal["gateway", "workspace_cas"]
PublicationConflictMode = Literal["fail", "overwrite"]
PublishClass = Literal["source", "derived", "artifact"]

# Derived outputs never published (segment-level, case-sensitive blacklist).
# ``_exec_tmp`` also matches a ``_exec_tmp``-prefixed file basename, keeping the
# legacy temp-file exclusion semantics.
DERIVED_SEGMENTS = frozenset({"build", ".git", ".gradle", "node_modules", "target", "dist", "__pycache__", "_exec_tmp"})
_EXEC_TMP_PREFIX = "_exec_tmp"


def classify_publish_path(rel_path: str) -> PublishClass:
    """Classify one workspace-relative publish path (pure segment blacklist).

    Segment-level, case-sensitive matching:
    - any segment in ``DERIVED_SEGMENTS`` (or an ``_exec_tmp``-prefixed
      basename) marks the path derived;
    - a ``build`` segment immediately followed by an ``outputs`` segment marks
      the ``**/build/outputs/**`` artifact exception, which wins over derived.
    Everything else is a CAS-protected source path.
    """
    parts = [part for part in str(rel_path).replace("\\", "/").split("/") if part not in {"", "."}]
    for index, part in enumerate(parts):
        if part == "build" and index + 1 < len(parts) and parts[index + 1] == "outputs":
            return "artifact"
    for part in parts:
        if part in DERIVED_SEGMENTS:
            return "derived"
    if parts and parts[-1].startswith(_EXEC_TMP_PREFIX):
        return "derived"
    return "source"


@dataclass(frozen=True, slots=True)
class SandboxExecutionScope:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class SandboxWorkspacePolicy:
    mode: WorkspaceMode
    session_id: uuid.UUID | None
    materialized_paths: tuple[str, ...]
    publish_paths: tuple[str, ...]

    @property
    def session_output_path(self) -> str | None:
        if self.session_id is None:
            return None
        return normalize_workspace_path(f"workspace/output/{self.session_id}")

    @property
    def guest_output_path(self) -> str | None:
        relative = self.session_output_path
        if relative is None:
            return None
        workspace_relative = relative.removeprefix("workspace/")
        return f"/workspace/{workspace_relative}"

    @property
    def publication_conflict_mode(self) -> PublicationConflictMode:
        """Return the durable write policy for this workspace mode."""
        return "overwrite" if self.mode == "isolated_output" else "fail"


def parse_canonical_uuid(value: str | uuid.UUID, *, label: str) -> uuid.UUID:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc
    if str(parsed) != str(value).lower():
        raise ValueError(f"{label} must be a canonical UUID")
    return parsed


def build_workspace_policy(
    *,
    mode: WorkspaceMode,
    session_id: uuid.UUID | None,
    default_paths: list[str] | tuple[str, ...],
) -> SandboxWorkspacePolicy:
    materialized = tuple(normalize_workspace_path(path) for path in default_paths)
    if mode == "merge":
        return SandboxWorkspacePolicy(mode, session_id, materialized, materialized)
    if mode != "isolated_output":
        raise ValueError("Unsupported sandbox workspace mode")
    if session_id is None:
        raise ValueError("isolated_output requires a Session")
    output_path = normalize_workspace_path(f"workspace/output/{session_id}")
    return SandboxWorkspacePolicy(mode, session_id, materialized, (output_path,))
