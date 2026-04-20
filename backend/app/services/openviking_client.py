"""OpenViking HTTP client for semantic memory retrieval.

Provides optional integration with a local OpenViking server to replace
full memory.md injection with relevance-ranked snippets.

Requires:
    openviking-server --workers 4 running at OPENVIKING_URL (default: http://127.0.0.1:1933)

Falls back gracefully if OpenViking is unavailable.

Scoping: memories are scoped per agent via X-OpenViking-Agent header.
Indexing: uses session/extract pipeline (POST content as text, not file path).
"""

import os
import re
import time
from pathlib import Path

import httpx
from loguru import logger

OPENVIKING_URL = os.environ.get("OPENVIKING_URL", "http://127.0.0.1:1933").rstrip("/")
OPENVIKING_TIMEOUT = float(os.environ.get("OPENVIKING_TIMEOUT", "3.0"))
OPENVIKING_LIMIT = int(os.environ.get("OPENVIKING_LIMIT", "5"))
OPENVIKING_SCORE_THRESHOLD = float(os.environ.get("OPENVIKING_SCORE_THRESHOLD", "0.35"))

# ── 持久化连接池（进程级单例）───────────────────────────────
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=OPENVIKING_URL,
            timeout=OPENVIKING_TIMEOUT,
            limits=httpx.Limits(
                max_connections=30,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
        )
    return _client


def _agent_headers(agent_id: str) -> dict:
    """每个 agent 用独立的 X-OpenViking-Agent header 隔离记忆空间。"""
    return {"X-OpenViking-Agent": f"clawith-{agent_id}"}


# ── 可用性缓存（避免每次请求都做健康检查）─────────────────
_available: bool | None = None
_available_checked_at: float = 0.0
_AVAILABILITY_TTL = 30.0


async def is_available() -> bool:
    """带缓存的健康检查，30 秒内只真正检查一次。"""
    global _available, _available_checked_at
    now = time.monotonic()
    if _available is not None and now - _available_checked_at < _AVAILABILITY_TTL:
        return _available
    try:
        resp = await _get_client().get("/health", timeout=1.0)
        data = resp.json() if resp.status_code == 200 else {}
        _available = data.get("healthy", False) or resp.status_code < 500
    except Exception:
        _available = False
    _available_checked_at = now
    return _available


def _invalidate_availability_cache() -> None:
    global _available, _available_checked_at
    _available = None
    _available_checked_at = 0.0


async def search_memory(query: str, agent_id: str, limit: int = OPENVIKING_LIMIT) -> list[str]:
    """Query OpenViking for memories relevant to *query* scoped to *agent_id*.

    Uses X-OpenViking-Agent header for agent-level scope isolation.
    Returns [] on any failure.
    """
    try:
        resp = await _get_client().post(
            "/api/v1/search/find",
            headers=_agent_headers(agent_id),
            json={
                "query": query,
                "limit": limit,
                "score_threshold": OPENVIKING_SCORE_THRESHOLD,
            },
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        memories = data.get("result", {}).get("memories", [])
        snippets = []
        for m in memories:
            content = m.get("content") or m.get("abstract") or ""
            if content:
                snippets.append(content.strip())
        return snippets

    except httpx.ConnectError:
        _invalidate_availability_cache()
        logger.debug("[OpenViking] connection lost, availability cache cleared")
        return []
    except Exception as e:
        logger.debug(f"[OpenViking] search_memory failed: {e}")
        return []


def _strip_markdown(text: str) -> str:
    """去掉 markdown 标题符号、代码块标记，保留纯文本内容供 OpenViking 提取。"""
    # 去掉代码块
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # 去掉标题 # 符号
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 去掉加粗/斜体符号
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    # 去掉表格分隔行
    text = re.sub(r"^\|[-| :]+\|$", "", text, flags=re.MULTILINE)
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def index_memory_file(agent_id: str, file_path: Path) -> bool:
    """Index a memory file into OpenViking via the session/extract pipeline.

    Reads file content → strips markdown → creates a session → sends content
    → fires extract (no wait) → cleans up session.
    All scoped to agent via X-OpenViking-Agent header.
    """
    if not file_path.exists():
        return False
    try:
        raw = file_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.debug(f"[OpenViking] index_memory_file read error: {e}")
        return False

    content = _strip_markdown(raw)
    if not content:
        return False

    headers = _agent_headers(agent_id)
    client = _get_client()
    session_id = None
    try:
        # 1. 创建 session
        r = await client.post("/api/v1/sessions", headers=headers, json={},
                              timeout=OPENVIKING_TIMEOUT)
        if r.status_code != 200:
            return False
        session_id = r.json().get("result", {}).get("session_id")
        if not session_id:
            return False

        # 2. 发送 memory 内容
        await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            headers=headers,
            json={"role": "user", "content": content},
            timeout=OPENVIKING_TIMEOUT,
        )

        # 3. 触发 extract，不等待结果（LLM 提取耗时，fire-and-forget）
        import asyncio
        asyncio.create_task(_fire_extract(client, session_id, headers))

        logger.debug(f"[OpenViking] triggered index for {file_path.name} agent={agent_id}")
        return True

    except httpx.ConnectError:
        _invalidate_availability_cache()
        return False
    except Exception as e:
        logger.debug(f"[OpenViking] index_memory_file error: {e}")
        return False


