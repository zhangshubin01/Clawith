"""Debug 会话 NDJSON 埋点 — 后端 Docker 内走 HTTP ingest，本机直写文件。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

SESSION_ID = os.getenv("DEBUG_SESSION_ID", "17de78")
RUN_ID = os.getenv("DEBUG_SESSION_RUN_ID", "backend-observe")
LOG_PATH = Path(
    os.getenv(
        "DEBUG_SESSION_LOG_PATH",
        "/tmp/clawith-debug.log",
    ),
)
INGEST_URLS: tuple[str, ...] = tuple(
    u
    for u in (
        os.getenv(
            "DEBUG_SESSION_INGEST_URL",
            "http://127.0.0.1:7413/ingest/378218e0-08cf-44b2-b3b9-84dbdea9a04e",
        ),
        os.getenv(
            "DEBUG_SESSION_INGEST_URL_DOCKER",
            "http://host.docker.internal:7413/ingest/378218e0-08cf-44b2-b3b9-84dbdea9a04e",
        ),
    )
    if u
)

_warned = False


def debug_session_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict,
    *,
    run_id: str | None = None,
) -> None:
    """追加 debug NDJSON；容器路径不可写时 POST 宿主机 ingest。"""
    # #region agent log
    payload = {
        "sessionId": SESSION_ID,
        "runId": run_id or RUN_ID,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
        return
    except OSError:
        pass

    headers = {
        "Content-Type": "application/json",
        "X-Debug-Session-Id": SESSION_ID,
    }
    for url in INGEST_URLS:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            continue

    global _warned
    if not _warned:
        _warned = True
        logger.warning(
            "[DEBUG-SESSION] 无法写入 debug 日志 path={} ingest_tried={}",
            LOG_PATH,
            len(INGEST_URLS),
        )
    # #endregion
