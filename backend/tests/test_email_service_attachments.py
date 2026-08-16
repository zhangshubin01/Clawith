"""Security regression tests for send_email attachment path resolution.

Attachment paths are LLM-controlled tool arguments: they must never be able
to read outside the calling agent's storage namespace or its local workspace
root, and must never exfiltrate absolute local files (e.g. /etc/passwd).
"""

from __future__ import annotations

import email as email_lib
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services import email_service

BASE_CONFIG = {
    "email_address": "sender@example.com",
    "auth_code": "secret",
    "email_provider": "custom",
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_ssl": True,
}


def _attached_filenames(msg_string: str) -> list[str]:
    msg = email_lib.message_from_string(msg_string)
    names: list[str] = []
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if disposition.startswith("attachment"):
            names.append(part.get_filename())
    return names


def _noop_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.exists.return_value = False
    storage.is_file.return_value = False
    return storage


@pytest.mark.asyncio
async def test_attachment_absolute_path_is_not_read(tmp_path: Path) -> None:
    """An absolute path (e.g. /etc/passwd) must never reach the local disk read."""
    sent: dict[str, str] = {}
    ws = tmp_path / "agent-a"
    ws.mkdir()

    def fake_send_smtp_email(**kwargs):
        sent["msg_string"] = kwargs["msg_string"]

    with (
        patch("app.services.email_service.send_smtp_email", side_effect=fake_send_smtp_email),
        patch("app.services.storage.get_storage_backend", return_value=_noop_storage()),
    ):
        await email_service.send_email(
            config=BASE_CONFIG,
            to="to@example.com",
            subject="s",
            body="b",
            attachments=["/etc/passwd"],
            workspace_path=ws,
            agent_id=uuid.uuid4(),
        )

    assert _attached_filenames(sent["msg_string"]) == []


@pytest.mark.asyncio
async def test_attachment_cannot_escape_workspace_root(tmp_path: Path) -> None:
    """`..` in an attachment path must not reach a sibling directory on disk."""
    sent: dict[str, str] = {}
    ws = tmp_path / "agent-a"
    ws.mkdir()
    outside = tmp_path / "victim" / "secret.txt"
    outside.parent.mkdir()
    outside.write_bytes(b"victim data")

    def fake_send_smtp_email(**kwargs):
        sent["msg_string"] = kwargs["msg_string"]

    with (
        patch("app.services.email_service.send_smtp_email", side_effect=fake_send_smtp_email),
        patch("app.services.storage.get_storage_backend", return_value=_noop_storage()),
    ):
        await email_service.send_email(
            config=BASE_CONFIG,
            to="to@example.com",
            subject="s",
            body="b",
            attachments=["../victim/secret.txt"],
            workspace_path=ws,
            agent_id=uuid.uuid4(),
        )

    assert _attached_filenames(sent["msg_string"]) == []


@pytest.mark.asyncio
async def test_attachment_storage_keys_stay_within_agent_prefix(tmp_path: Path) -> None:
    """Traversal attempts are rejected before storage is touched; valid paths stay scoped."""
    agent_id = uuid.uuid4()
    requested: list[str] = []
    storage = _noop_storage()

    async def fake_exists(key):
        requested.append(key)
        return False

    async def fake_is_file(key):
        requested.append(key)
        return False

    storage.exists.side_effect = fake_exists
    storage.is_file.side_effect = fake_is_file

    ws = tmp_path / "agent-a"
    ws.mkdir()
    with (
        patch("app.services.email_service.send_smtp_email", side_effect=lambda **kwargs: None),
        patch("app.services.storage.get_storage_backend", return_value=storage),
    ):
        await email_service.send_email(
            config=BASE_CONFIG,
            to="to@example.com",
            subject="s",
            body="b",
            attachments=[
                "../victim-agent/workspace/notes/secret.pdf",
                "a/../../victim/x.pdf",
                "notes/report.txt",
            ],
            workspace_path=ws,
            agent_id=agent_id,
        )

    # Traversal attempts must never reach the storage backend, and the valid
    # path must be queried strictly inside the agent's own prefix.
    assert requested == [f"{agent_id}/notes/report.txt"]


@pytest.mark.asyncio
async def test_normal_attachment_still_attached(tmp_path: Path) -> None:
    """A legitimate workspace-relative attachment keeps working."""
    sent: dict[str, str] = {}
    ws = tmp_path / "agent-a"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "report.txt").write_bytes(b"report body")

    def fake_send_smtp_email(**kwargs):
        sent["msg_string"] = kwargs["msg_string"]

    with (
        patch("app.services.email_service.send_smtp_email", side_effect=fake_send_smtp_email),
        patch("app.services.storage.get_storage_backend", return_value=_noop_storage()),
    ):
        await email_service.send_email(
            config=BASE_CONFIG,
            to="to@example.com",
            subject="s",
            body="b",
            attachments=["notes/report.txt"],
            workspace_path=ws,
            agent_id=uuid.uuid4(),
        )

    assert _attached_filenames(sent["msg_string"]) == ["report.txt"]
