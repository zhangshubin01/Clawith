"""OpenViking HTTP client for semantic memory retrieval.

Provides optional integration with a local OpenViking server to replace
full memory.md injection with relevance-ranked snippets.

Requires:
    openviking-server --workers 4 running at OPENVIKING_URL (default: http://127.0.0.1:1933)

Falls back gracefully if OpenViking is unavailable.
"""

import os
import time
from pathlib import Path

import httpx
from loguru import logger

OPENVIKING_URL = os.environ.get("OPENVIKING_URL", "http://127.0.0.1:1933")
OPENVIKING_API_KEY = os.environ.get("OPENVIKING_API_KEY", "")

# Cache availability result for 30 seconds to avoid repeated checks
_last_available_check = 0.0
_cached_available: bool | None = None

def _get_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if OPENVIKING_API_KEY:
        headers["Authorization"] = f"Bearer {OPENVIKING_API_KEY}"
    return headers

async def is_available() -> bool:
    """Check if OpenViking server is reachable and healthy. Cache result for 30 seconds."""
    global _last_available_check, _cached_available

    now = time.time()
    if _cached_available is not None and (now - _last_available_check) < 30:
        return _cached_available

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{OPENVIKING_URL}/health",
                headers=_get_headers(),
                timeout=5.0
            )
            _cached_available = response.status_code == 200
            if _cached_available:
                logger.info("[OpenViking] Health check passed")
            else:
                logger.warning(f"[OpenViking] Health check failed: {response.status_code}")
    except Exception as e:
        _cached_available = False
        logger.warning(f"[OpenViking] Health check failed: {e}")

    _last_available_check = now
    return _cached_available

async def search_memory(
    query: str,
    agent_id: str,
    top_k: int = 5,
    grep_filter: str | None = None,
) -> list[dict]:
    """
    Search agent's personal memory for semantic similarity to query.

    Args:
        query: Search query text
        agent_id: Agent ID (scopes search to this agent's memory)
        top_k: Number of top results to return
        grep_filter: Optional keyword filter to filter documents before search

    Returns:
        List of results with "path", "content", "score" keys, sorted by score descending
    """
    available = await is_available()
    if not available:
        return []

    try:
        payload = {
            "query": query,
            "scope": f"agent:{agent_id}",
            "top_k": top_k,
        }
        if grep_filter:
            payload["grep_filter"] = grep_filter

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{OPENVIKING_URL}/search",
                json=payload,
                headers=_get_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("results", [])
    except Exception as e:
        logger.warning(f"[OpenViking] search_memory failed: {e}")
        return []

