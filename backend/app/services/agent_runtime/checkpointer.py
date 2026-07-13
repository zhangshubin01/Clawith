"""LangGraph checkpoint wiring without product-state dual writes."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

from app.config import Settings, get_settings


class CheckpointerConfigurationError(ValueError):
    """Checkpoint persistence cannot be configured safely."""


_CHECKPOINT_SCHEMA = "langgraph_checkpoint"


def runtime_thread_config(run_id: uuid.UUID) -> dict[str, dict[str, str]]:
    """Return the one supported checkpoint identity: thread_id equals Run ID."""
    return {"configurable": {"thread_id": str(run_id)}}


def _to_psycopg_url(database_url: str) -> str:
    """Remove SQLAlchemy driver markers before handing a DSN to psycopg."""
    value = database_url.strip()
    if not value:
        raise CheckpointerConfigurationError("Checkpoint database URL must not be blank")

    scheme, separator, remainder = value.partition("://")
    if not separator:
        raise CheckpointerConfigurationError("Checkpoint database URL must be a PostgreSQL URL")
    if scheme == "postgres":
        scheme = "postgresql"
    elif scheme.startswith("postgresql+"):
        scheme = "postgresql"
    elif scheme != "postgresql":
        raise CheckpointerConfigurationError("Checkpoint database URL must use PostgreSQL")
    normalized = f"{scheme}://{remainder}"
    parts = urlsplit(normalized)
    query = parse_qsl(parts.query, keep_blank_values=True)
    existing_options = [value for key, value in query if key == "options"]
    other_query = [(key, value) for key, value in query if key != "options"]
    search_path_option = f"-csearch_path={_CHECKPOINT_SCHEMA}"
    options = " ".join([*existing_options, search_path_option])
    return urlunsplit(parts._replace(query=urlencode([*other_query, ("options", options)])))


def checkpoint_database_url(settings: Settings | None = None) -> str:
    """Resolve the dedicated checkpoint DSN, falling back to the primary database."""
    runtime_settings = settings or get_settings()
    configured = runtime_settings.LANGGRAPH_CHECKPOINT_DATABASE_URL or runtime_settings.DATABASE_URL
    return _to_psycopg_url(configured)


def checkpoint_serializer(
    settings: Settings | None = None,
) -> SerializerProtocol | None:
    """Build the installed LangGraph AES serializer when a key is configured."""
    runtime_settings = settings or get_settings()
    key = runtime_settings.LANGGRAPH_AES_KEY
    if key is None:
        return None

    key_bytes = key.encode("utf-8")
    if len(key_bytes) not in (16, 24, 32):
        raise CheckpointerConfigurationError("LANGGRAPH_AES_KEY must encode to 16, 24, or 32 bytes")
    try:
        return EncryptedSerializer.from_pycryptodome_aes(key=key_bytes)
    except ImportError as exc:
        raise CheckpointerConfigurationError("Checkpoint AES encryption requires pycryptodome") from exc


def create_checkpointer(
    settings: Settings | None = None,
) -> AbstractAsyncContextManager[AsyncPostgresSaver]:
    """Create a lazy saver context; schema setup is an explicit migration concern."""
    manager = AsyncPostgresSaver.from_conn_string(
        checkpoint_database_url(settings),
        serde=checkpoint_serializer(settings),
    )
    return cast(AbstractAsyncContextManager[AsyncPostgresSaver], manager)
