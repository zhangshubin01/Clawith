"""启动器：运行 integrations/clawith-ide-acp/server.py（正式分发目录）。

分发与文档见 integrations/clawith-ide-acp/README.md
"""

from pathlib import Path
import runpy

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "integrations" / "clawith-ide-acp" / "server.py"
if not _SCRIPT.is_file():
    raise SystemExit(
        f"Missing {_SCRIPT}. Copy integrations/clawith-ide-acp/ from the Clawith repo or restore the file."
    )
runpy.run_path(str(_SCRIPT), run_name="__main__")
