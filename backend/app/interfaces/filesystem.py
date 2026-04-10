"""File system service interface."""
from abc import ABC, abstractmethod
from typing import Optional


class FileSystemService(ABC):
    """Abstract interface for file system operations.

    Different implementations can delegate to IDE (via ACP/MCP) or use native filesystem.
    """

    @abstractmethod
    async def read_text_file(self, file_path: str) -> str:
        """Read content from a text file."""
        ...

    @abstractmethod
    async def write_text_file(self, file_path: str, content: str) -> None:
        """Write content to a text file."""
        ...
