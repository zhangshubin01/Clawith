"""Interface definitions for external services."""

from .filesystem import FileSystemService
from .permission import (
    PendingFileWrite,
    PendingPermissionRequest,
    PermissionResult,
    BatchPermissionApprovalService,
)

__all__ = [
    "FileSystemService",
    "PendingFileWrite",
    "PendingPermissionRequest",
    "PermissionResult",
    "BatchPermissionApprovalService",
]
