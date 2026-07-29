"""IM 安全原语 — 借鉴 DeepThink 设计，独立实现。

三层去重架构：
  ProcessingLock   → 覆盖 "到达→写入LRU" 之间的并发 in-flight 窗口 (5min TTL)
  DedupCache       → LRU + TTL 去重，容量满时逐出最旧一条而非全量清空 (30min TTL, 1000 条)
  is_stale         → 重连后丢弃 30 分钟以上旧消息

Usage in api/feishu.py:
  from app.services.im_safety import im_dedup

  async def process_feishu_event(agent_id, body):
      event_id = body.get("header", {}).get("event_id", "")
      if not im_dedup.acquire(event_id):
          return {"code": 0, "msg": "already processing"}
      try:
          if im_dedup.is_duplicate(event_id):
              return {"code": 0, "msg": "already processed"}
          # ... 处理消息 ...
          im_dedup.mark_seen(event_id)
      finally:
          im_dedup.release(event_id)
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# ProcessingLock — 覆盖 in-flight 并发窗口
# ═══════════════════════════════════════════════════════════════════

class ProcessingLock:
    """短 TTL in-memory 锁，防止同一事件被并发处理。

    填补 DedupCache 的空白窗口：事件从到达、经过 is_duplicate 检查、
    到 mark_seen 写入之间的并发窗口。
    """

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._locks: dict[str, float] = {}  # id → expire_at (monotonic seconds)
        self._last_sweep = time.monotonic()

    def acquire(self, key: str) -> bool:
        """获取锁。已持有时返回 False。"""
        now = time.monotonic()
        self._sweep(now)
        if key in self._locks and self._locks[key] > now:
            return False
        self._locks[key] = now + self._ttl
        return True

    def release(self, key: str) -> None:
        """释放锁。"""
        self._locks.pop(key, None)

    def _sweep(self, now: float) -> None:
        """每 60s 清理一次过期锁。"""
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        expired = [k for k, exp in self._locks.items() if exp <= now]
        for k in expired:
            self._locks.pop(k, None)


# ═══════════════════════════════════════════════════════════════════
# DedupCache — LRU + TTL 去重
# ═══════════════════════════════════════════════════════════════════

class DedupCache:
    """LRU 去重缓存，带 TTL 过期。

    - 使用 OrderedDict 实现 LRU：最近访问的条目在末尾
    - 容量满时逐出最旧 1 条（而非全量清空）
    - TTL 过期在每次 is_duplicate 时懒清理
    """

    def __init__(self, ttl_seconds: int = 1800, max_entries: int = 1000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: OrderedDict[str, float] = OrderedDict()  # key → timestamp(monotonic)

    def is_duplicate(self, key: str) -> bool:
        """检查 key 是否已存在且未过期。"""
        self._prune_expired()
        if key in self._data:
            # LRU: 移到末尾
            self._data.move_to_end(key)
            return True
        return False

    def mark_seen(self, key: str) -> None:
        """标记 key 为已处理。
        容量满时逐出最旧 1 条；delete+set 将 key 移到末尾刷新 LRU。
        """
        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self._max:
            self._data.popitem(last=False)  # 逐出最旧
        self._data[key] = time.monotonic()

    def clear(self) -> None:
        """清空缓存（断线重连时丢弃旧状态）。"""
        self._data.clear()

    def size(self) -> int:
        """当前缓存大小（仅用于监控/测试）。"""
        return len(self._data)

    def _prune_expired(self) -> None:
        """懒清理：从头部扫描，遇到第一个未过期条目立即停止。
        OrderedDict 按插入顺序排列，最早的在前。
        """
        now = time.monotonic()
        while self._data:
            _key, ts = next(iter(self._data.items()))
            if now - ts > self._ttl:
                self._data.popitem(last=False)
            else:
                break


# ═══════════════════════════════════════════════════════════════════
# StaleDetector — 重连后回放保护
# ═══════════════════════════════════════════════════════════════════

DEFAULT_STALE_SECONDS: int = 30 * 60  # 30 分钟


def is_stale(create_time_ms: Optional[float], window_seconds: int = DEFAULT_STALE_SECONDS) -> bool:
    """返回 True 当消息创建时间早于 window_seconds 前。

    用于重连后丢弃飞书回放的旧消息，防止重复处理断线期间已过期的消息。
    """
    if create_time_ms is None or not create_time_ms or create_time_ms <= 0:
        return False
    try:
        age = (time.time() * 1000 - create_time_ms) / 1000.0
        return age > window_seconds
    except (OverflowError, ValueError):
        return False


# ═══════════════════════════════════════════════════════════════════
# 组合门面 — 给 feishu.py 用的统一入口
# ═══════════════════════════════════════════════════════════════════

class IMSafetyGate:
    """三层去重门面，替代 api/feishu.py 中的 _processed_events set。"""

    def __init__(self):
        self._lock = ProcessingLock(ttl_seconds=300)
        self._dedup = DedupCache(ttl_seconds=1800, max_entries=1000)

    def acquire(self, key: str) -> bool:
        """获取 in-flight 锁。"""
        return self._lock.acquire(key)

    def release(self, key: str) -> None:
        """释放 in-flight 锁。"""
        self._lock.release(key)

    def is_duplicate(self, key: str) -> bool:
        """检查是否已处理。"""
        return self._dedup.is_duplicate(key)

    def mark_seen(self, key: str) -> None:
        """标记为已处理。"""
        self._dedup.mark_seen(key)

    def size(self) -> int:
        """当前去重缓存大小。"""
        return self._dedup.size()


# 全局实例 — 替代 _processed_events set
im_dedup = IMSafetyGate()