async def index_memory_file(
    file_path: str,
    agent_id: str,
    content: str | None = None,
) -> bool:
    """
    Index or update a single memory file for an agent.
    Called automatically after writing to memory.md to keep index fresh.

    Args:
        file_path: Full path to the file
        agent_id: Agent ID for scoping
        content: Optional pre-read content (avoids re-reading)

    Returns:
        True if indexing succeeded, False otherwise
    """
    available = await is_available()
    if not available:
        return False

    if content is None:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"[OpenViking] Failed to read {file_path}: {e}")
            return False

    try:
        payload = {
            "path": file_path,
            "content": content,
            "scope": f"agent:{agent_id}",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{OPENVIKING_URL}/index",
                json=payload,
                headers=_get_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            logger.debug(f"[OpenViking] Indexed {file_path} in scope agent:{agent_id}")
            return True
    except Exception as e:
        logger.warning(f"[OpenViking] index_memory_file failed: {e}")
        return False

async def search_enterprise(
    query: str,
    top_k: int = 5,
    grep_filter: str | None = None,
) -> list[dict]:
    """
    Search enterprise shared information (enterprise_info/ directory) for semantic similarity.

    Args:
        query: Search query text
        top_k: Number of top results to return
        grep_filter: Optional keyword filter to filter documents before search

    Returns:
        List of results with "path", "content", "score" keys, sorted by score descending
    """
    available = await is_available()
    if not available:
        return []

    try:
        payload = {
            "query": query,
            "scope": "enterprise",
            "top_k": top_k,
        }
        if grep_filter:
            payload["grep_filter"] = grep_filter

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{OPENVIKING_URL}/search",
                json=payload,
                headers=_get_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("results", [])
    except Exception as e:
        logger.warning(f"[OpenViking] search_enterprise failed: {e}")
        return []

def _find_enterprise_dir(root_path: Path) -> Path | None:
    """Find enterprise_info directory, trying multiple locations."""
    # Try given root path
    candidate = root_path / "enterprise_info"
    if candidate.exists():
        return candidate
    # Try current working directory (where agent runs)
    candidate = Path.cwd() / "enterprise_info"
    if candidate.exists():
        return candidate
    return None

def _find_skills_dir(root_path: Path) -> Path | None:
    """Find skills directory, trying multiple locations."""
    # Try given root path
    candidate = root_path / "skills"
    if candidate.exists():
        return candidate
    # Try current working directory (where agent runs)
    candidate = Path.cwd() / "skills"
    if candidate.exists():
        return candidate
    return None

async def index_enterprise_info(root_path: Path) -> bool:
    """
    Index all markdown files in enterprise_info/ directory into enterprise scope.
    Called on startup to keep shared info searchable.

    Args:
        root_path: Project root path

    Returns:
        True if indexing succeeded (partial success counts as True)
    """
    available = await is_available()
    if not available:
        logger.info("[OpenViking] Not available, skipping enterprise index")
        return False

    enterprise_dir = _find_enterprise_dir(root_path)
    if enterprise_dir is None:
        logger.info("[OpenViking] enterprise_info directory not found, skipping")
        return False

    success_count = 0
    fail_count = 0

    for md_path in enterprise_dir.rglob("*.md"):
        try:
            content = md_path.read_text(encoding="utf-8")
            rel_path = str(md_path.relative_to(enterprise_dir.parent))
            payload = {
                "path": rel_path,
                "content": content,
                "scope": "enterprise",
            }
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{OPENVIKING_URL}/index",
                    json=payload,
                    headers=_get_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
                success_count += 1
        except Exception as e:
            logger.warning(f"[OpenViking] Failed to index {md_path}: {e}")
            fail_count += 1

    logger.info(f"[OpenViking] Indexed enterprise_info: {success_count} succeeded, {fail_count} failed")
    return fail_count == 0

async def search_skills(
    query: str,
    top_k: int = 5,
    grep_filter: str | None = None,
) -> list[dict]:
    """
    Search installed skills documentation for semantic similarity.

    Args:
        query: Search query text
        top_k: Number of top results to return
        grep_filter: Optional keyword filter to filter documents before search

    Returns:
        List of results with "path", "content", "score" keys, sorted by score descending
    """
    available = await is_available()
    if not available:
        return []

    try:
        payload = {
            "query": query,
            "scope": "skills",
            "top_k": top_k,
        }
        if grep_filter:
            payload["grep_filter"] = grep_filter

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{OPENVIKING_URL}/search",
                json=payload,
                headers=_get_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("results", [])
    except Exception as e:
        logger.warning(f"[OpenViking] search_skills failed: {e}")
        return []

async def index_all_skills(root_path: Path) -> bool:
    """
    Index all installed skills (skills/*/*SKILL.md) into skills scope.
    Called on startup to keep skill recommendations semantic-searchable.

    Args:
        root_path: Project root path

    Returns:
        True if indexing succeeded (partial success counts as True)
    """
    available = await is_available()
    if not available:
        logger.info("[OpenViking] Not available, skipping skills index")
        return False

    skills_dir = _find_skills_dir(root_path)
    if skills_dir is None:
        logger.info("[OpenViking] skills directory not found, skipping")
        return False

    success_count = 0
    fail_count = 0

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            content = skill_file.read_text(encoding="utf-8")
            rel_path = str(skill_file.relative_to(skills_dir.parent))
            payload = {
                "path": rel_path,
                "content": content,
                "scope": "skills",
            }
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{OPENVIKING_URL}/index",
                    json=payload,
                    headers=_get_headers(),
                    timeout=10.0,
                )
                response.raise_for_status()
                success_count += 1
        except Exception as e:
            logger.warning(f"[OpenViking] Failed to index skill {skill_file}: {e}")
            fail_count += 1

    logger.info(f"[OpenViking] Indexed skills: {success_count} succeeded, {fail_count} failed")
    return fail_count == 0

