# tests/plugins/test_plugin_loader.py
import pytest
from fastapi import FastAPI
from app.plugins import load_plugins


def test_load_plugins_no_crash_on_empty():
    """插件目录为空时不应抛出异常。"""
    app = FastAPI()
    load_plugins(app)  # 不应 raise


def test_load_plugins_registers_clawith_mcp():
    """clawith_mcp 插件应成功注册路由。"""
    app = FastAPI()
    load_plugins(app)
    routes = [r.path for r in app.routes]
    assert "/mcp" in routes
    assert "/v1/chat/completions" in routes
