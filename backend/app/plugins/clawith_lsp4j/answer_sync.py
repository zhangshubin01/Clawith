"""LSP4J: decide how to sync final reply to the IDE via chat/answer before chat/finish.

The Tongyi MarkdownStreamPanel primarily renders chat/answer chunks. Text delivered only
through the finish tool never passes on_chunk, so reply_parts can be empty while
chat/finish fullAnswer is non-empty — the bubble stays blank unless we emit a matching
chat/answer here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerSyncPlan:
    """What to send before ``chat/finish``."""

    branch: str
    text: str | None
    overwrite: bool


def plan_answer_sync_before_finish(
    *,
    cancelled: bool,
    reply: str,
    streamed_plain: str,
    stream_mode_on: bool,
) -> AnswerSyncPlan:
    """Return which chat/answer (if any) to send so the IDE panel matches reply.

    branch values:
        finish_only — streamed_plain empty, send full reply (finish-tool-only path).
        non_stream — ask.stream=false; chunks were not pushed to the buffer/UI.
        tail — streamed prefix; send remainder only.
        overwrite_mismatch — streamed text diverged from final reply; replace with full reply.
        skip_equal — streamed_plain already equals reply; streaming should have populated UI.
        skip_cancel / skip_empty_reply — nothing to send.
    """
    if cancelled:
        return AnswerSyncPlan("skip_cancel", None, False)
    if not reply:
        return AnswerSyncPlan("skip_empty_reply", None, False)
    if not streamed_plain:
        return AnswerSyncPlan("finish_only", reply, False)
    if not stream_mode_on:
        return AnswerSyncPlan("non_stream", reply, False)
    if reply.startswith(streamed_plain) and len(reply) > len(streamed_plain):
        return AnswerSyncPlan("tail", reply[len(streamed_plain) :], False)
    if streamed_plain != reply:
        return AnswerSyncPlan("overwrite_mismatch", reply, True)
    return AnswerSyncPlan("skip_equal", None, False)
