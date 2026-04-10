"""Error handling and normalization for ACP."""
from typing import Optional


def normalize_error(err: Exception) -> None:
    """Normalize error codes to match POSIX conventions.
    
    Converts IDE returned error messages to proper OSError with correct errno codes
    so upstream file system logic can handle them correctly.
    """
    error_message = str(err)
    if (
        'Resource not found' in error_message or
        'ENOENT' in error_message or
        'does not exist' in error_message or
        'No such file' in error_message
    ):
        new_err = OSError(error_message)
        new_err.errno = 2  # ENOENT
        raise new_err
    raise err