async def _fire_extract(client: httpx.AsyncClient, session_id: str, headers: dict) -> None:
    """Fire extract and cleanup in background, ignoring errors."""
    try:
        await client.post(
            f"/api/v1/sessions/{session_id}/extract",
            headers=headers,
            json={},
            timeout=30.0,  # 给 LLM 提取足够时间
        )
    except Exception:
        pass
    finally:
        try:
            await client.delete(f"/api/v1/sessions/{session_id}",
                                headers=headers, timeout=2.0)
        except Exception:
            pass


async def index_enterprise_info(project_root: Path) -> bool:
    """Index enterprise information from the project into OpenViking.

    Searches for enterprise info files (README.md, docs/*.md) and indexes them
    into OpenViking for semantic search.

    Args:
        project_root: Root path of the project

    Returns:
        True if indexing was successful, False otherwise
    """
    if not await is_available():
        return False

    # Look for enterprise info files
    enterprise_dirs = [
        project_root / "docs",
        project_root / "enterprise_info",
    ]

    indexed_count = 0
    for enterprise_dir in enterprise_dirs:
        if not enterprise_dir.exists():
            continue

        # Index markdown files in the enterprise directory
        for md_file in enterprise_dir.rglob("*.md"):
            try:
                # Use a special agent_id for enterprise info
                success = await index_memory_file("enterprise", md_file)
                if success:
                    indexed_count += 1
                    logger.debug(f"[OpenViking] Indexed enterprise file: {md_file}")
            except Exception as e:
                logger.debug(f"[OpenViking] Failed to index {md_file}: {e}")

    if indexed_count > 0:
        logger.info(f"[OpenViking] Indexed {indexed_count} enterprise info files")
    return indexed_count > 0


async def index_all_skills(project_root: Path) -> bool:
    """Index all skill definitions from the project into OpenViking.

    Searches for skill definition files and indexes them for semantic search.

    Args:
        project_root: Root path of the project

    Returns:
        True if indexing was successful, False otherwise
    """
    if not await is_available():
        return False

    # Look for skill definition files
    skills_dirs = [
        project_root / "skills",
        project_root / "app" / "skills",
    ]

    indexed_count = 0
    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue

        # Index skill definition files
        for skill_file in skills_dir.rglob("*.md"):
            try:
                # Use a special agent_id for skills
                success = await index_memory_file("skills", skill_file)
                if success:
                    indexed_count += 1
                    logger.debug(f"[OpenViking] Indexed skill file: {skill_file}")
            except Exception as e:
                logger.debug(f"[OpenViking] Failed to index {skill_file}: {e}")

    if indexed_count > 0:
        logger.info(f"[OpenViking] Indexed {indexed_count} skill files")
    return indexed_count > 0
