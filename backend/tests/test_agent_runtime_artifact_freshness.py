"""L3 artifact-freshness ledger fallback — claim extraction + verifier wiring."""

from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest

from app.services.agent_runtime.state import RuntimeContext
from app.services.agent_runtime.verification import (
    ToolLedgerRuntimeVerifier,
    _artifact_claims_not_in_ledger,
    _extract_artifact_claims,
    _normalize_artifact_path,
)


class _ManyResult:
    def __init__(self, values) -> None:
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class _DB:
    def __init__(self, results: deque) -> None:
        self.results = results

    async def execute(self, _statement):
        return self.results.popleft()


def _factory(*results):
    remaining = deque(results)

    @asynccontextmanager
    async def factory():
        yield _DB(remaining)

    return factory


def _execution(
    *,
    tool_call_id: str = "call-1",
    artifacts: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
):
    return SimpleNamespace(
        status="succeeded",
        tool_call_id=tool_call_id,
        tool_name="android_compile",
        result_ref=None,
        result_metadata={
            "artifact_refs": list(artifacts),
            "evidence_refs": list(evidence),
        },
    )


async def _always_readable(_reference, _tenant_id, _run_id):
    return True


def _context() -> RuntimeContext:
    return RuntimeContext(
        tenant_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        executor=object(),  # type: ignore[arg-type]
    )


# ─── claim extraction ───


def test_extract_backtick_wrapped_apk_path() -> None:
    candidate = "✅ 项目重新编译完成。**产物**：`app/build/outputs/apk/debug/app-debug.apk`"
    assert _extract_artifact_claims(candidate) == ["app/build/outputs/apk/debug/app-debug.apk"]


def test_extract_bare_apk_path_without_backticks() -> None:
    candidate = "产物位于 app/build/outputs/apk/debug/app-debug.apk。"
    assert _extract_artifact_claims(candidate) == ["app/build/outputs/apk/debug/app-debug.apk"]


def test_extract_workspace_scheme_apk_path() -> None:
    candidate = f"产物：workspace://{uuid.uuid4()}/app/build/app-debug.aab"
    assert len(_extract_artifact_claims(candidate)) == 1


def test_extract_ignores_non_artifact_extensions() -> None:
    candidate = "我读取了 app/build.gradle 和 src/main.kt"
    assert _extract_artifact_claims(candidate) == []


def test_extract_ignores_bare_filename_without_path_separator() -> None:
    candidate = "文件名是 app-debug.apk"
    assert _extract_artifact_claims(candidate) == []


def test_extract_ignores_http_urls() -> None:
    candidate = "安装包地址 https://cdn.example.com/release/app.apk"
    assert _extract_artifact_claims(candidate) == []


def test_extract_dedupes_repeated_paths() -> None:
    path = "app/build/outputs/apk/debug/app-debug.apk"
    candidate = f"产物 `{path}`（同 `{path}`）"
    assert _extract_artifact_claims(candidate) == [path]


# ─── path normalization / ledger matching ───


def test_normalize_strips_workspace_scheme_and_agent_id() -> None:
    agent_id = uuid.uuid4()
    assert _normalize_artifact_path(f"workspace://{agent_id}/app/build/app-debug.apk") == "app/build/app-debug.apk"


def test_normalize_strips_workspace_prefix() -> None:
    assert _normalize_artifact_path("workspace/app/build/app-debug.apk") == "app/build/app-debug.apk"


def test_normalize_rejects_http_refs() -> None:
    assert _normalize_artifact_path("https://example.com/app.apk") is None


def test_claims_suffix_match_ledger_workspace_ref() -> None:
    agent_id = uuid.uuid4()
    ledger = [f"workspace://{agent_id}/app/app/build/outputs/apk/debug/app-debug.apk"]
    claims = ["app/build/outputs/apk/debug/app-debug.apk"]
    assert _artifact_claims_not_in_ledger(claims, ledger) == []


def test_claims_flagged_when_ledger_empty() -> None:
    claims = ["app/build/outputs/apk/debug/app-debug.apk"]
    assert _artifact_claims_not_in_ledger(claims, []) == claims


def test_claims_flagged_when_filename_differs() -> None:
    agent_id = uuid.uuid4()
    ledger = [f"workspace://{agent_id}/app/app/build/outputs/apk/debug/app-debug.apk"]
    claims = ["app/build/outputs/apk/debug/app-debug-20260819-1457.apk"]
    assert _artifact_claims_not_in_ledger(claims, ledger) == claims


# ─── verifier integration ───


@pytest.mark.asyncio
async def test_verify_repairs_apk_claim_absent_from_ledger() -> None:
    verifier = ToolLedgerRuntimeVerifier(session_factory=_factory(_ManyResult([])))
    candidate = "✅ 编译完成。产物：`app/build/outputs/apk/debug/app-debug.apk`"
    result = await verifier.verify(  # type: ignore[arg-type]
        {"lifecycle": {"pending_tool_calls": []}},
        _context(),
        candidate,
    )
    assert result.outcome == "repair"
    assert result.details["code"] == "artifact_path_not_in_ledger"
    assert result.details["paths"] == ["app/build/outputs/apk/debug/app-debug.apk"]


@pytest.mark.asyncio
async def test_verify_passes_apk_claim_backed_by_ledger() -> None:
    agent_id = uuid.uuid4()
    reference = f"workspace://{agent_id}/app/app/build/outputs/apk/debug/app-debug.apk"
    execution = _execution(artifacts=(reference,))
    verifier = ToolLedgerRuntimeVerifier(
        session_factory=_factory(_ManyResult([execution])),
        reference_exists=_always_readable,
    )
    candidate = "✅ 编译完成。产物：`app/build/outputs/apk/debug/app-debug.apk`"
    result = await verifier.verify(  # type: ignore[arg-type]
        {"lifecycle": {"pending_tool_calls": []}},
        _context(),
        candidate,
    )
    assert result.outcome == "pass"
    assert result.details["artifact_refs"] == [reference]


@pytest.mark.asyncio
async def test_verify_passes_when_no_artifact_claim_present() -> None:
    verifier = ToolLedgerRuntimeVerifier(session_factory=_factory(_ManyResult([])))
    result = await verifier.verify(  # type: ignore[arg-type]
        {"lifecycle": {"pending_tool_calls": []}},
        _context(),
        "已完成，无产物。",
    )
    assert result.outcome == "pass"


@pytest.mark.asyncio
async def test_verify_ignores_input_file_mentions() -> None:
    verifier = ToolLedgerRuntimeVerifier(session_factory=_factory(_ManyResult([])))
    result = await verifier.verify(  # type: ignore[arg-type]
        {"lifecycle": {"pending_tool_calls": []}},
        _context(),
        "我查看了 app/build.gradle 与 src/main.kt，未做任何修改。",
    )
    assert result.outcome == "pass"
