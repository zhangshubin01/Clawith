"""ACP-based file system service that delegates to JetBrains IDE.

Design:
- Files inside the IDE project root → delegate to IDE via ACP RPC
- Files outside the project root OR inside ~/.clawith → fall back to native
- This respects IDE's permission model and triggers proper indexing
"""
import os
import pathlib
from typing import Optional, Dict
from uuid import UUID
from app.interfaces.filesystem import FileSystemService
from .connection import AgentSideConnection
from .errors import normalize_error


class AcpFileSystemService(FileSystemService):
    """ACP-based file system service that delegates to JetBrains IDE.
    
    Design:
    - Files inside the IDE project root → delegate to IDE via ACP RPC
    - Files outside the project root OR inside ~/.clawith → fall back to native
    - This respects IDE's permission model and triggers proper indexing
    """
    
    def __init__(
        self,
        connection: AgentSideConnection,
        session_id: UUID,
        capabilities: Dict[str, bool],
        fallback: FileSystemService,
        root: str,
    ):
        self._connection = connection
        self._session_id = session_id
        self._capabilities = capabilities
        self._fallback = fallback
        self._root = pathlib.Path(root).resolve()
        self._clawith_dir = pathlib.Path.home() / '.clawith'
    
    def _should_use_fallback(self, file_path: str) -> bool:
        """Determine whether to use native fallback or delegate to IDE."""
        path = pathlib.Path(file_path).resolve()
        
        # Rule 1: If not within project root → always fallback
        try:
            path.relative_to(self._root)
        except ValueError:
            return True
        
        # Rule 2: If within ~/.clawith → always fallback (configuration/cache)
        try:
            path.relative_to(self._clawith_dir)
            return True
        except ValueError:
            pass
        
        # Rule 3: Check if IDE supports the operation
        return False
    
    async def read_text_file(self, file_path: str) -> str:
        """Read a text file.
        
        If file is outside project or in .clawith → fallback to native.
        Otherwise → delegate to IDE via ACP.
        """
        if (
            not self._capabilities.get('readTextFile', False) or
            self._should_use_fallback(file_path)
        ):
            return await self._fallback.read_text_file(file_path)
        
        try:
            await self._connection.read_text_file(
                path=file_path,
                sessionId=self._session_id,
            )
            # Response will be handled in the receive loop by the pending request future
            # This method is only used when making synchronous RPC calls for permission requests
            return "{}"  # noqa: unreachable
        except Exception as err:
            normalize_error(err)
    
    async def write_text_file(self, file_path: str, content: str) -> None:
        """Write a text file.
        
        Same fallback rules as read.
        """
        if (
            not self._capabilities.get('writeTextFile', False) or
            self._should_use_fallback(file_path)
        ):
            await self._fallback.write_text_file(file_path, content)
            return
        
        try:
            await self._connection.write_text_file(
                path=file_path,
                content=content,
                sessionId=self._session_id,
            )
        except Exception as err:
            normalize_error(err)
