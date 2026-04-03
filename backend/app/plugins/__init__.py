# backend/app/plugins/__init__.py
import importlib
import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from loguru import logger

_PLUGINS_DIR = Path(__file__).parent
_loaded_plugins: set[str] = set()  # idempotency guard


def load_plugins(app: FastAPI) -> None:
    """扫描 plugins/ 目录，加载每个含 plugin.json 的插件。"""
    from app.plugins.base import ClawithPlugin

    for item in sorted(_PLUGINS_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("_"):
            continue
        if not (item / "plugin.json").exists():
            continue
        if item.name in _loaded_plugins:
            logger.debug(f"[plugin] {item.name}: 已加载，跳过重复注册")
            continue
        if not (item / "__init__.py").exists():
            logger.warning(f"[plugin] {item.name}: 缺少 __init__.py，跳过")
            continue
        try:
            # Use the real package path when available, otherwise load from file.
            real_pkg = f"app.plugins.{item.name}"
            init_file = item / "__init__.py"
            if real_pkg in sys.modules:
                module = sys.modules[real_pkg]
            else:
                spec = importlib.util.spec_from_file_location(real_pkg, init_file)
                if spec is None or spec.loader is None:
                    logger.warning(f"[plugin] {item.name}: 无法创建模块 spec，跳过")
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[real_pkg] = module
                spec.loader.exec_module(module)
            plugin_instance = getattr(module, "plugin", None)
            if plugin_instance is None:
                logger.warning(f"[plugin] {item.name}: 未导出 'plugin' 实例，跳过")
                continue
            if not isinstance(plugin_instance, ClawithPlugin):
                logger.warning(
                    f"[plugin] {item.name}: 'plugin' 不是 ClawithPlugin 实例 "
                    f"(got {type(plugin_instance).__name__})，跳过"
                )
                continue
            plugin_instance.register(app)
            _loaded_plugins.add(item.name)
            logger.info(f"[plugin] 已加载: {plugin_instance.name} v{plugin_instance.version}")
        except Exception as exc:
            logger.exception(f"[plugin] 加载 {item.name} 失败: {exc}")
