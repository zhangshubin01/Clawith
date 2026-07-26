"""Android 编译沙箱 Prometheus 指标。

记录构建总数、耗时、并发数，支持 Grafana 可视化。
prometheus_client 为可选依赖，未安装时指标操作为空操作。
"""

from loguru import logger

try:
    from prometheus_client import Counter, Gauge, Histogram

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False


if _HAS_PROMETHEUS:
    build_total = Counter(
        "clawith_android_build_total",
        "Android 构建总数",
        ["status", "gradle_task"],
    )
    build_duration_seconds = Histogram(
        "clawith_android_build_duration_seconds",
        "Android 构建耗时（秒）",
        ["gradle_task"],
        buckets=[60, 120, 300, 600, 900, 1800],
    )
    build_concurrency = Gauge(
        "clawith_android_build_concurrency",
        "当前并发构建数",
    )


def record_build_start():
    """构建开始时记录并发数 +1。"""
    if _HAS_PROMETHEUS:
        build_concurrency.inc()


def record_build_end():
    """构建结束时记录并发数 -1。"""
    if _HAS_PROMETHEUS:
        build_concurrency.dec()


def record_build(task: str, duration_ms: int, success: bool):
    """记录一次构建的指标和结构化日志。"""
    status = "success" if success else "failure"
    if _HAS_PROMETHEUS:
        build_total.labels(status=status, gradle_task=task).inc()
        build_duration_seconds.labels(gradle_task=task).observe(duration_ms / 1000)
    logger.bind(
        tool="android_compile",
        task=task,
        duration_ms=duration_ms,
        success=success,
    ).info("[AndroidBuild] build_complete")
