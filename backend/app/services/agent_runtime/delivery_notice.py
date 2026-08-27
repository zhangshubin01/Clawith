"""Failure-notice recognition and downgrade for model-visible history.

The terminal failure notice rendered by
``agent_runtime.delivery._safe_failure_content`` is a four-line banner meant
for human eyes. Feeding it verbatim into a later run's context confused the
model into treating the old failure as pending work (2026-08-27 Feishu group
incident: the model answered a previous turn's question and drifted on a
stale ``reconciliation_required`` banner). Session-context loaders downgrade
such banners to a single-line historical note instead.

The recognition format lives next to nothing else on purpose: it is the
contract counterpart of ``_safe_failure_content``'s fixed four-line shape,
kept import-light so ``session_context_service`` can depend on it without
pulling in the delivery graph.
"""

_HEADLINE_EXECUTION = "任务执行未完成。"
_HEADLINE_PLANNING = "任务规划未完成。"
# The remaining two line prefixes are shared with the generator so the
# recogniser can never drift from the rendered banner format.
_LABEL_ERROR_CODE = "错误码："
_LABEL_RUN_ID = "Run ID："


def _is_terminal_failure_notice(content: str) -> bool:
    """True when ``content`` is the generated terminal-failure banner.

    Requires all three generated elements: the headline, a ``错误码：`` line
    and a ``Run ID：`` line — normal replies never carry this combination.
    """
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines or lines[0] not in (_HEADLINE_EXECUTION, _HEADLINE_PLANNING):
        return False
    return any(line.startswith(_LABEL_ERROR_CODE) for line in lines) and any(
        line.startswith(_LABEL_RUN_ID) for line in lines
    )


def _downgrade_failure_notice(content: str) -> str:
    """Compress the failure banner into one historical note.

    Keeps the semantics (headline + error code + run id prefix) while marking
    it explicitly as settled history — the model must not read it as pending
    work. The result is no longer recognised by ``_is_terminal_failure_notice``
    (idempotent downgrade).
    """
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    headline = lines[0].rstrip("。") if lines else _HEADLINE_EXECUTION.rstrip("。")
    code = ""
    run_id = ""
    for line in lines[1:]:
        if not code and line.startswith(_LABEL_ERROR_CODE):
            code = line[len(_LABEL_ERROR_CODE) :]
        elif not run_id and line.startswith(_LABEL_RUN_ID):
            run_id = line[len(_LABEL_RUN_ID) :]
    parts = [f"（历史记录，已终结，无需处理）{headline}"]
    if code:
        parts.append(f"{_LABEL_ERROR_CODE}{code}")
    if run_id:
        parts.append(f"{_LABEL_RUN_ID}{run_id[:8]}")
    return "，".join(parts) + "。"
