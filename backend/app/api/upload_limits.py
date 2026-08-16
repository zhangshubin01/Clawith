"""Shared upload size-limit enforcement for file-upload endpoints.

Every upload endpoint in this codebase buffers the full request body into
memory with ``await file.read()`` before validating it, so without a cap a
single authenticated request can exhaust the process (P0-3). Enforce the
configured limit in two layers:

- ``precheck_content_length`` rejects requests whose Content-Length header
  already exceeds the cap — cheap, before any byte is read.
- ``enforce_content_limit`` hard-checks the bytes after ``read()``, because
  Content-Length can be absent (chunked transfer) or forged.
"""

from fastapi import HTTPException, Request

from app.config import get_settings

# Chat image uploads are base64-encoded into the model payload; keep them small.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def max_upload_bytes() -> int:
    return get_settings().MAX_UPLOAD_BYTES


def _detail(max_bytes: int, noun: str) -> str:
    return f"{noun} too large (max {max_bytes // (1024 * 1024)}MB)"


def precheck_content_length(
    request: Request,
    *,
    max_bytes: int | None = None,
    status_code: int = 413,
    noun: str = "File",
) -> None:
    """Reject before reading when the declared Content-Length exceeds the cap."""
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        length = int(raw)
    except ValueError:
        return  # Unparseable header; the post-read check still applies.
    if length > limit:
        raise HTTPException(status_code=status_code, detail=_detail(limit, noun))


def enforce_content_limit(
    content: bytes,
    *,
    max_bytes: int | None = None,
    status_code: int = 413,
    noun: str = "File",
) -> None:
    """Hard-check the bytes after ``read()`` (Content-Length can be forged/absent)."""
    limit = max_bytes if max_bytes is not None else max_upload_bytes()
    if len(content) > limit:
        raise HTTPException(status_code=status_code, detail=_detail(limit, noun))
