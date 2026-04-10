"""help command - display help for available ACP commands."""
from .base import AcpCommand, CommandContext, CommandResult
from .registry import CommandRegistry


class HelpCommand(AcpCommand):
    def __init__(self, registry: CommandRegistry):
        self._registry = registry
    
    @property
    def name(self) -> str:
        return "help"
    
    @property
    def description(self) -> str:
        return "Show help for all available ACP commands"
    
    async def execute(
        self,
        context: CommandContext,
        args: list[str],
    ) -> CommandResult:
        commands = self._registry.get_all_commands()
        
        lines = [
            "Available ACP commands:",
            "",
        ]
        
        for cmd in commands:
            alias_text = ""
            if cmd.aliases:
                alias_text = f" ({', '.join(cmd.aliases)})"
            lines.append(f"  {cmd.name:<12} - {cmd.description}{alias_text}")
        
        lines.append("")
        lines.append("These commands can be used from the ACP interactive terminal prompt when connected over stdio,")
        lines.append("or via the web UI chat when connected over WebSocket ACP connection.")
        
        content = "\n".join(lines)
        return CommandResult(True, content)
