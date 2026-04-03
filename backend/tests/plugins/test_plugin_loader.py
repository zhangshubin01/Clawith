# backend/tests/plugins/test_plugin_loader.py
"""Tests for the plugin loader.

These tests use temporary directories so they never depend on the real plugins/ contents.
Integration tests that verify specific plugins (clawith_mcp) belong in those plugins' own test files.
"""
import importlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.plugins.base import ClawithPlugin


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_valid_plugin(tmp_path: Path, name: str, routes: list[str]) -> Path:
    """Create a minimal valid plugin directory in tmp_path."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
    route_lines = "\n".join(
        f'@_router.get("{r}")\nasync def _r{i}(): return {{}}'
        for i, r in enumerate(routes)
    )
    (plugin_dir / "__init__.py").write_text(f"""\
from fastapi import APIRouter
from app.plugins.base import ClawithPlugin

_router = APIRouter()
{route_lines}

class _Plugin(ClawithPlugin):
    name = "{name}"
    version = "1.0.0"
    def register(self, app):
        app.include_router(_router)

plugin = _Plugin()
""")
    return plugin_dir


def _load_from(tmp_path: Path, app: FastAPI, monkeypatch) -> None:
    """Run load_plugins but point _PLUGINS_DIR at tmp_path."""
    import app.plugins as _mod
    monkeypatch.setattr(_mod, "_PLUGINS_DIR", tmp_path)
    # Also reset the idempotency set so tests don't interfere with each other
    monkeypatch.setattr(_mod, "_loaded_plugins", set())
    _mod.load_plugins(app)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_load_plugins_no_crash_on_empty(tmp_path, monkeypatch):
    """Empty plugins directory must not raise."""
    app = FastAPI()
    _load_from(tmp_path, app, monkeypatch)
    # No routes beyond FastAPI's built-in ones
    custom = [r for r in app.routes if hasattr(r, "path") and r.path not in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")]
    assert custom == []


def test_load_plugins_registers_routes(tmp_path, monkeypatch):
    """A valid plugin with plugin.json and __init__.py registers its routes."""
    _make_valid_plugin(tmp_path, "my_plugin", ["/hello", "/world"])
    app = FastAPI()
    _load_from(tmp_path, app, monkeypatch)
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/hello" in paths
    assert "/world" in paths


def test_load_plugins_skips_missing_init(tmp_path, monkeypatch):
    """Plugin dir with plugin.json but no __init__.py must be skipped (no crash)."""
    broken = tmp_path / "broken_plugin"
    broken.mkdir()
    (broken / "plugin.json").write_text("{}")
    # No __init__.py

    app = FastAPI()
    _load_from(tmp_path, app, monkeypatch)  # must not raise


def test_load_plugins_error_isolation(tmp_path, monkeypatch):
    """A broken plugin must not prevent subsequent plugins from loading."""
    # First plugin: broken (raises during register)
    bad_dir = tmp_path / "aaa_bad"
    bad_dir.mkdir()
    (bad_dir / "plugin.json").write_text("{}")
    (bad_dir / "__init__.py").write_text("""\
from app.plugins.base import ClawithPlugin

class _Bad(ClawithPlugin):
    name = "bad"
    def register(self, app):
        raise RuntimeError("intentional failure")

plugin = _Bad()
""")
    # Second plugin: good
    _make_valid_plugin(tmp_path, "zzz_good", ["/ok"])

    app = FastAPI()
    _load_from(tmp_path, app, monkeypatch)  # must not raise

    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/ok" in paths, "good plugin must load even if earlier plugin failed"


def test_load_plugins_idempotent(tmp_path, monkeypatch):
    """Calling load_plugins twice must not register routes twice."""
    _make_valid_plugin(tmp_path, "once_plugin", ["/unique"])

    import app.plugins as _mod
    monkeypatch.setattr(_mod, "_PLUGINS_DIR", tmp_path)
    monkeypatch.setattr(_mod, "_loaded_plugins", set())

    app = FastAPI()
    _mod.load_plugins(app)
    _mod.load_plugins(app)  # second call — must be a no-op

    count = sum(1 for r in app.routes if hasattr(r, "path") and r.path == "/unique")
    assert count == 1, f"Route /unique registered {count} times, expected 1"


def test_load_plugins_skips_non_plugin_instance(tmp_path, monkeypatch):
    """Plugin that exports a non-ClawithPlugin 'plugin' attribute must be skipped."""
    bad_dir = tmp_path / "wrong_export"
    bad_dir.mkdir()
    (bad_dir / "plugin.json").write_text("{}")
    (bad_dir / "__init__.py").write_text('plugin = "i_am_not_a_plugin"\n')

    app = FastAPI()
    _load_from(tmp_path, app, monkeypatch)  # must not raise, must not register anything
