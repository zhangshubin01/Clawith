"""FastAPI middleware for request tracing and logging."""

import uuid
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging_config import set_trace_id
from loguru import logger


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Middleware to inject trace ID into request context and log requests."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate or extract trace ID from header
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())[:12]
        set_trace_id(trace_id)

        # Add trace ID to request state for access in endpoints
        request.state.trace_id = trace_id

        start_time = time.time()

        # Log request (not bound — entry logs always visible for traceability)
        client_host = request.client.host if request.client else "-"
        logger.info(
            f"--> {request.method} {request.url.path} "
            f"[client: {client_host}]"
        )

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            # Add trace ID to response headers
            response.headers["X-Trace-Id"] = trace_id

            # 大响应负载告警: 超过 50KB 的响应可能影响性能和带宽
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    _size = int(content_length)
                    if _size > 100_000:
                        logger.warning(
                            f"[API] 超大响应: {request.method} {request.url.path} "
                            f"size={_size // 1024}KB status={response.status_code}"
                        )
                    elif _size > 50_000:
                        logger.info(
                            f"[API] 大响应: {request.method} {request.url.path} "
                            f"size={_size // 1024}KB status={response.status_code}"
                        )
                except ValueError:
                    pass

            # Log response (bind request_info so the noise filter can evaluate it)
            logger.bind(
                request_info={
                    "path": request.url.path,
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration": duration,
                }
            ).info(
                f"<-- {request.method} {request.url.path} "
                f"{response.status_code} {duration:.3f}s"
            )

            return response

        except Exception:
            duration = time.time() - start_time
            logger.error(
                f"<-- {request.method} {request.url.path} "
                f"ERROR {duration:.3f}s", exc_info=True
            )
            raise
