"""Unit tests for LSP4J pre-finish chat/answer sync planner."""

import pytest

from app.plugins.clawith_lsp4j.answer_sync import plan_answer_sync_before_finish


@pytest.mark.parametrize(
    ("cancelled", "reply", "streamed", "stream_on", "branch", "text", "overwrite"),
    [
        (True, "hi", "", True, "skip_cancel", None, False),
        (False, "", "", True, "skip_empty_reply", None, False),
        (False, "final", "", True, "finish_only", "final", False),
        (False, "final", "x", False, "non_stream", "final", False),
        (False, "abcd", "ab", True, "tail", "cd", False),
        (False, "new", "old", True, "overwrite_mismatch", "new", True),
        (False, "same", "same", True, "skip_equal", None, False),
    ],
)
def test_plan_answer_sync_before_finish(cancelled, reply, streamed, stream_on, branch, text, overwrite):
    p = plan_answer_sync_before_finish(
        cancelled=cancelled,
        reply=reply,
        streamed_plain=streamed,
        stream_mode_on=stream_on,
    )
    assert p.branch == branch
    assert p.text == text
    assert p.overwrite is overwrite
