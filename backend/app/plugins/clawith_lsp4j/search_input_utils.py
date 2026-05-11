"""LSP4J 本地检索纯函数（无 DB / 异步依赖），供 jsonrpc_router 与单测复用。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")


def sanitize_search_input(raw: Any) -> str:
    """清洗检索输入，去除前后空格和成对引号。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    while len(s) >= 2:
        first, last = s[0], s[-1]
        if (first, last) not in {('"', '"'), ("'", "'"), ("`", "`"), ("“", "”"), ("‘", "’")}:
            break
        s = s[1:-1].strip()
    return s


def contains_han(text: str) -> bool:
    return bool(_HAN_RE.search(text or ""))


def longest_latin_identifier(query: str) -> str:
    best = ""
    for m in _LATIN_ID_RE.finditer(query or ""):
        t = m.group(0)
        if len(t) > len(best):
            best = t
    return best


def infer_implicit_file_pattern_from_description(query: str, existing_pattern: str) -> str:
    e = (existing_pattern or "").strip()
    if e and e != "*":
        return e
    q = (query or "").lower()
    if "kotlin" in q or "kt文件" in query or ".kt" in (query or ""):
        return "**/*.kt"
    if "java文件" in query or ("java" in q and "文件" in query):
        return "**/*.java"
    return existing_pattern or "*"


def is_extension_only_language_glob(pattern: str) -> bool:
    if not pattern:
        return False
    p = pattern.replace("\\", "/").strip().lower()
    return p.endswith("*.kt") or p.endswith("*.java") or p.endswith("**/*.kt") or p.endswith("**/*.java")


def filename_keyword_for_search_file(query: str, effective_pattern: str) -> str:
    """混用中文描述时提取拉丁词干；扩展名已限定时不再附加描述词过滤。"""
    q = sanitize_search_input(query)
    if not q:
        return ""
    if is_extension_only_language_glob(effective_pattern):
        return ""
    if contains_han(q):
        return longest_latin_identifier(q)
    return q.replace("*", "").replace("?", "").strip()


def is_unusable_natural_language_file_query(query: str, effective_pattern: str, is_resource_query: bool) -> bool:
    """纯中文且无资源语义、未推断出扩展名时，无法用文件名子串搜索。"""
    if is_resource_query:
        return False
    q = sanitize_search_input(query)
    if not q:
        return False
    if effective_pattern not in ("*", ""):
        ep = effective_pattern.replace("\\", "/").lower()
        if ep.endswith("*.kt") or ep.endswith("**/*.kt") or ep.endswith("*.java") or ep.endswith("**/*.java"):
            return False
    return bool(contains_han(q) and not longest_latin_identifier(q))


def is_android_resource_query(query: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:^|\b)(?:R\.)?(layout|string|id|drawable|color|menu|anim|mipmap)\s*[./:]?\s*[A-Za-z0-9_]+",
            query or "",
        )
    )


def extract_android_resource_name(query: str) -> str:
    m = re.search(
        r"(?i)(?:^|\b)(?:R\.)?(layout|string|id|drawable|color|menu|anim|mipmap)\s*[./:]?\s*([A-Za-z0-9_]+)",
        query or "",
    )
    return m.group(2) if m else ""


def is_android_resource_path(path_text: str) -> bool:
    normalized = (path_text or "").replace("\\", "/").lower()
    return (
        "/src/main/res/" in normalized
        or "/res/layout/" in normalized
        or "/res/values/" in normalized
        or "/res/menu/" in normalized
        or "/res/drawable/" in normalized
    )


def android_module_tier(path_text: str) -> int:
    """多模块排序：app 主模块 > feature 模块 > 其他 src/main > 其余。"""
    n = (path_text or "").replace("\\", "/").lower()
    if "/app/src/main/" in n:
        return 0
    if "/feature/" in n and "/src/main/" in n:
        return 1
    if "/src/main/" in n:
        return 2
    return 3


def collect_android_values_xml_hits(root: Path, resource_name: str, max_files_to_read: int = 200) -> list[dict[str, str]]:
    """扫描 res/**/values/**/*.xml 中 name=\"resource\" 声明（string/color/dimen 等）。"""
    if not resource_name or not root.exists():
        return []
    pattern = re.compile(
        r'name\s*=\s*["\']' + re.escape(resource_name) + r'["\']',
        re.IGNORECASE,
    )
    hits: list[dict[str, str]] = []
    read_count = 0
    try:
        for p in root.rglob("*.xml"):
            if read_count >= max_files_to_read:
                break
            parts_lower = [x.lower() for x in p.parts]
            if "res" not in parts_lower or "values" not in parts_lower:
                continue
            if not p.is_file():
                continue
            read_count += 1
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pattern.search(text):
                hits.append({"fileName": p.name, "path": str(p.resolve())})
    except OSError:
        pass
    return hits
