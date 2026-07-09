"""lossless_compaction 单元测试。"""

from app.services.llm.lossless_compaction import (
    collapse_runs,
    compact_lossless,
    expand_runs,
    search_heading,
    search_unheading,
)
from app.services.llm.content_router import compress_one


def test_collapse_runs_roundtrip():
    original = "a\na\na\nb\n"
    compacted = collapse_runs(original)
    assert compacted == "a\n... (repeated 3 times)\nb\n"
    assert expand_runs(compacted) == original


def test_search_heading_roundtrip():
    original = "src/a.py:1:foo\nsrc/a.py:2:bar\nsrc/b.py:1:baz\n"
    compacted = search_heading(original)
    assert len(compacted) < len(original)
    assert search_unheading(compacted) == original


def test_diff_strip_index():
    original = "diff --git a/a b/a\nindex 123abc..456def 100644\n@@ -1 +1 @@\n-a\n+b\n"
    compacted = compact_lossless(original, "diff")
    assert "index 123abc" not in compacted
    assert "@@ -1 +1 @@" in compacted


def test_content_router_uses_lossless_log_before_lossy_marker():
    original = "WARN same\n" * 20 + "done\n"
    compacted = compress_one(original, tool_name="execute_code", budget_tokens=1)
    assert "... (repeated 20 times)" in compacted
    assert "<!-- ctx:trimmed -->" not in compacted


def test_excluded_search_can_use_verified_lossless_fold():
    original = "src/a.py:1:foo\nsrc/a.py:2:bar\nsrc/a.py:3:baz\nsrc/b.py:1:qux\n"
    compacted = compress_one(original, tool_name="search_file", budget_tokens=1)
    assert compacted != original
    assert "<!-- ctx:trimmed -->" not in compacted
    assert search_unheading(compacted) == original


def test_relevance_split_keeps_matching_log_context():
    lines = [f"2026-07-04T10:{i:04d}Z INFO worker-{i} completed batch {i}" for i in range(520)]
    lines[401] = "2026-07-04T10:0401Z ERROR needle_401 failure"
    original = "\n".join(lines)
    compacted = compress_one(
        original,
        tool_name="execute_code",
        budget_tokens=1,
        user_query="why did needle_401 fail",
        tool_args_text="query=needle_401",
    )
    assert "<!-- ctx:trimmed -->" in compacted
    assert "needle_401 failure" in compacted
    assert len(compacted) < len(original)
