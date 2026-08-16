"""Unit tests for the shared upload size-limit helpers (all upload endpoints)."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import upload_limits
from app.config import get_settings


def _request(content_length: str | None = None) -> SimpleNamespace:
    headers = {} if content_length is None else {"content-length": content_length}
    return SimpleNamespace(headers=headers)


def test_precheck_rejects_when_content_length_exceeds_limit(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(HTTPException) as exc_info:
        upload_limits.precheck_content_length(_request("101"))
    assert exc_info.value.status_code == 413


def test_precheck_allows_under_limit_absent_and_unparseable() -> None:
    # Under limit, no header (chunked), and an unparseable header all pass;
    # the post-read hard check is the backstop for the last two.
    upload_limits.precheck_content_length(_request("99"))
    upload_limits.precheck_content_length(_request())
    upload_limits.precheck_content_length(_request("not-a-number"))


def test_enforce_content_limit_rejects_forged_content_length(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(HTTPException) as exc_info:
        upload_limits.enforce_content_limit(b"x" * 101)
    assert exc_info.value.status_code == 413


def test_enforce_content_limit_allows_within_limit() -> None:
    upload_limits.enforce_content_limit(b"x" * 100, max_bytes=100)


def test_image_limit_uses_400_and_image_noun() -> None:
    with pytest.raises(HTTPException) as exc_info:
        upload_limits.precheck_content_length(
            _request(str(upload_limits.MAX_IMAGE_BYTES + 1)),
            max_bytes=upload_limits.MAX_IMAGE_BYTES,
            status_code=400,
            noun="Image",
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Image too large (max 10MB)"
