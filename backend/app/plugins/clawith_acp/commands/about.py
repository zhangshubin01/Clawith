"""about command - display version information about Clawith ACP."""
from .base import AcpCommand, CommandContext, CommandResult
import app


class AboutCommand(AcpCommand):
    @property
    def name(self) -> str:
        return "about"
    
    @property
    def description(self) -> str:
        return "Show version information about Clawith and ACP protocol"
    
    async def execute(
        self,
        context: CommandContext,
        args: list[str],
    ) -> CommandResult:
        from app import __version__
        from .registry import CommandRegistry
        
        content = f"""\
Clawith - Multi-agent collaboration platform

Version: {__version__}

ACP (Agent Client Protocol) integration:
  - Enables JetBrains IDE (IDEA / Android Studio / WebStorm) to use Clawith as AI backend
  - Supports IDE file system proxy (ide_read_file / ide_write_file)
  - Supports terminal command execution in IDE
  - Supports structured diff preview for code editing
  - Multi-modal (text / image / audio)

Website: https://github.com/dataelement/Clawith
"""
        return CommandResult(True, content)
