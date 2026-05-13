"""插件基类 — 定义 ClawithPlugin 抽象接口，所有插件需继承此类并实现 register 方法。"""

# backend/app/plugins/base.py
from abc import ABC, abstractmethod
from typing import ClassVar

from fastapi import FastAPI


class ClawithPlugin(ABC):
    """所有 Clawith 插件的基类。"""
    name: ClassVar[str] = ""
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = ""

    @abstractmethod
    def register(self, app: FastAPI) -> None:
        """向 FastAPI app 注册路由、启动钩子等。"""
        ...
