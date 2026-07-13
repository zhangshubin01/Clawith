"""Receipt-backed sequential tool execution for durable Runtime nodes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Protocol, cast
import uuid

from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.agent_tool_execution import AgentToolExecution
from app.services.agent_runtime.a2a_runtime import (
    RuntimeA2AService,
    a2a_waiting_request,
)
from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.node_executor import (
    RuntimeCancelSource,
    ToolStepResult,
)
from app.services.agent_runtime.state import (
    JsonObject,
    RuntimeContext,
    RuntimeGraphState,
)
from app.services.agent_runtime.tool_execution import (
    ToolExecutionError,
    ToolExecutionOutcome,
    ToolExecutionReservation,
    mark_tool_execution_failed,
    mark_tool_execution_succeeded,
    mark_tool_execution_unknown,
    reserve_tool_execution,
)
from app.services.agent_tools import execute_tool, get_agent_tools_for_llm


_READ_TOOL_NAMES = frozenset(
    {
        "list_files",
        "read_file",
        "list_focus_items",
        "search_files",
        "find_files",
        "list_triggers",
        "jina_search",
        "jina_read",
        "read_webpage",
        "read_document",
        "discover_resources",
        "bitable_list_tables",
        "bitable_list_fields",
        "bitable_query_records",
        "feishu_doc_search",
        "feishu_wiki_list",
        "feishu_doc_read",
        "feishu_calendar_list",
        "feishu_user_search",
        "feishu_approval_query",
        "feishu_approval_get",
        "read_emails",
        "list_published_pages",
        "search_clawhub",
        "agentbay_browser_screenshot",
        "agentbay_code_read_file",
    }
)
_CONTROL_TOOL_NAMES = frozenset({"finish", "wait"})
_HEARTBEAT_PRIVATE_PLAZA_TOOLS = frozenset(
    {"plaza_get_new_posts", "plaza_create_post", "plaza_add_comment"}
)
_HEARTBEAT_PLAZA_LIMITS = {
    "plaza_create_post": 1,
    "plaza_add_comment": 2,
}


class ToolExecutor(Protocol):
    async def __call__(
        self,
        tool_name: str,
        arguments: dict,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str = "",
        on_output: object | None = None,
    ) -> str: ...


ToolProvider = Callable[[uuid.UUID], Awaitable[list[dict]]]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    side_effect_classification: str
    retry_policy: str


def _policy(tool_name: str) -> ToolPolicy:
    if tool_name in _READ_TOOL_NAMES:
        return ToolPolicy("read", "safe")
    return ToolPolicy("external_write", "never")


def _tool_name(tool: Mapping[str, object]) -> str | None:
    function = tool.get("function")
    if not isinstance(function, Mapping):
        return None
    name = function.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _allowed_tool_names(tools: Sequence[Mapping[str, object]]) -> frozenset[str]:
    return frozenset(name for name in (_tool_name(tool) for tool in tools) if name)


def _call_fields(call: JsonObject) -> tuple[str, str, dict]:
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ToolExecutionError(
            "invalid_tool_call",
            "Runtime tool call requires a non-empty ID",
        )
    if not isinstance(function, Mapping):
        raise ToolExecutionError(
            "invalid_tool_call",
            "Runtime tool call requires a function object",
        )
    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolExecutionError(
            "invalid_tool_call",
            "Runtime tool call requires a function name",
        )
    raw_arguments = function.get("arguments", "{}")
    try:
        arguments = (
            json.loads(raw_arguments)
            if isinstance(raw_arguments, str)
            else dict(raw_arguments)
            if isinstance(raw_arguments, Mapping)
            else None
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToolExecutionError(
            "invalid_tool_call",
            "Runtime tool arguments must be one JSON object",
        ) from exc
    if not isinstance(arguments, dict):
        raise ToolExecutionError(
            "invalid_tool_call",
            "Runtime tool arguments must be one JSON object",
        )
    return call_id.strip(), name.strip(), arguments


def _assistant_message_id(
    state: RuntimeGraphState,
    calls: Sequence[JsonObject],
) -> str:
    ordered_call_ids = [cast(str, call.get("id")) for call in calls if isinstance(call.get("id"), str)]
    call_ids = set(ordered_call_ids)
    if len(ordered_call_ids) != len(calls) or len(call_ids) != len(calls):
        raise ToolExecutionError(
            "invalid_tool_call",
            "pending tool calls require unique non-empty IDs",
        )
    matches = []
    for message in reversed(state["lifecycle"].get("run_messages", [])):
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        message_call_ids = {
            cast(str, raw.get("id")) for raw in raw_calls if isinstance(raw, Mapping) and isinstance(raw.get("id"), str)
        }
        if call_ids.issubset(message_call_ids):
            matches.append(message)
            break
    if not matches:
        raise ToolExecutionError(
            "tool_exchange_missing_assistant",
            "pending tool calls have no matching assistant message",
        )
    message_id = matches[0].get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ToolExecutionError(
            "tool_exchange_missing_assistant",
            "tool proposal assistant message has no stable ID",
        )
    return message_id


def _result_message_id(run_id: uuid.UUID, call_id: str) -> str:
    return str(uuid.uuid5(run_id, f"tool-result:{call_id}"))


def _result_message(
    *,
    run_id: uuid.UUID,
    call_id: str,
    tool_name: str,
    outcome: ToolExecutionOutcome,
) -> JsonObject:
    content = outcome.result_summary or (
        "Tool completed without inline output."
        if outcome.status == "succeeded"
        else "Tool execution failed without a reusable result."
    )
    return {
        "id": _result_message_id(run_id, call_id),
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": content,
        "execution_status": outcome.status,
        "result_ref": outcome.result_ref,
    }


def _waiting_request(
    *,
    run_id: uuid.UUID,
    call_id: str,
    requires_confirmation: bool,
    error_code: str | None,
) -> JsonObject:
    return {
        "waiting_type": "user" if requires_confirmation else "external",
        "correlation_id": str(uuid.uuid5(run_id, f"tool-reconcile:{call_id}")),
        "reason": error_code or "tool_reconciliation_required",
        "tool_call_id": call_id,
    }


def _heartbeat_tool_limit(
    state: RuntimeGraphState,
    agent: Agent,
    tool_name: str,
) -> int | None:
    if state["registry"].source_type != "heartbeat":
        return None
    is_private = (getattr(agent, "access_mode", None) or "company") != "company"
    if is_private and tool_name in _HEARTBEAT_PRIVATE_PLAZA_TOOLS:
        return 0
    return _HEARTBEAT_PLAZA_LIMITS.get(tool_name)


def _heartbeat_blocked_summary(
    agent: Agent,
    tool_name: str,
    limit: int,
) -> str:
    is_private = (getattr(agent, "access_mode", None) or "company") != "company"
    if is_private and tool_name in _HEARTBEAT_PRIVATE_PLAZA_TOOLS:
        return "[BLOCKED] Private heartbeat Agents cannot use Agent Plaza."
    return (
        f"[BLOCKED] Heartbeat limit reached for {tool_name} "
        f"(maximum {limit})."
    )


class RuntimeToolStepService:
    """Reserve, execute, and settle one model-proposed tool batch in order."""

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        cancel_source: RuntimeCancelSource,
        tool_provider: ToolProvider = get_agent_tools_for_llm,
        tool_executor: ToolExecutor = execute_tool,
        a2a_service: RuntimeA2AService | None = None,
        lease_ttl_seconds: int = 300,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        self._session_factory = session_factory
        self._cancel_source = cancel_source
        self._tool_provider = tool_provider
        self._tool_executor = tool_executor
        self._a2a_service = a2a_service
        self._lease_ttl_seconds = lease_ttl_seconds

    async def _agent(
        self,
        state: RuntimeGraphState,
    ) -> Agent:
        try:
            tenant_id = uuid.UUID(state["registry"].tenant_id)
            agent_id = uuid.UUID(state["registry"].agent_id or "")
        except ValueError as exc:
            raise ToolExecutionError(
                "invalid_runtime_identity",
                "Runtime tool state contains an invalid UUID",
            ) from exc
        async with self._session_factory() as db:
            result = await db.execute(
                select(Agent).where(
                    Agent.id == agent_id,
                    Agent.tenant_id == tenant_id,
                )
            )
            agent = result.scalar_one_or_none()
        if agent is None or agent.is_expired:
            raise ToolExecutionError(
                "agent_unavailable",
                "Runtime tool Agent is unavailable in this tenant",
            )
        return agent

    async def _reserve(
        self,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        call_id: str,
        tool_name: str,
        assistant_message_id: str,
        arguments: dict,
        policy: ToolPolicy,
        lease_owner: str,
    ) -> ToolExecutionReservation:
        async with self._session_factory() as db:
            async with db.begin():
                return await reserve_tool_execution(
                    db,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    assistant_message_id=assistant_message_id,
                    arguments=arguments,
                    sanitized_arguments=arguments,
                    request_ref=None,
                    side_effect_classification=cast(str, policy.side_effect_classification),  # type: ignore[arg-type]
                    retry_policy=cast(str, policy.retry_policy),  # type: ignore[arg-type]
                    lease_owner=lease_owner,
                    lease_ttl_seconds=self._lease_ttl_seconds,
                )

    async def _mark_succeeded(
        self,
        *,
        tenant_id: uuid.UUID,
        reservation: ToolExecutionReservation,
        lease_owner: str,
        result: str,
    ) -> ToolExecutionOutcome:
        async with self._session_factory() as db:
            async with db.begin():
                execution = await mark_tool_execution_succeeded(
                    db,
                    tenant_id=tenant_id,
                    execution_id=reservation.execution.id,
                    lease_owner=lease_owner,
                    result_summary=result,
                    result_ref=None,
                )
        return ToolExecutionOutcome(
            status="succeeded",
            result_summary=execution.result_summary,
            result_ref=execution.result_ref,
        )

    async def _mark_exception(
        self,
        *,
        tenant_id: uuid.UUID,
        reservation: ToolExecutionReservation,
        lease_owner: str,
        policy: ToolPolicy,
        exc: Exception,
    ) -> ToolExecutionOutcome:
        summary = f"{type(exc).__name__}: tool execution failed"
        async with self._session_factory() as db:
            async with db.begin():
                if policy.side_effect_classification == "read":
                    execution = await mark_tool_execution_failed(
                        db,
                        tenant_id=tenant_id,
                        execution_id=reservation.execution.id,
                        lease_owner=lease_owner,
                        result_summary=summary,
                    )
                else:
                    execution = await mark_tool_execution_unknown(
                        db,
                        tenant_id=tenant_id,
                        execution_id=reservation.execution.id,
                        lease_owner=lease_owner,
                        result_summary=summary,
                    )
        return ToolExecutionOutcome(
            status=cast(str, execution.status),  # type: ignore[arg-type]
            result_summary=execution.result_summary,
            result_ref=execution.result_ref,
        )

    async def _successful_tool_count(
        self,
        *,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_name: str,
    ) -> int:
        async with self._session_factory() as db:
            result = await db.execute(
                select(func.count(AgentToolExecution.id)).where(
                    AgentToolExecution.tenant_id == tenant_id,
                    AgentToolExecution.run_id == run_id,
                    AgentToolExecution.tool_name == tool_name,
                    AgentToolExecution.status == "succeeded",
                )
            )
            return int(result.scalar_one())

    async def _mark_policy_blocked(
        self,
        *,
        tenant_id: uuid.UUID,
        reservation: ToolExecutionReservation,
        lease_owner: str,
        result_summary: str,
    ) -> ToolExecutionOutcome:
        async with self._session_factory() as db:
            async with db.begin():
                execution = await mark_tool_execution_failed(
                    db,
                    tenant_id=tenant_id,
                    execution_id=reservation.execution.id,
                    lease_owner=lease_owner,
                    result_summary=result_summary,
                )
        return ToolExecutionOutcome(
            status="failed",
            result_summary=execution.result_summary,
            result_ref=execution.result_ref,
        )

    async def execute_pending(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
        tool_calls: tuple[JsonObject, ...],
    ) -> ToolStepResult:
        try:
            tenant_id = uuid.UUID(state["registry"].tenant_id)
            run_id = uuid.UUID(state["registry"].run_id)
            agent = await self._agent(state)
            assistant_message_id = _assistant_message_id(state, tool_calls)
            allowed_names = _allowed_tool_names(await self._tool_provider(agent.id))
            messages: list[JsonObject] = []
            for index, call in enumerate(tool_calls):
                cancel = await self._cancel_source.get_cancel(state, context)
                if cancel is not None:
                    return ToolStepResult(
                        messages=tuple(messages),
                        cancel_signal=cancel,
                    )
                call_id, tool_name, arguments = _call_fields(call)
                if tool_name in _CONTROL_TOOL_NAMES or tool_name not in allowed_names:
                    raise ToolExecutionError(
                        "tool_not_enabled",
                        f"tool {tool_name!r} is not enabled for this Agent",
                    )
                policy = _policy(tool_name)
                lease_owner = f"runtime:{context.command_id}:{call_id}"[:128]
                reservation = await self._reserve(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    assistant_message_id=assistant_message_id,
                    arguments=arguments,
                    policy=policy,
                    lease_owner=lease_owner,
                )
                if reservation.reusable_result is not None:
                    messages.append(
                        _result_message(
                            run_id=run_id,
                            call_id=call_id,
                            tool_name=tool_name,
                            outcome=reservation.reusable_result,
                        )
                    )
                    if tool_name == "send_message_to_agent" and self._a2a_service:
                        waiting_request = a2a_waiting_request(
                            source_run_id=run_id,
                            tool_call_id=call_id,
                            arguments=arguments,
                            result_ref=reservation.reusable_result.result_ref,
                        )
                        if waiting_request is not None:
                            return ToolStepResult(
                                messages=tuple(messages),
                                waiting_request=waiting_request,
                                pending_tool_calls=tool_calls[index + 1 :],
                            )
                    continue
                if reservation.blocked:
                    if reservation.prior_failure is not None:
                        messages.append(
                            _result_message(
                                run_id=run_id,
                                call_id=call_id,
                                tool_name=tool_name,
                                outcome=reservation.prior_failure,
                            )
                        )
                        continue
                    return ToolStepResult(
                        messages=tuple(messages),
                        waiting_request=_waiting_request(
                            run_id=run_id,
                            call_id=call_id,
                            requires_confirmation=reservation.requires_confirmation,
                            error_code=reservation.error_code,
                        ),
                        pending_tool_calls=tool_calls[index:],
                    )

                if tool_name == "send_message_to_agent" and self._a2a_service:
                    try:
                        actor_user_id = (
                            uuid.UUID(context.actor_user_id)
                            if context.actor_user_id
                            else None
                        )
                        a2a_result = await self._a2a_service.execute(
                            tenant_id=tenant_id,
                            source_run_id=run_id,
                            source_agent_id=agent.id,
                            tool_call_id=call_id,
                            arguments=arguments,
                            reservation=reservation,
                            lease_owner=lease_owner,
                            actor_user_id=actor_user_id,
                        )
                    except Exception as exc:
                        outcome = await self._mark_exception(
                            tenant_id=tenant_id,
                            reservation=reservation,
                            lease_owner=lease_owner,
                            policy=policy,
                            exc=exc,
                        )
                        if outcome.status == "unknown":
                            return ToolStepResult(
                                messages=tuple(messages),
                                waiting_request=_waiting_request(
                                    run_id=run_id,
                                    call_id=call_id,
                                    requires_confirmation=True,
                                    error_code="tool_outcome_unknown",
                                ),
                                pending_tool_calls=tool_calls[index:],
                            )
                    else:
                        if a2a_result is not None:
                            messages.append(
                                _result_message(
                                    run_id=run_id,
                                    call_id=call_id,
                                    tool_name=tool_name,
                                    outcome=a2a_result.outcome,
                                )
                            )
                            if a2a_result.waiting_request is not None:
                                return ToolStepResult(
                                    messages=tuple(messages),
                                    waiting_request=a2a_result.waiting_request,
                                    pending_tool_calls=tool_calls[index + 1 :],
                                )
                            continue

                heartbeat_limit = _heartbeat_tool_limit(state, agent, tool_name)
                if heartbeat_limit is not None:
                    successful_count = (
                        0
                        if heartbeat_limit == 0
                        else await self._successful_tool_count(
                            tenant_id=tenant_id,
                            run_id=run_id,
                            tool_name=tool_name,
                        )
                    )
                    if successful_count >= heartbeat_limit:
                        outcome = await self._mark_policy_blocked(
                            tenant_id=tenant_id,
                            reservation=reservation,
                            lease_owner=lease_owner,
                            result_summary=_heartbeat_blocked_summary(
                                agent,
                                tool_name,
                                heartbeat_limit,
                            ),
                        )
                        messages.append(
                            _result_message(
                                run_id=run_id,
                                call_id=call_id,
                                tool_name=tool_name,
                                outcome=outcome,
                            )
                        )
                        continue

                try:
                    raw_result = await self._tool_executor(
                        tool_name,
                        arguments,
                        agent.id,
                        context.actor_user_id and uuid.UUID(context.actor_user_id) or agent.creator_id,
                        state["registry"].session_id or "",
                    )
                    outcome = await self._mark_succeeded(
                        tenant_id=tenant_id,
                        reservation=reservation,
                        lease_owner=lease_owner,
                        result=str(raw_result),
                    )
                except Exception as exc:
                    outcome = await self._mark_exception(
                        tenant_id=tenant_id,
                        reservation=reservation,
                        lease_owner=lease_owner,
                        policy=policy,
                        exc=exc,
                    )
                    if outcome.status == "unknown":
                        return ToolStepResult(
                            messages=tuple(messages),
                            waiting_request=_waiting_request(
                                run_id=run_id,
                                call_id=call_id,
                                requires_confirmation=True,
                                error_code="tool_outcome_unknown",
                            ),
                            pending_tool_calls=tool_calls[index:],
                        )
                messages.append(
                    _result_message(
                        run_id=run_id,
                        call_id=call_id,
                        tool_name=tool_name,
                        outcome=outcome,
                    )
                )
            return ToolStepResult(messages=tuple(messages))
        except ToolExecutionError as exc:
            return ToolStepResult(
                error={"code": exc.code, "message": str(exc)},
            )
        except Exception as exc:
            return ToolStepResult(
                error={
                    "code": "tool_execution_failed",
                    "message": f"Runtime tool step failed: {type(exc).__name__}",
                }
            )


__all__ = ["RuntimeToolStepService", "ToolPolicy"]
