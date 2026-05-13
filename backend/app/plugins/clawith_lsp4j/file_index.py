"""LSP4J 项目文件索引 — 会话级缓存，避免每次搜索都 rglob 全量扫描。

构建策略（按优先级）：
1. git ls-files — 最快（0.1-1s），返回所有已跟踪文件
2. os.scandir 递归 — 回退方案（2-10s），覆盖未跟踪文件

索引结构：
- by_basename: 文件名 → [完整路径列表]，O(1) 查找
- by_dir: 目录 → [条目名列表]，O(1) 目录浏览
- all_files: 所有文件路径列表（用于 grep 遍历）
"""

import os
import subprocess
import time
from pathlib import Path
from collections import defaultdict

from loguru import logger

# 索引缓存: project_path → FileIndex，模块级跨请求复用
_index_cache: dict[str, "FileIndex"] = {}
_INDEX_CACHE_TTL = 1800.0  # 30 分钟过期，覆盖大部分会话生命周期

# 始终排除的目录（构建索引时跳过）
_EXCLUDED_DIRS = {
    ".git", ".gradle", ".idea", ".kotlin", "build", ".build",
    "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", ".next", ".output",
    "target", ".intellijPlatform", ".claude", ".code-review-graph",
}

# 可搜索的文件扩展名
_SEARCHABLE_EXTENSIONS = frozenset({
    ".kt", ".java", ".kts", ".xml", ".gradle", ".properties",
    ".pro", ".json", ".yaml", ".yml", ".toml",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html",
    ".md", ".txt", ".sh", ".bat", ".cmake", ".mk",
    ".h", ".cpp", ".c", ".hpp", ".rs", ".go", ".swift",
})


class FileIndex:
    """项目文件索引，支持按文件名、目录、glob 模式快速查询。"""

    def __init__(self, project_path: str):
        self.project_root = Path(project_path).resolve()
        self.by_basename: dict[str, list[str]] = defaultdict(list)
        self.by_dir: dict[str, list[str]] = defaultdict(list)
        self.all_files: list[str] = []
        self.built_at: float = 0.0
        self.file_count: int = 0
        self.build_method: str = "none"

    @property
    def age(self) -> float:
        return time.monotonic() - self.built_at

    def is_stale(self, ttl: float = _INDEX_CACHE_TTL) -> bool:
        return self.age > ttl

    def search_by_basename(self, query: str) -> list[str]:
        """按文件名搜索（大小写不敏感），返回匹配的完整路径列表。"""
        q = query.lower()
        results = []
        for name, paths in self.by_basename.items():
            if q in name.lower():
                results.extend(paths)
        return results[:50]

    def search_by_pattern(self, pattern: str) -> list[str]:
        """按 glob 模式匹配文件名。"""
        import fnmatch
        results = []
        for name, paths in self.by_basename.items():
            if fnmatch.fnmatch(name, pattern):
                results.extend(paths)
        return results[:50]

    def list_dir(self, rel_path: str) -> list[dict]:
        """列出目录内容，返回 DirItem 列表。"""
        # 标准化路径作为 key
        norm = rel_path.replace("\\", "/").rstrip("/")
        entries = self.by_dir.get(norm, [])
        if not entries and norm == ".":
            entries = self.by_dir.get("", [])

        items = []
        for entry in entries:
            entry = entry.rstrip("/")
            full = os.path.join(self.project_root, norm, entry) if norm else os.path.join(self.project_root, entry)
            p = Path(full)
            is_dir = p.is_dir() if p.exists() else entry.endswith("/")
            items.append({
                "fileName": entry,
                "fileCount": 0,
                "fileSize": p.stat().st_size if p.exists() and not is_dir else 0,
                "type": "directory" if is_dir else "file",
                "path": str(p.absolute()),
            })
        return items

    def search_file(self, query: str, pattern: str = "*") -> list[dict]:
        """组合搜索：先按 query 匹配文件名，再按 pattern 过滤。"""
        import fnmatch

        candidates = self.search_by_basename(query) if query else list(self.by_basename.keys())
        results = []
        seen = set()
        for fp in candidates:
            fname = os.path.basename(fp)
            if pattern != "*" and not fnmatch.fnmatch(fname, pattern):
                continue
            if fp not in seen:
                seen.add(fp)
                results.append({"fileName": fname, "path": fp})
                if len(results) >= 50:
                    break
        return results


