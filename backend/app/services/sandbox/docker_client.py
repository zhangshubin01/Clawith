"""集中式 Docker 客户端工厂。

所有需要 docker.from_env() 的模块统一通过此模块获取客户端。
优先通过 socat 代理 socket 连接 Docker；如果环境变量
DOCKER_CLIENT_FALLBACK=1 或代理 socket 不可用，回退到 docker.from_env()。
"""

import os
from threading import Lock

import docker
import requests
from loguru import logger

_DOCKER_CLIENT = None
_LOCK = Lock()

# 代理 socket 路径，与 entrypoint.sh 中保持一致
_DOODSOCK_SOCKET = "/var/run/doodsock.sock"

# 回退到 docker.from_env() 前必须设置此环境变量
# 因为 clawith 用户无权访问 /var/run/docker.sock，回退必然失败
_FALLBACK_ENV = "DOCKER_CLIENT_FALLBACK"


def _create_client() -> docker.DockerClient:
    """创建 Docker 客户端实例。

    优先通过 socat 代理 socket 连接（entrypoint 启动的 sidecar），
    该 socket 权限为 clawith:clawith mode=660。

    如果 DOCKER_CLIENT_FALLBACK=1（仅本地开发或 root 运行场景），
    跳过代理 socket 直接调用 docker.from_env()。
    """
    if os.environ.get(_FALLBACK_ENV) == "1":
        logger.info("[DockerClient] DOCKER_CLIENT_FALLBACK=1, using docker.from_env() (dev/root only)")
        return docker.from_env()

    if os.path.exists(_DOODSOCK_SOCKET):
        logger.debug(f"[DockerClient] connecting via proxy socket: {_DOODSOCK_SOCKET}")
        client = docker.DockerClient(base_url=f"unix://{_DOODSOCK_SOCKET}")
        # 验证代理是否存活（os.path.exists 无法区分 socat 死 vs socket 残留）
        try:
            client.ping()
        except (docker.errors.DockerException, requests.exceptions.ConnectionError) as exc:
            logger.warning(
                f"[DockerClient] proxy socket ping failed (socat may be dead): {exc}. "
                f"Set {_FALLBACK_ENV}=1 to bypass proxy."
            )
            client.close()
            # 不静默回退 — clawith 用户无权访问 docker.sock，回退必然 PermissionError
            # 调用方应捕获 DockerException 并报告明确错误
            raise
        return client

    # 代理 socket 不存在：本地开发（root 运行）或 K8s（无 docker.sock）
    return docker.from_env()


def get_docker_client() -> docker.DockerClient:
    """返回全局单例 Docker 客户端。

    线程安全，延迟初始化。如果初始化后发现连接不可用，调用方应通过
    check_docker_health() 或捕获 DockerException 来触发重连。
    """
    global _DOCKER_CLIENT
    if _DOCKER_CLIENT is None:
        with _LOCK:
            if _DOCKER_CLIENT is None:
                _DOCKER_CLIENT = _create_client()
    return _DOCKER_CLIENT


def reset_docker_client() -> None:
    """重置 Docker 客户端单例，关闭旧连接池。

    用于以下场景：
    - socat sidecar 重启后刷新连接
    - 测试隔离（避免跨用例的状态泄漏）
    - DOCKER_CLIENT_FALLBACK 动态切换
    """
    global _DOCKER_CLIENT
    with _LOCK:
        old = _DOCKER_CLIENT
        _DOCKER_CLIENT = None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass  # 关闭连接池中的异常不影响重置


def check_docker_health() -> dict:
    """检查 Docker 连接状态。

    返回结构化健康信息，供 /api/health 端点聚合展示。
    """
    result = {
        "proxy_socket_exists": os.path.exists(_DOODSOCK_SOCKET),
        "client_cached": _DOCKER_CLIENT is not None,
        "status": "unknown",
    }

    # 验证实时连接
    client = _DOCKER_CLIENT or _create_client()
    try:
        client.ping()
        result["status"] = "ok"
    except (docker.errors.DockerException, requests.exceptions.ConnectionError) as exc:
        logger.warning(f"[DockerClient] health check failed: {exc}")
        result["status"] = "degraded"
        result["error"] = str(exc)
        # 可能 socat 已死，主动清除失效连接
        if _DOCKER_CLIENT is client:
            reset_docker_client()

    if "error" not in result:
        if client is not _DOCKER_CLIENT:
            client.close()  # 仅 health check 创建的一次性连接

    return result
