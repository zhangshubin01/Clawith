"""Code-safety pattern checks shared by sandbox backends.

IMPORTANT: these blacklist checks are NOT a security boundary.  They reject
obvious dangerous snippets before execution, but string matching is trivially
bypassable (``__import__('o'+'s')``, ``/usr/bin/curl``, ...).  The actual
isolation boundary is the sandbox itself (bubblewrap / container / remote
API).  Never rely on this module to keep untrusted code from the host.
"""

import re

from loguru import logger

_DANGEROUS_BASH_ALWAYS = [
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){ :",
    "chmod 777 /",
    "chown ",
    "shutdown",
    "reboot",
]

_DANGEROUS_BASH_NETWORK = [
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
]

_DANGEROUS_PYTHON_IMPORTS_ALWAYS = [
    "shutil.rmtree",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
]

_DANGEROUS_PYTHON_IMPORTS_NETWORK = [
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
]

_DANGEROUS_NODE_ALWAYS = [
    "fs.rmSync",
    "fs.rmdirSync",
    "process.exit",
]

_DANGEROUS_NODE_NETWORK = ["require('http')", "require('https')", "require('net')"]


def check_code_safety(language: str, code: str, allow_network: bool = False) -> str | None:
    """Check code for dangerous patterns. Returns error message if unsafe, None if ok."""
    code_lower = code.lower()

    if language == "bash":
        # Always check dangerous patterns
        for pattern in _DANGEROUS_BASH_ALWAYS:
            if pattern.lower() in code_lower:
                logger.warning(f"Blocked: dangerous command detected ({pattern.strip()})")
                return f"Blocked: dangerous command detected ({pattern.strip()})"
        # Network commands only when network is not allowed
        if not allow_network:
            for pattern in _DANGEROUS_BASH_NETWORK:
                if pattern.lower() in code_lower:
                    logger.warning(f"Blocked: network command not allowed ({pattern.strip()})")
                    return f"Blocked: network command not allowed ({pattern.strip()})"
        if "../../" in code:
            return "Blocked: directory traversal not allowed"

    elif language == "python":
        # Always check dangerous patterns
        for pattern in _DANGEROUS_PYTHON_IMPORTS_ALWAYS:
            if pattern.lower() in code_lower:
                logger.warning(f"Blocked: unsafe operation detected ({pattern.strip()})")
                return f"Blocked: unsafe operation detected ({pattern.strip()})"
        # Network imports only when network is not allowed
        if not allow_network:
            for pattern in _DANGEROUS_PYTHON_IMPORTS_NETWORK:
                if pattern.lower() in code_lower:
                    logger.warning(f"Blocked: network operation not allowed ({pattern.strip()})")
                    return f"Blocked: network operation not allowed ({pattern.strip()})"

    elif language == "node":
        # Always check dangerous patterns
        for pattern in _DANGEROUS_NODE_ALWAYS:
            if pattern.lower() in code_lower:
                return f"Blocked: unsafe operation detected ({pattern})"
        # Network requires only when network is not allowed
        if not allow_network:
            for pattern in _DANGEROUS_NODE_NETWORK:
                if pattern.lower() in code_lower:
                    logger.warning(f"Blocked: network operation not allowed ({pattern.strip()})")
                    return f"Blocked: network operation not allowed ({pattern.strip()})"

    return None


# git subcommands that can mutate/rewind the working tree — direction-5
# "side-effect commands" that a model might invoke while exploring a repo
# (checkout / reset / restore / clean / switch).  This is a recall-only,
# over-inclusive detector (see plan §4.1): string matching is trivially
# bypassable, so semantics stay with the LLM — we only flag and record.
_GIT_SIDE_EFFECT_RE = re.compile(
    r"\bgit\s+(checkout|reset|restore|clean|switch)\b",
    re.IGNORECASE,
)


def detect_git_side_effect_commands(language: str, code: str) -> list[str]:
    """Return the git side-effect subcommands present in ``code``.

    Only ``bash`` is scanned — Python/Node subprocess arg-lists are out of
    scope by design.  Results are deduplicated, lowercased and sorted so the
    value is deterministic for metadata/bookkeeping.  Over-inclusive on
    purpose: false positives are preferred over false negatives (a "git
    checkout" in an echoed string still gets flagged).
    """
    if language != "bash" or not code:
        return []
    return sorted({m.group(1).lower() for m in _GIT_SIDE_EFFECT_RE.finditer(code)})
