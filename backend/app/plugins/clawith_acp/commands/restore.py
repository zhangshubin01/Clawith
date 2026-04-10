"""restore command - restore an existing chat session from database."""
from .base import AcpCommand, CommandContext, CommandResult
from sqlalchemy import select
from app.database import async_session
from app.models.chat_session import ChatSession


class RestoreCommand(AcpCommand):
    @property
    def name(self) -> str:
        return "restore"
    
    @property
    def description(self) -> str:
        return "Restore an existing chat session from database by session ID"
    
    async def execute(
        self,
        context: CommandContext,
        args: list[str],
    ) -> CommandResult:
        session_id = args[0] if args else None
        if not session_id:
            return CommandResult(False, message="Missing session_id argument. Usage: acp restore <session-id>")
        
        try:
            sid_uuid = uuid.UUID(session_id)
        except ValueError:
            return CommandResult(False, message=f"Invalid session ID '{session_id}': must be a valid UUID.")
        
        async with async_session() as db:
            result = await db.execute(
                select(ChatSession).where(ChatSession.id == sid_uuid)
            )
            sess = result.scalar_one_or_none()
            if not sess:
                return CommandResult(False, message=f"Session {session_id} not found in database.")
        
        return CommandResult(
            True,
            f"✅ Session {session_id} found:\n"
            f"  Title: {sess.title or '(untitled)'}\n"
            f"  Created: {sess.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"  You can now continue chatting in this session.",
        )
