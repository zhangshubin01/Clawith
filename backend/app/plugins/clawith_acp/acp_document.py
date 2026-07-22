"""IDE 打开文件上下文 — 内存 DocumentStore，供 prompt 注入 LLM。"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from loguru import logger

_MAX_CHANGE_SUMMARY = 512


@dataclass
class DocumentState:
    uri: str
    language_id: str | None = None
    version: int = 0
    is_focused: bool = False
    last_change_summary: str = ""


@dataclass
class _SessionDocuments:
    by_uri: dict[str, DocumentState] = field(default_factory=dict)
    focused_uri: str | None = None


class DocumentSessionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, _SessionDocuments] = {}

    def _bucket(self, session_id: str) -> _SessionDocuments:
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionDocuments()
        return self._sessions[session_id]

    def did_open(self, session_id: str, uri: str, *, language_id: str | None = None, version: int = 0) -> None:
        with self._lock:
            bucket = self._bucket(session_id)
            bucket.by_uri[uri] = DocumentState(
                uri=uri, language_id=language_id, version=version,
                is_focused=bucket.focused_uri == uri,
            )
        logger.info("[ACP-DOC] didOpen session={} uri={} lang={}", session_id[:8], uri, language_id)

    def did_change(self, session_id: str, uri: str, *, version: int | None = None, summary: str = "") -> None:
        with self._lock:
            bucket = self._bucket(session_id)
            state = bucket.by_uri.get(uri)
            if state is None:
                state = DocumentState(uri=uri)
                bucket.by_uri[uri] = state
            if version is not None:
                state.version = version
            if summary:
                state.last_change_summary = summary[:_MAX_CHANGE_SUMMARY]
        logger.debug("[ACP-DOC] didChange session={} uri={} v={}", session_id[:8], uri, version)

    def did_close(self, session_id: str, uri: str) -> None:
        with self._lock:
            bucket = self._bucket(session_id)
            bucket.by_uri.pop(uri, None)
            if bucket.focused_uri == uri:
                bucket.focused_uri = None
        logger.info("[ACP-DOC] didClose session={} uri={}", session_id[:8], uri)

    def did_focus(self, session_id: str, uri: str) -> None:
        with self._lock:
            bucket = self._bucket(session_id)
            bucket.focused_uri = uri
            for state in bucket.by_uri.values():
                state.is_focused = state.uri == uri
            if uri not in bucket.by_uri:
                bucket.by_uri[uri] = DocumentState(uri=uri, is_focused=True)
        logger.info("[ACP-DOC] didFocus session={} uri={}", session_id[:8], uri)

    def apply_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        focused = snapshot.get("focusedUri") or snapshot.get("focused_uri")
        open_uris = snapshot.get("openUris") or snapshot.get("open_uris") or []
        language_ids = snapshot.get("languageIds") or snapshot.get("language_ids") or {}
        if not isinstance(open_uris, list):
            open_uris = []
        if not isinstance(language_ids, dict):
            language_ids = {}
        with self._lock:
            bucket = _SessionDocuments()
            bucket.focused_uri = str(focused) if focused else None
            for raw_uri in open_uris:
                uri = str(raw_uri)
                lang = language_ids.get(uri) or language_ids.get(raw_uri)
                bucket.by_uri[uri] = DocumentState(
                    uri=uri,
                    language_id=str(lang) if lang else None,
                    is_focused=uri == bucket.focused_uri,
                )
            self._sessions[session_id] = bucket
        logger.info("[ACP-DOC] snapshot session={} open={} focused={}", session_id[:8], len(open_uris), focused)

    def format_for_prompt(self, session_id: str) -> str:
        with self._lock:
            bucket = self._sessions.get(session_id)
            if not bucket or not bucket.by_uri:
                return ""
            lines = ["## Open Documents (IDE)"]
            for uri, state in bucket.by_uri.items():
                lang = state.language_id or "?"
                focus = " (focused)" if state.is_focused or uri == bucket.focused_uri else ""
                lines.append(f"- {uri} [{lang}]{focus}")
            return "\n".join(lines)

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
        logger.debug("[ACP-DOC] clear session={}", session_id[:8])


document_store = DocumentSessionStore()

DOCUMENT_NOTIFICATION_METHODS = frozenset({
    "document/didOpen", "document/didChange", "document/didClose",
    "document/didFocus", "document/didSave",
})


def handle_document_notification(session_id: str | None, method: str, params: dict[str, Any]) -> None:
    if not session_id:
        logger.warning("[ACP-DOC] 忽略 {}: 无 session_id", method)
        return
    uri = str(params.get("uri") or params.get("documentUri") or "")
    if not uri and method != "document/didSave":
        logger.warning("[ACP-DOC] 忽略 {}: 缺少 uri", method)
        return
    language_id = params.get("languageId") or params.get("language_id")
    version = params.get("version")
    ver_int = int(version) if isinstance(version, int) else 0
    if method == "document/didOpen":
        document_store.did_open(session_id, uri, language_id=language_id, version=ver_int)
    elif method == "document/didChange":
        changes = params.get("changes") or params.get("contentChanges") or []
        summary = ""
        if isinstance(changes, list) and changes:
            first = changes[0] if isinstance(changes[0], dict) else {}
            summary = str(first.get("text") or first.get("content") or "")[:_MAX_CHANGE_SUMMARY]
        document_store.did_change(session_id, uri, version=ver_int if version is not None else None, summary=summary)
    elif method == "document/didClose":
        document_store.did_close(session_id, uri)
    elif method == "document/didFocus":
        document_store.did_focus(session_id, uri)
    elif method == "document/didSave":
        document_store.did_change(session_id, uri, summary="saved")
    else:
        logger.warning("[ACP-DOC] 未知方法 {}", method)
