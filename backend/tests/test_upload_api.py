import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from app.api import upload
from app.config import get_settings
from app.models.user import User


@pytest.mark.parametrize("extension", [".pdf", ".docx", ".xlsx", ".xls"])
def test_office_extraction_uses_file_bytes_for_adversarial_filename(
    monkeypatch, tmp_path: Path, extension: str
) -> None:
    """Uploaded names are data, never part of Python source passed to a subprocess."""
    file_path = tmp_path / f"report');__import__('os').system('id');#{extension}"
    file_path.write_bytes(b"not-a-real-pdf")
    captured: dict[str, object] = {}

    def fake_extract(file_bytes: bytes, filename: str) -> str:
        captured["file_bytes"] = file_bytes
        captured["filename"] = filename
        return "safe extracted text"

    monkeypatch.setattr(upload, "extract_document_text", fake_extract)

    assert upload.extract_text(file_path, extension) == "safe extracted text"
    assert captured == {"file_bytes": b"not-a-real-pdf", "filename": file_path.name}


def _upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


def _request(content_length: str | None = None) -> SimpleNamespace:
    """Minimal stand-in for fastapi.Request — the limit helpers only read headers."""
    headers = {} if content_length is None else {"content-length": content_length}
    return SimpleNamespace(headers=headers)


def _sample_user() -> User:
    user = User()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.role = "member"
    return user


@pytest.mark.asyncio
async def test_upload_rejects_invalid_agent_id() -> None:
    """A non-UUID agent_id (e.g. a prefix-escape attempt) is rejected before any write."""
    uf = _upload_file("hello.txt", b"hello")
    with pytest.raises(HTTPException) as exc_info:
        await upload.upload_file(
            file=uf,
            agent_id="../../enterprise_info_victim",
            current_user=_sample_user(),
            db=AsyncMock(),
            request=_request(),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_checks_agent_access_and_scopes_key(tmp_path: Path) -> None:
    """Uploads must pass check_agent_access and land inside the validated agent prefix."""
    uf = _upload_file("hello.txt", b"hello")
    agent_id = uuid.uuid4()
    storage = AsyncMock()
    storage.exists.return_value = False
    saved = tmp_path / "hello.txt"
    saved.write_bytes(b"hello")

    with (
        patch("app.api.upload.check_agent_access", AsyncMock()) as mock_check,
        patch("app.api.upload.get_storage_backend", return_value=storage),
        patch("app.api.upload.ensure_local_path", AsyncMock(return_value=saved)),
    ):
        result = await upload.upload_file(
            file=uf,
            agent_id=str(agent_id),
            current_user=_sample_user(),
            db=AsyncMock(),
            request=_request(),
        )

    mock_check.assert_awaited_once()
    written_key = storage.write_bytes.await_args.args[0]
    assert written_key == f"{agent_id}/workspace/uploads/hello.txt"
    assert result["workspace_path"] == "workspace/uploads/hello.txt"


@pytest.mark.asyncio
async def test_upload_cross_tenant_access_denied_aborts_write() -> None:
    """When check_agent_access denies (cross-tenant), no storage write may happen."""
    uf = _upload_file("hello.txt", b"hello")

    async def deny(*args, **kwargs):
        raise HTTPException(status_code=403, detail="No access to this agent")

    storage = AsyncMock()
    with (
        patch("app.api.upload.check_agent_access", side_effect=deny),
        patch("app.api.upload.get_storage_backend", return_value=storage),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload.upload_file(
                file=uf,
                agent_id=str(uuid.uuid4()),
                current_user=_sample_user(),
                db=AsyncMock(),
                request=_request(),
            )
    assert exc_info.value.status_code == 403
    storage.write_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_fallback_sanitizes_filename(tmp_path: Path, monkeypatch) -> None:
    """The /tmp fallback branch must not let the filename traverse directories."""
    monkeypatch.setattr(upload, "FALLBACK_UPLOAD_DIR", tmp_path)
    uf = _upload_file("../../evil.txt", b"evil")

    result = await upload.upload_file(
        file=uf, agent_id="", current_user=_sample_user(), db=AsyncMock(), request=_request()
    )

    saved = tmp_path / result["saved_filename"]
    assert saved.is_file()
    assert saved.parent == tmp_path
    assert not (tmp_path.parent / "evil.txt").exists()


# ─── Upload size limits ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_rejects_oversize_content_length_before_read(monkeypatch) -> None:
    """Content-Length over MAX_UPLOAD_BYTES → 413 before any byte is read or written."""
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_BYTES", 100)
    uf = _upload_file("big.bin", b"")
    uf.read = AsyncMock(side_effect=AssertionError("read() must not be called after precheck"))
    storage = AsyncMock()
    with (
        patch("app.api.upload.check_agent_access", AsyncMock()),
        patch("app.api.upload.get_storage_backend", return_value=storage),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload.upload_file(
                file=uf,
                agent_id=str(uuid.uuid4()),
                current_user=_sample_user(),
                db=AsyncMock(),
                request=_request(content_length="101"),
            )
    assert exc_info.value.status_code == 413
    uf.read.assert_not_awaited()
    storage.write_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_rejects_oversize_body_with_forged_content_length(monkeypatch) -> None:
    """A forged small Content-Length cannot bypass the post-read hard check."""
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_BYTES", 100)
    uf = _upload_file("big.bin", b"x" * 101)
    storage = AsyncMock()
    with (
        patch("app.api.upload.check_agent_access", AsyncMock()),
        patch("app.api.upload.get_storage_backend", return_value=storage),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload.upload_file(
                file=uf,
                agent_id=str(uuid.uuid4()),
                current_user=_sample_user(),
                db=AsyncMock(),
                request=_request(content_length="10"),
            )
    assert exc_info.value.status_code == 413
    storage.write_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_image_oversize_content_length_rejected_before_read() -> None:
    """Image over 10MB is rejected (400) from the Content-Length header alone."""
    uf = _upload_file("photo.png", b"")
    uf.read = AsyncMock(side_effect=AssertionError("read() must not be called after precheck"))
    with (
        patch("app.api.upload.check_agent_access", AsyncMock()),
        patch("app.api.upload.get_storage_backend", return_value=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload.upload_file(
                file=uf,
                agent_id=str(uuid.uuid4()),
                current_user=_sample_user(),
                db=AsyncMock(),
                request=_request(content_length=str(10 * 1024 * 1024 + 1)),
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Image too large (max 10MB)"


@pytest.mark.asyncio
async def test_upload_image_oversize_body_rejected_after_read() -> None:
    """Image over 10MB without a Content-Length header is still rejected (400)."""
    uf = _upload_file("photo.png", b"x" * (10 * 1024 * 1024 + 1))
    with (
        patch("app.api.upload.check_agent_access", AsyncMock()),
        patch("app.api.upload.get_storage_backend", return_value=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload.upload_file(
                file=uf,
                agent_id=str(uuid.uuid4()),
                current_user=_sample_user(),
                db=AsyncMock(),
                request=_request(),
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Image too large (max 10MB)"
