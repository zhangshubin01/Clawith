from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, List, Optional


class CommandContext:
    """Context passed to command execution."""
    
    def __init__(
        self,
        session_id: str,
        send_message: callable[[str], None],
    ):
        self.session_id = session_id
        self.send_message = send_message


class CommandResult:
    """Result from command execution."""
    
    def __init__(
        self,
        success: bool,
        data: Any = None,
        message: Optional[str] = None,
    ):
        self.success = success
        self.data = data
        self.message = message


class AcpCommand(ABC):
    """Base interface for ACP slash commands."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Command name (e.g., "about")."""
        ...
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Brief description for help output."""
        ...
    
    @property
    def aliases(self) -> List[str]:
        """Alternative names for this command."""
        return []
    
    @property
    def subcommands(self) -> List[AcpCommand]:
        """Nested subcommands (for "extensions list" style)."""
        return []
    
    @abstractmethod
    async def execute(
        self,
        context: CommandContext,
        args: List[str],
    ) -> CommandResult:
        """Execute the command with given arguments."""
        ...
