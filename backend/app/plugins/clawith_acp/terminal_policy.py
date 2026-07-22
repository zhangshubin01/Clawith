"""ACP 终端命令分类 — 与 IDE TerminalService.timeoutForCommand 对齐。

后端据此决定 blocking(wait_for_exit) 还是 streaming(poll)，避免 Gradle 静默期被误判 DISAPPEARED。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from loguru import logger


class TerminalMode(str, Enum):
    """终端执行模式：blocking 等待完整结果；streaming 增量推送。"""

    BLOCKING = "blocking"
    STREAMING = "streaming"


@dataclass(frozen=True)
class TerminalPolicy:
    """单条命令的超时与路由策略。"""

    mode: TerminalMode
    timeout_seconds: float
    bucket: str  # 日志 bucket，与 IDE TerminalService 命名一致


def routing_mode_from_env() -> str:
    """ACP_TERMINAL_ROUTING=auto|blocking|streaming，默认 auto 按 policy 决策。"""
    return os.getenv("ACP_TERMINAL_ROUTING", "auto").strip().lower()


def resolve_terminal_policy(command: str) -> TerminalPolicy:
    """分类规则必须与 demo-new TerminalService.timeoutForCommand 一致。

    注意：gradle/gradlew/mvn 优先于 test 子串 —— ./gradlew test 为 300s 而非 600s。
    """
    lower = (command or "").lower()
    preview = (command or "")[:120] or "<empty>"

    if any(p in lower for p in ("tail -f", "watch ", "kubectl logs -f")):
        policy = TerminalPolicy(TerminalMode.STREAMING, 600.0, "interactive")
    elif "gradle" in lower or "gradlew" in lower or "mvn" in lower:
        policy = TerminalPolicy(TerminalMode.BLOCKING, 300.0, "build-tool(300s)")
    elif "test" in lower or " build" in lower or lower.startswith("build"):
        policy = TerminalPolicy(TerminalMode.BLOCKING, 600.0, "long(600s)")
    else:
        policy = TerminalPolicy(TerminalMode.BLOCKING, 30.0, "default(30s)")

    logger.info(
        "[ACP-TERM-POLICY] bucket={} mode={} timeoutSec={} cmdPreview={}",
        policy.bucket,
        policy.mode.value,
        int(policy.timeout_seconds),
        preview,
    )
    return policy


def effective_terminal_mode(policy: TerminalPolicy) -> TerminalMode:
    """结合环境变量覆盖，得到最终执行模式。"""
    routing = routing_mode_from_env()
    if routing == "blocking":
        return TerminalMode.BLOCKING
    if routing == "streaming":
        return TerminalMode.STREAMING
    return policy.mode