def _build_from_git(project_path: str) -> FileIndex | None:
    """通过 git ls-files 构建索引，最快方式。返回 None 表示不可用。"""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        idx = FileIndex(project_path)
        lines = result.stdout.strip().split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # 跳过排除目录中的文件
            parts = line.replace("\\", "/").split("/")
            if any(seg in _EXCLUDED_DIRS for seg in parts):
                continue
            # 跳过非搜索扩展名
            ext = os.path.splitext(line)[1].lower()
            if ext and ext not in _SEARCHABLE_EXTENSIONS:
                continue

            idx.all_files.append(line)
            basename = os.path.basename(line)
            idx.by_basename[basename].append(line)
            # 为每个上级目录注册条目
            dir_parts = parts[:-1]
            for i in range(len(dir_parts) + 1):
                dir_key = "/".join(dir_parts[:i]) if i > 0 else ""
                entry_name = "/".join(parts[i:i+1])
                if entry_name not in idx.by_dir[dir_key]:
                    idx.by_dir[dir_key].append(entry_name)

        idx.file_count = len(idx.all_files)
        idx.built_at = time.monotonic()
        idx.build_method = "git_ls_files"
        logger.info(
            "[FILE-INDEX] built via git ls-files: project={} files={} elapsed={:.2f}s",
            project_path, idx.file_count, idx.age,
        )
        return idx
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.info("[FILE-INDEX] git ls-files unavailable: {}", e)
        return None


def _build_from_scandir(project_path: str) -> FileIndex:
    """通过 os.scandir 递归构建索引（git 不可用时的回退方案）。"""
    idx = FileIndex(project_path)
    start = time.monotonic()
    root = Path(project_path)

    for entry in root.rglob("*"):
        if not entry.is_file():
            continue
        try:
            rel = str(entry.relative_to(root))
        except ValueError:
            continue

        parts = rel.replace("\\", "/").split("/")
        if any(seg in _EXCLUDED_DIRS for seg in parts):
            continue
        ext = entry.suffix.lower()
        if ext and ext not in _SEARCHABLE_EXTENSIONS:
            continue

        idx.all_files.append(rel)
        idx.by_basename[entry.name].append(rel)
        dir_parts = parts[:-1]
        for i in range(len(dir_parts) + 1):
            dir_key = "/".join(dir_parts[:i]) if i > 0 else ""
            entry_name = "/".join(parts[i:i+1])
            if entry_name not in idx.by_dir[dir_key]:
                idx.by_dir[dir_key].append(entry_name)

    idx.file_count = len(idx.all_files)
    idx.built_at = time.monotonic()
    idx.build_method = "scandir"
    elapsed = time.monotonic() - start
    logger.info(
        "[FILE-INDEX] built via scandir: project={} files={} elapsed={:.2f}s",
        project_path, idx.file_count, elapsed,
    )
    return idx


def get_or_build_index(project_path: str, force_rebuild: bool = False) -> FileIndex | None:
    """获取或构建项目文件索引（带缓存）。

    Args:
        project_path: 项目根路径
        force_rebuild: 强制重建索引
    """
    if not project_path or not os.path.isdir(project_path):
        return None

    cache_key = os.path.abspath(project_path)

    # 清理过期缓存
    expired = [k for k, v in _index_cache.items() if v.is_stale()]
    for k in expired:
        del _index_cache[k]

    if not force_rebuild and cache_key in _index_cache and not _index_cache[cache_key].is_stale():
        idx = _index_cache[cache_key]
        logger.debug("[FILE-INDEX] cache hit: project={} age={:.0f}s", project_path, idx.age)
        return idx

    # 构建新索引: git ls-files 优先
    idx = _build_from_git(project_path)
    if idx is None:
        idx = _build_from_scandir(project_path)

    _index_cache[cache_key] = idx
    return idx


