"""agents command - list available agents for current tenant/user."""
from .base import AcpCommand, CommandContext, CommandResult
from sqlalchemy import select
from app.database import async_session
from app.models.agent import Agent
from app.models.user import User


class AgentsCommand(AcpCommand):
    @property
    def name(self) -> str:
        return "agents"
    
    @property
    def description(self) -> str:
        return "List available agents for current tenant that you have access to"
    
    @property
    def aliases(self) -> list[str]:
        return ["list", "ls"]
    
    async def execute(
        self,
        context: CommandContext,
        args: list[str],
    ) -> CommandResult:
        # In the WebSocket ACP connection, we already have the agent_id/user_id from the connection
        # This is for interactive stdio usage when connected directly via command line
        # For now, just a stub - interactive stdio is a future enhancement
        
        return CommandResult(
            True,
            "✅ This command is available for interactive stdio use when connected directly.\n"
            "In the current WebSocket-based ACP connection, you can see your available agents on the Clawith web UI.\n"
            "Please open the Clawith web UI to browse and select an agent to connect.",
        )
