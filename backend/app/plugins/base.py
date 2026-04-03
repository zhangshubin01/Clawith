# backend/app/plugins/base.py
from abc import ABC, abstractmethod
from fastapi import FastAPI


class ClawithPlugin(ABC):
    """所有 Clawith 插件的基类。"""
    name: str = ""
    version: str = "1.0.0"
    description: str = ""

    @abstractmethod
    def register(self, app: FastAPI) -> None:
        """向 FastAPI app 注册路由、启动钩子等。"""
        ...
