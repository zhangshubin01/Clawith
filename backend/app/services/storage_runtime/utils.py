"""Storage path helpers."""


class InvalidStorageKeyError(ValueError):
    """Storage key contains path-traversal semantics."""


def normalize_storage_key(key: str) -> str:
    """Normalize a storage key and reject traversal semantics.

    Unlike workspace-path normalization, storage keys must never silently
    resolve ``..`` segments: a storage key is the physical addressing of
    files across agent/tenant namespaces, so traversal there is an attack
    (P0-2) rather than a convenience. Raise instead of popping.
    """
    clean = (key or "").replace("\\", "/").strip().lstrip("/")
    parts: list[str] = []
    for part in clean.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InvalidStorageKeyError(f"Path traversal not allowed in storage key: {key!r}")
        parts.append(part)
    return "/".join(parts)


def join_storage_key(*parts: str) -> str:
    """Join segments into a storage key, strict-normalizing each segment.

    Each segment is normalized independently so a ``..`` inside one segment
    can never consume a prefix contributed by another segment.
    """
    normalized = [normalize_storage_key(str(part)) for part in parts]
    return "/".join(part for part in normalized if part)


def agent_storage_prefix(agent_id: str) -> str:
    return normalize_storage_key(agent_id)


def tenant_storage_prefix(tenant_id: str) -> str:
    return normalize_storage_key(f"enterprise_info_{tenant_id}")
