"""File watcher to automatically trigger OpenViking incremental indexing when agent memory changes."""

import asyncio
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent

from loguru import logger

from .openviking_client import index_memory_file

class AgentMemoryWatcher(FileSystemEventHandler):
    """Watch for changes to memory.md and trigger incremental indexing automatically."""

    def __init__(self, agents_root: Path):
        self.agents_root = agents_root
        self.debounce_task: asyncio.Task | None = None
        self.debounce_delay = 2.0  # seconds - debounce rapid successive saves

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        # Only trigger for memory.md changes (the most frequently updated document)
        if path.name != "memory.md":
            return

        # Extract agent_id from parent directory (agents/<agent_id>/memory.md)
        agent_id = path.parent.name
        logger.debug(f"[OpenViking] Detected memory change for agent {agent_id}: {path}")

        # Debounce: avoid multiple reindexes for rapid saves
        if self.debounce_task and not self.debounce_task.done():
            self.debounce_task.cancel()

        # Schedule reindex after debounce delay
        try:
            loop = asyncio.get_running_loop()
            self.debounce_task = loop.create_task(
                self._debounced_index(agent_id, str(path))
            )
        except RuntimeError:
            # No running event loop (shouldn't happen in our setup)
            logger.warning("[OpenViking] No running event loop for incremental indexing")

    async def _debounced_index(self, agent_id: str, path: str):
        """Wait for file to stabilize before indexing."""
        await asyncio.sleep(self.debounce_delay)
        logger.info(f"[OpenViking] Triggering incremental indexing for {path}")
        await index_memory_file(path, agent_id)

def start_watcher(agents_root: Path = Path.home() / ".clawith" / "data" / "agents") -> Observer:
    """Start the file watcher in a background thread.

    Args:
        agents_root: Root directory containing agent data (default: ~/.clawith/data/agents)

    Returns:
        Running Observer instance
    """
    if not agents_root.exists():
        logger.warning(f"[OpenViking] Agents root {agents_root} does not exist, not starting watcher")
        return None

    event_handler = AgentMemoryWatcher(agents_root)
    observer = Observer()
    observer.schedule(event_handler, str(agents_root), recursive=True)
    observer.start()
    logger.info(f"[OpenViking] Started memory change watcher at {agents_root}")
    return observer