async def index_all_agents(clawith_data_root: Path) -> bool:
    """
    Index all agents' core documents (soul.md, memory.md, focus.md, reflections.md)
    into a shared agent_docs scope for cross-agent semantic search.

    This allows querying across all agents for knowledge and patterns.

    Args:
        clawith_data_root: Clawith data root, typically ~/.clawith/data

    Returns:
        True if indexing succeeded (partial success counts as True)
    """
    available = await is_available()
    if not available:
        logger.info("[OpenViking] Not available, skipping agents index")
        return False

    # Search from ~/.clawith recursively to catch any agent document locations
    root = Path(os.path.expanduser("~/.clawith"))
    if not root.exists():
        logger.info("[OpenViking] ~/.clawith directory not found, skipping")
        return False

    success_count = 0
    fail_count = 0
    target_files = {"soul.md", "memory.md", "focus.md", "reflections.md"}

    # Use rglob to find all target files anywhere under ~/.clawith
    for target in target_files:
        for doc_path in root.rglob(target):
            if not doc_path.is_file():
                continue
            # Skip files inside virtual environments or node_modules
            if ".venv" in str(doc_path) or "node_modules" in str(doc_path):
                continue
            try:
                content = doc_path.read_text(encoding="utf-8")
                # Get relative path from home
                rel_path = str(doc_path.relative_to(Path.home()))
                # Try to extract agent_id from path components (UUID-like directories)
                agent_id = "unknown"
                parts = doc_path.parts
                for part in parts:
                    # Agent directories are UUIDs with dashes and 36 chars
                    if len(part) == 36 and '-' in part:
                        agent_id = part
                        break
                payload = {
                    "path": rel_path,
                    "content": content,
                    "scope": "agent_docs",
                    "metadata": {
                        "agent_id": agent_id,
                        "document_type": target.split(".")[0],
                    }
                }
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{OPENVIKING_URL}/index",
                        json=payload,
                        headers=_get_headers(),
                        timeout=10.0,
                    )
                    response.raise_for_status()
                    success_count += 1
            except Exception as e:
                logger.warning(f"[OpenViking] Failed to index agent doc {doc_path}: {e}")
                fail_count += 1

    logger.info(f"[OpenViking] Indexed all agents: {success_count} succeeded, {fail_count} failed")
    return fail_count == 0

async def search_all_agents(
    query: str,
    top_k: int = 5,
    grep_filter: str | None = None,
    agent_id: str | None = None,
) -> list[dict]:
    """
    Search across all agents' indexed documents in agent_docs scope.

    Args:
        query: Search query text
        top_k: Number of top results to return
        grep_filter: Optional keyword filter to filter documents before search
        agent_id: Optional filter to only search within a specific agent

    Returns:
        List of results with "path", "content", "score", "metadata" keys
    """
    available = await is_available()
    if not available:
        return []

    try:
        payload = {
            "query": query,
            "scope": "agent_docs",
            "top_k": top_k,
        }
        if grep_filter:
            payload["grep_filter"] = grep_filter
        if agent_id:
            payload["metadata_filter"] = {"agent_id": agent_id}

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{OPENVIKING_URL}/search",
                json=payload,
                headers=_get_headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("results", [])
    except Exception as e:
        logger.warning(f"[OpenViking] search_all_agents failed: {e}")
        return []
