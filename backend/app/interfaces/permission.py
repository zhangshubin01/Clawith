"""Permission request and approval interface for batch pre-write approval."""
from abc import ABC, abstractmethod
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel


class PendingFileWrite(BaseModel):
    """Pending file write permission request."""
    permission_id: str
    session_id: UUID
    ws_key: str
    file_path: str
    old_content: Optional[str]
    new_content: str
    created_at: float  # timestamp


class PendingPermissionRequest(BaseModel):
    """Generic pending permission request."""
    permission_id: str
    session_id: UUID
    ws_key: str
    tool_name: str
    file_path: Optional[str] = None
    old_content: Optional[str] = None
    new_content: Optional[str] = None
    args_summary: Optional[str] = None
    created_at: float  # timestamp


class PermissionResult(BaseModel):
    """Result of a permission decision."""
    permission_id: str
    granted: bool
    processed_at: float


class BatchPermissionApprovalService(ABC):
    """Interface for batch permission approval service.

    This service accumulates permission requests (like file writes)
    and allows user to review all changes before applying them.
    """

    @abstractmethod
    def enqueue(self, request: PendingPermissionRequest) -> None:
        """Enqueue a new permission request for later approval."""
        ...

    @abstractmethod
    def get_pending(self, session_id: UUID) -> List[PendingPermissionRequest]:
        """Get all pending permission requests for a session."""
        ...

    @abstractmethod
    def get_by_id(self, permission_id: str) -> Optional[PendingPermissionRequest]:
        """Get a specific pending permission request by ID."""
        ...

    @abstractmethod
    def process_result(self, result: PermissionResult) -> None:
        """Process a permission decision (grant/deny)."""
        ...

    @abstractmethod
    def wait_for_decision(self, permission_id: str, timeout_seconds: float = 120.0) -> bool:
        """Wait for a decision on a permission request.

        Returns True if granted, False if denied or timeout.
        """
        ...

    @abstractmethod
    def clear_session(self, session_id: UUID) -> None:
        """Clear all pending requests for a session."""
        ...

    @abstractmethod
    def count_pending(self, session_id: UUID) -> int:
        """Count pending requests for a session."""
        ...
