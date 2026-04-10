"""clawith-acp - Agent Client Protocol (ACP) plugin for JetBrains IDE integration.

This plugin provides a WebSocket endpoint for JetBrains IDE (IDEA/Android Studio) to connect to
Clawith as an AI backend. It supports:
- Text/image/audio multi-modal prompts
- IDE file system proxy (ide_read_file/ide_write_file etc)
- Terminal command execution in IDE
- Structured diff preview for file editing with permission approval
- Cancellation support for running prompts
- Dynamic model/mode selection
"""
from .plugin import register_plugin

register_plugin()
