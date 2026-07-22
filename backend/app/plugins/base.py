"""Clawith 插件系统基类。

所有 Clawith 插件必须继承 ClawithPlugin 并实现 register(app) 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


class ClawithPlugin(ABC):
    """Clawith 插件抽象基类。

    每个插件在 register(app) 中：
    - 注册 FastAPI 路由
    - 安装工具钩子
    - 初始化后台服务
    """

    name: str = "unnamed"
    version: str = "0.1.0"
    description: str = ""

    @abstractmethod
    def register(self, app: FastAPI) -> None:
        """向 FastAPI app 注册插件路由和钩子。"""
        ...
