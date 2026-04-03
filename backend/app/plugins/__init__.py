# backend/app/plugins/__init__.py
import importlib
from pathlib import Path

from fastapi import FastAPI
from loguru import logger

_PLUGINS_DIR = Path(__file__).parent


def load_plugins(app: FastAPI) -> None:
    """扫描 plugins/ 目录，加载每个含 plugin.json 的插件。"""
    for item in sorted(_PLUGINS_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("_"):
            continue
        if not (item / "plugin.json").exists():
            continue
        try:
            module = importlib.import_module(f"app.plugins.{item.name}")
            plugin_instance = getattr(module, "plugin", None)
            if plugin_instance is None:
                logger.warning(f"[plugin] {item.name}: 未导出 'plugin' 实例，跳过")
                continue
            plugin_instance.register(app)
            logger.info(f"[plugin] 已加载: {plugin_instance.name} v{plugin_instance.version}")
        except Exception as exc:
            logger.error(f"[plugin] 加载 {item.name} 失败: {exc}")
