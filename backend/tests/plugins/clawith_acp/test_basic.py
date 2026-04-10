"""
Basic smoke tests for the clawith_acp plugin.
These tests verify that the main modules can be imported without errors.
"""
import pytest


def test_import_router():
    """Test importing the router module."""
    from app.plugins.clawith_acp import router
    assert router is not None


def test_import_connection():
    """Test importing the connection module."""
    from app.plugins.clawith_acp import connection
    assert connection is not None


def test_import_file_system_service():
    """Test importing the file_system_service module."""
    from app.plugins.clawith_acp import file_system_service
    assert file_system_service is not None


def test_import_types():
    """Test importing the types module."""
    from app.plugins.clawith_acp import types
    assert types is not None


def test_import_errors():
    """Test importing the errors module."""
    from app.plugins.clawith_acp import errors
    assert errors is not None