def build_code_map(project_path: str) -> str:
    """生成代码结构地图 — 用 rg 提取所有类/函数声明，按目录组织。

    返回: Markdown 格式的代码地图，可直接作为 system message 内容。
    """
    import shutil
    # 服务器 PATH 可能不包含 /opt/homebrew/bin，用常见路径查找 rg
    _rg_path = shutil.which("rg") or shutil.which("rg", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    if not _rg_path:
        logger.warning("[CODE-MAP] rg not found")
        return ""

    _DECL_PATTERN = (
        r"(class|object|interface|enum\s+class|data\s+class|sealed\s+class|abstract\s+class|"
        r"fun\s+|suspend\s+fun\s+|@Composable\s+fun\s+)"
    )
    try:
        result = subprocess.run(
            [_rg_path, "--no-heading", "--with-filename", "--line-number",
             "--glob=*.kt", "--glob=*.java",
             "--glob=!.git", "--glob=!.gradle", "--glob=!build",
             "-e", _DECL_PATTERN, project_path],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""

    if result.returncode not in (0, 1):
        return ""

    from collections import defaultdict
    dir_tree: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, lineno_str, decl = parts
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue
        dir_name = os.path.dirname(file_path) or "."
        dir_tree[dir_name].append((os.path.basename(file_path), lineno, decl.strip()[:120]))

    for d in dir_tree:
        seen = set()
        filtered = []
        for fname, lineno, decl in dir_tree[d]:
            if fname in seen:
                continue
            seen.add(fname)
            filtered.append((fname, lineno, decl))
        dir_tree[d] = filtered[:8]

    total_dirs = len(dir_tree)
    total_files = sum(len(v) for v in dir_tree.values())
    lines = [
        "## 项目代码结构地图",
        f"共 {total_dirs} 个目录, {total_files} 个关键文件",
        "",
    ]

    def _dir_priority(d: str) -> int:
        dl = d.lower()
        if "/app/" in dl or dl.startswith("app"):
            return 0
        if any(k in dl for k in ("/ui/", "/view/", "/screen/", "/presentation/")):
            return 1
        if any(k in dl for k in ("/data/", "/model/", "/repository/", "/network/")):
            return 2
        return 3

    sorted_dirs = sorted(dir_tree.keys(), key=_dir_priority)
    for d in sorted_dirs[:40]:
        entries = dir_tree[d]
        display_dir = d.replace(project_path, "").lstrip("/") or "项目根目录"
        lines.append(f"### {display_dir}")
        for fname, lineno, decl in sorted(entries, key=lambda x: x[0]):
            lines.append(f"- `{fname}:{lineno}` — {decl}")
        lines.append("")

    if total_dirs > 40:
        lines.append(f"... 还有 {total_dirs - 40} 个目录未显示")

    lines.append("")
    lines.append("> 使用 read_file 读取具体文件，使用 grep_code 搜索代码内容。")
    return "\n".join(lines)


_code_map_cache: dict[str, tuple[float, str]] = {}
_CODE_MAP_CACHE_TTL = 300.0


def get_or_build_code_map(project_path: str) -> str:
    """获取或构建代码结构地图（带缓存）。"""
    if not project_path or not os.path.isdir(project_path):
        return ""
    cache_key = os.path.abspath(project_path)
    if cache_key in _code_map_cache:
        ts, cmap = _code_map_cache[cache_key]
        if time.monotonic() - ts < _CODE_MAP_CACHE_TTL:
            return cmap
    cmap = build_code_map(project_path)
    if cmap:
        _code_map_cache[cache_key] = (time.monotonic(), cmap)
        logger.info("[CODE-MAP] built: project={} size={}", project_path, len(cmap))
    else:
        logger.warning("[CODE-MAP] build returned empty: project={} isdir={}", project_path, os.path.isdir(project_path))
    return cmap
