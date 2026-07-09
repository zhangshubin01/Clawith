"""格式保真的可逆轻量压缩（移植 headroom lossless_compaction 的安全子集）。

这些变换不依赖 CCR：grep 仍像 grep，日志仍像日志，diff 仍可应用。
每个可逆变换都会自校验；不变小或校验失败就回退原文。
"""

from __future__ import annotations

import re

__all__ = [
    "strip_ansi",
    "collapse_runs",
    "expand_runs",
    "search_heading",
    "search_unheading",
    "diff_strip_index",
    "compact_lossless",
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_RUN_MARKER_RE = re.compile(r"^\.\.\. \(repeated (\d+) times\)$")
_GREP_ROW_RE = re.compile(r"^(?P<path>[^\n:]+):(?P<line>\d+):(?P<content>.*)$")
_HEADING_ROW_RE = re.compile(r"^(?P<line>\d+):(?P<content>.*)$")
_DIFF_INDEX_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+( [0-7]+)?$")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色；颜色不承载上下文事实。"""
    return _ANSI_RE.sub("", text)


def _split_keep_trailing(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], False
    had_trailing = text.endswith("\n")
    body = text[:-1] if had_trailing else text
    return body.split("\n"), had_trailing


def _join(lines: list[str], had_trailing: bool) -> str:
    out = "\n".join(lines)
    return out + "\n" if had_trailing else out


def collapse_runs(text: str) -> str:
    """折叠连续重复行；`expand_runs` 可还原。"""
    lines, had_trailing = _split_keep_trailing(text)
    if not lines:
        return text
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i
        while j + 1 < n and lines[j + 1] == lines[i]:
            j += 1
        run_len = j - i + 1
        if run_len >= 2:
            out.append(lines[i])
            out.append(f"... (repeated {run_len} times)")
        else:
            out.append(lines[i])
        i = j + 1
    return _join(out, had_trailing)


def expand_runs(text: str) -> str:
    """还原 `collapse_runs` 生成的重复行 marker。"""
    lines, had_trailing = _split_keep_trailing(text)
    if not lines:
        return text
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if i + 1 < len(lines):
            match = _RUN_MARKER_RE.match(lines[i + 1])
            if match:
                out.extend([line] * int(match.group(1)))
                i += 2
                continue
        out.append(line)
        i += 1
    return _join(out, had_trailing)


def search_heading(text: str) -> str:
    """grep `path:line:content` 转 ripgrep --heading 形态。"""
    lines, had_trailing = _split_keep_trailing(text)
    if not lines:
        return text
    out: list[str] = []
    current_path: str | None = None
    for line in lines:
        match = _GREP_ROW_RE.match(line)
        if match:
            path = match.group("path")
            if path != current_path:
                out.append(path)
                current_path = path
            out.append(f"{match.group('line')}:{match.group('content')}")
        else:
            out.append(line)
            current_path = None
    return _join(out, had_trailing)


def search_unheading(text: str) -> str:
    """还原 `search_heading`。"""
    lines, had_trailing = _split_keep_trailing(text)
    if not lines:
        return text
    out: list[str] = []
    current_path: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        data = _HEADING_ROW_RE.match(line)
        if current_path is not None and data:
            out.append(f"{current_path}:{data.group('line')}:{data.group('content')}")
            i += 1
            continue
        if not data and i + 1 < len(lines) and _HEADING_ROW_RE.match(lines[i + 1]):
            current_path = line
            i += 1
            continue
        current_path = None
        out.append(line)
        i += 1
    return _join(out, had_trailing)


def diff_strip_index(text: str) -> str:
    """删除 unified diff 的 index 行；hunk 内容保持可读、可应用。"""
    lines, had_trailing = _split_keep_trailing(text)
    if not lines:
        return text
    return _join([line for line in lines if not _DIFF_INDEX_RE.match(line)], had_trailing)


def _smaller(candidate: str, original: str) -> bool:
    return len(candidate) < len(original)


def compact_lossless(content: str, kind: str) -> str:
    """按格式执行可逆/保语义压缩；失败或不变小则返回原文。"""
    if not content:
        return content
    try:
        if kind == "log":
            baseline = strip_ansi(content)
            candidate = collapse_runs(baseline)
            if expand_runs(candidate) != baseline:
                return content
            return candidate if _smaller(candidate, content) else content
        if kind == "search":
            candidate = search_heading(content)
            if search_unheading(candidate) != content:
                return content
            return candidate if _smaller(candidate, content) else content
        if kind == "diff":
            candidate = diff_strip_index(content)
            return candidate if _smaller(candidate, content) else content
        if kind == "text":
            candidate = collapse_runs(content)
            if expand_runs(candidate) != content:
                return content
            return candidate if _smaller(candidate, content) else content
    except Exception:
        return content
    return content
