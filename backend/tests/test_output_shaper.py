import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.output_shaper import build_output_shaping_suffix


def test_output_shaper_default_off():
    assert build_output_shaping_suffix(
        path="acp",
        user_query="hello",
        recent_tool_count=3,
        model_name="gpt-4o",
    ) == ""


def test_output_shaper_skips_detail_request(monkeypatch):
    class _S:
        CTX_OUTPUT_SHAPER_ENABLED = True
        CTX_OUTPUT_SHAPER_PATHS = "acp,ws"
        CTX_OUTPUT_SHAPER_MAX_SUFFIX_CHARS = 500

    import app.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    assert build_output_shaping_suffix(
        path="acp",
        user_query="请详细逐步说明",
        recent_tool_count=1,
        model_name="gpt-4o",
    ) == ""


def test_output_shaper_applies_when_enabled(monkeypatch):
    class _S:
        CTX_OUTPUT_SHAPER_ENABLED = True
        CTX_OUTPUT_SHAPER_PATHS = "ws"
        CTX_OUTPUT_SHAPER_MAX_SUFFIX_CHARS = 500

    import app.config as cfg
    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    suffix = build_output_shaping_suffix(
        path="ws",
        user_query="summary please",
        recent_tool_count=2,
        model_name="gpt-4o",
    )
    assert "回复约束" in suffix
