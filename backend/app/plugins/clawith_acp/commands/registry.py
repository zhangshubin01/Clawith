from typing import List, Optional
from .base import AcpCommand


class CommandRegistry:
    """Registry for ACP slash commands."""
    
    def __init__(self):
        self._commands: List[AcpCommand] = []
    
    def register(self, command: AcpCommand) -> None:
        """Register a new command."""
        self._commands.append(command)
    
    def get_all_commands(self) -> List[AcpCommand]:
        """Get all registered commands."""
        return self._commands
    
    def find_command(self, path: List[str]) -> Optional[AcpCommand]:
        """Find a command by path (supports nested subcommands)."""
        current_commands = self._commands
        current_cmd: Optional[AcpCommand] = None
        
        for i, part in enumerate(path):
            for cmd in current_commands:
                if cmd.name == part or part in cmd.aliases:
                    current_cmd = cmd
                    if i < len(path) - 1 and cmd.subcommands:
                        current_commands = cmd.subcommands
                    break
            else:
                return None
        
        return current_cmd
