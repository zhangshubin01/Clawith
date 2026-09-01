from dataclasses import dataclass
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a workspace-relative path escapes its allowed root."""


@dataclass(frozen=True)
class ResolvedWorkspacePath:
    path: Path
    relative_root: Path
    is_enterprise: bool = False


def enterprise_info_root(workspace_root: Path, tenant_id: str | None = None) -> Path:
    suffix = f"enterprise_info_{tenant_id}" if tenant_id else "enterprise_info"
    return (workspace_root / suffix).resolve()


# 注入到所有路径类工具参数描述尾部的统一路径契约声明（L1 描述注入）。
# 所有内置工具的 path/pattern 参数一律相对 agent 工作区根目录解析，
# 与 read_file/list_files 回显的路径同形（如 workspace/my-app）。
PATH_CONVENTION_TEXT = (
    "Path is resolved relative to the agent workspace root directory — the same base "
    "shown in read_file/list_files results (e.g. 'workspace/my-app'). Absolute paths "
    "are rejected. Before using an unverified path, discover it with list_files or "
    "find_files; do not guess conventional paths (e.g. Java package directories)."
)


def describe_path_failure(
    root: Path,
    rel_path: str,
    *,
    label: str = "path",
    max_entries: int = 10,
    workspace_subdir: str = "workspace",
) -> str:
    """为「路径未命中」生成带上下文的诊断文本，供工具错误消息返回给模型（L2）。

    模型此前拿到的是零信息错误（如 "gradlew not found"），只能脑补原因并空转。
    这里一次性给出：解析后的绝对路径、基准根、最深存在祖先的顶层条目、
    以及 workspace/<rel>（或反向剥离）候选提示——让一次失败自带答案。
    """
    root_resolved = root.resolve()
    try:
        target = resolve_path_within_root(root_resolved, rel_path, label=label)
    except WorkspacePathError:
        return (
            f"{label} '{rel_path}' is not a valid workspace-relative path "
            f"(resolved against agent workspace root: {root_resolved})."
        )

    # 找到最深存在的祖先目录
    ancestor = target
    missing_below: list[str] = []
    while not ancestor.exists() and ancestor != root_resolved:
        missing_below.append(ancestor.name)
        ancestor = ancestor.parent
    if not ancestor.exists():
        ancestor = root_resolved

    lines = [
        f"Not found: {label} '{rel_path}'.",
        f"Resolved against agent workspace root → {target}.",
    ]
    if missing_below:
        lines.append(
            f"Deepest existing directory: {ancestor} "
            f"(missing below it: {'/'.join(reversed(missing_below))})."
        )
    try:
        entries = sorted(p.name for p in ancestor.iterdir())
    except OSError:
        entries = []
    if entries:
        shown = ", ".join(entries[:max_entries])
        if len(entries) > max_entries:
            shown += ", …"
        lines.append(f"Entries under it: {shown}")

    # 候选提示：少传 workspace/ 前缀 → 提示补上；多传 → 提示剥离
    normalized = (rel_path or "").strip().lstrip("/")
    candidates: list[str] = []
    if (
        workspace_subdir
        and normalized
        and normalized != workspace_subdir
        and not normalized.startswith(f"{workspace_subdir}/")
    ):
        candidate = f"{workspace_subdir}/{normalized}"
        try:
            candidate_path = (root_resolved / candidate).resolve()
            candidate_path.relative_to(root_resolved)
            if candidate_path.exists():
                candidates.append(candidate)
        except (ValueError, OSError):
            pass
    if normalized.startswith(f"{workspace_subdir}/"):
        suffix = normalized[len(workspace_subdir) + 1 :]
        if suffix and (root_resolved / suffix).exists():
            candidates.append(suffix)
    if candidates:
        lines.append("Did you mean: " + "; ".join(f"'{c}'" for c in candidates) + "?")
    return "\n".join(lines)


def resolve_path_within_root(
    root: Path,
    rel_path: str = "",
    *,
    allow_root: bool = True,
    require_subpath: bool = False,
    label: str = "path",
) -> Path:
    root_resolved = root.resolve()
    normalized = (rel_path or "").strip()

    if require_subpath and not normalized:
        raise WorkspacePathError(f"{label} must point to a file or subdirectory under the allowed root")

    candidate = Path(normalized)
    if candidate.is_absolute():
        raise WorkspacePathError(f"Absolute {label} is not allowed")

    target = (root_resolved / candidate).resolve() if normalized else root_resolved
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise WorkspacePathError(f"Access denied for this {label}") from exc

    if not allow_root and target == root_resolved:
        raise WorkspacePathError(f"{label} must not resolve to the root directory")

    return target


def resolve_agent_visible_path(
    agent_workspace: Path,
    rel_path: str,
    *,
    workspace_root: Path,
    tenant_id: str | None = None,
    allow_root: bool = True,
    require_subpath_for_enterprise: bool = False,
) -> ResolvedWorkspacePath:
    normalized = (rel_path or "").strip()

    if normalized.startswith("enterprise_info"):
        enterprise_root = enterprise_info_root(workspace_root, tenant_id)
        sub_path = normalized[len("enterprise_info"):].lstrip("/")
        target = resolve_path_within_root(
            enterprise_root,
            sub_path,
            allow_root=allow_root,
            require_subpath=require_subpath_for_enterprise,
            label="enterprise_info path",
        )
        return ResolvedWorkspacePath(
            path=target,
            relative_root=enterprise_root,
            is_enterprise=True,
        )

    target = resolve_path_within_root(
        agent_workspace,
        normalized,
        allow_root=allow_root,
        label="workspace path",
    )
    return ResolvedWorkspacePath(
        path=target,
        relative_root=agent_workspace.resolve(),
        is_enterprise=False,
    )
