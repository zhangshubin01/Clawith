"""memory command - list and clear ACP session memory."""
from .base import AcpCommand, CommandContext, CommandResult
from sqlalchemy import select
from app.database import async_session
from app.models.chat_session import ChatSession
from app.models.agent import Agent


class MemoryCommand(AcpCommand):
    @property
    def name(self) -> str:
        return "memory"
    
    @property
    def description(self) -> str:
        return "List recent ACP chat sessions or clear old sessions"
    
    @property
    def aliases(self) -> list[str]:
        return ["mem"]
    
    async def execute(
        self,
        context: CommandContext,
        args: list[str],
    ) -> CommandResult:
        subcommand = args[0] if args else None
        
        async with async_session() as db:
            # TODO: Get agent_id from context - for now, we just list recent sessions
            # owned by the current user that created this ACP connection
            # agent_id is known from the ACP websocket connection
            # This is just a stub for the interactive ACP command line use when connected over stdio
            
            result = await db.execute(
                select(ChatSession)
                .where(ChatSession.source_channel == "ide_acp")
                .order_by(ChatSession.last_message_at.desc())
                .limit(20),
            )
            
            sessions = result.scalars().all()
            if not sessions:
                return CommandResult(True, "No recent ACP sessions found.")
            
            lines = ["Recent ACP sessions:", ""]
            for s in sessions:
                sid = str(s.id)
                title = s.title or "(untitled)"
                date = (s.last_message_at or s.created_at).strftime('%Y-%m-%d %H:%M')
                lines.append(f"  {sid:<36}  {date:<16}  {title}")
            
            lines.append("")
            lines.append("Use `acp memory clear` to clear old sessions")
            
            return CommandResult(True, "\n".join(lines))
