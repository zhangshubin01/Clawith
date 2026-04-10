"""init command - initialize ACP configuration directory."""
from .base import AcpCommand, CommandContext, CommandResult
import os
import pathlib


class InitCommand(AcpCommand):
    @property
    def name(self) -> str:
        return "init"
    
    @property
    def description(self) -> str:
        return "Initialize ACP configuration directory in the current project"
    
    async def execute(
        self,
        context: CommandContext,
        args: list[str],
    ) -> CommandResult:
        cwd = pathlib.Path.cwd()
        acp_dir = cwd / '.clawith' / 'acp'
        if acp_dir.exists():
            return CommandResult(
                True,
                f"Directory {acp_dir} already exists. Nothing to do.",
            )
        
        acp_dir.mkdir(parents=True, exist_ok=True)
        
        # Create basic config file
        config_file = acp_dir / 'config.json'
        if not config_file.exists():
            default_config = {
                "default_model": None,
                "default_mode": "default",
                "auto_approve_diff": False,
            }
            config_file.write_text(
                f'{json.dumps(default_config, indent=2, ensure_ascii=False)}\n',
            )
        
        return CommandResult(
            True,
            f"✅ Initialized ACP configuration directory at {acp_dir}/\n\n"
            f"You can edit {config_file} to set your default model and mode.",
        )
