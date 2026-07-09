"""context_tracker 单元测试。"""

from app.services.llm.context_tracker import (
    ContextTracker,
    build_proactive_hints,
    track_session_compression,
    _session_trackers,
)


def setup_function():
    _session_trackers.clear()


def test_track_and_analyze_keyword_overlap():
    t = ContextTracker()
    t.track_compression(
        "abc" * 20 + "def",
        1,
        "search_files",
        workspace_key="agent-1",
        sample_content="src/auth/middleware.py:42: def authenticate",
    )
    recs = t.analyze_query(
        "show authenticate middleware auth",
        workspace_key="agent-1",
    )
    assert recs
    assert recs[0].hash_key.startswith("abc")


def test_workspace_isolation():
    t = ContextTracker()
    t.track_compression("h1", 1, "read_file", workspace_key="a", sample_content="foo bar")
    assert not t.analyze_query("foo bar", workspace_key="b")


def test_build_proactive_hints_session():
    h = "hash1234567890" * 4
    track_session_compression(
        session_id="sess-1",
        hash_key=h,
        tool_name="grep",
        sample_content="config.yaml authentication enabled",
        workspace_key="agent-x",
    )
    hints = build_proactive_hints("sess-1", "agent-x", "where is authentication config")
    assert "retrieve_context" in hints
    assert h[:16] in hints


def test_filter_skill_body_for_mode():
    from app.services.agent_context import filter_skill_body_for_mode

    body = """Intro
<!-- mode:agent -->
Agent only
<!-- /mode -->
<!-- mode:plan -->
Plan only
<!-- /mode -->
"""
    out = filter_skill_body_for_mode(body, mode="agent")
    assert "Agent only" in out
    assert "Plan only" not in out
