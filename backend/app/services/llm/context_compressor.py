"""上下文压缩管道 — 类型感知压缩 + CTX-GUARD + 断路器 + 会话级去重。

从 caller.py（git e43694d9）按符号边界提取，避免手写重写。
测试规格见 tests/test_context_compress.py（68 用例）。
caller.py 通过重导出保持 `from app.services.llm.caller import ...` 兼容。

分层职责：
- Layer 1（轮次）：_ctx_compress / _multi_role_compress，call_llm 每轮按 token 预算触发
- 类型感知：_detect → _json/_search/_log/_code/_text
- 会话隔离：ContextCompressor（缓存/断路器/跳过统计按会话）
"""

from __future__ import annotations

import json
import os
import re
import time
import hashlib
import threading
from collections import OrderedDict

from loguru import logger

# F1: 条件导入 tiktoken — 未安装时自动降级到 chars//3
try:
    import tiktoken
    HAS_TIKTOKEN = True
except ImportError:
    HAS_TIKTOKEN = False
    tiktoken = None  # type: ignore[assignment]



# ═══════════════════════════════════════════════════════════════════════════════
# 孤儿 tool 链修复（截断/压缩后保证 assistant.tool_calls 与 tool 消息配对）
# ═══════════════════════════════════════════════════════════════════════════════
def _repair_truncated_messages(messages: list) -> list:
    """修复截断产生的孤儿 tool_calls: 删除缺少 tool_result 的 assistant(tool_calls)。

    CTX-GUARD 截断 `api_messages[:_half] + api_messages[-_half:]` 可能从中间
    切断 assistant(tool_calls) → tool(result) 链。此函数扫描并移除孤立的
    assistant 消息(其 tool_calls 无对应 tool 响应), 防止 LLM API 400 错误。
    """
    # 收集所有 tool 消息的 tool_call_id
    tool_result_ids = {
        m.tool_call_id for m in messages
        if m.role == "tool" and getattr(m, "tool_call_id", None)
    }
    repaired = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            # 过滤掉无对应 tool_result 的 tool_call
            valid_calls = [
                tc for tc in m.tool_calls
                if tc.get("id", "") in tool_result_ids
            ]
            if not valid_calls:
                # 全部孤立 — 丢弃此 assistant 消息
                continue
            if len(valid_calls) != len(m.tool_calls):
                # 部分孤立 — 仅保留有效调用
                m.tool_calls = valid_calls
            repaired.append(m)
        elif m.role == "tool":
            tid = getattr(m, "tool_call_id", None)
            if tid and tid not in _collect_all_tool_call_ids(messages):
                # 孤立的 tool 响应(对应 assistant 已移除) — 丢弃
                continue
            repaired.append(m)
        else:
            repaired.append(m)
    removed = len(messages) - len(repaired)
    if removed:
        orphaned_tool_names = [
            tc.get("function", {}).get("name", "?")
            for m in messages
            if m.role == "assistant" and m.tool_calls
            for tc in m.tool_calls
            if tc.get("id", "") not in tool_result_ids
        ][:10]
        logger.warning(
            f"[CTX-GUARD-REPAIR] removed {removed} orphaned msgs "
            f"tools={orphaned_tool_names}"
        )
    return repaired


def _collect_all_tool_call_ids(messages: list) -> set:
    """收集所有 assistant 消息中的 tool_call id。"""
    ids = set()
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                ids.add(tc.get("id", ""))
    return ids




# ═══════════════════════════════════════════════════════════════════════════════
# 上下文压缩管道 (v4.1 — 类型感知压缩 + 指标修正)
# ═══════════════════════════════════════════════════════════════════════════════
_TOOL_MIN, _JSON_SMALL, _SEARCH_MIN, _LOG_MIN = 512, 20, 30, 40
_TEXT_MIN, _TEXT_MAX = 4096, 8192
_CACHE_MAX, _BREAKER_MAX, _BREAKER_COOLDOWN = 200, 3, 60.0
_CCR_MAX = 500
# CTX-GUARD 绝对阈值 — 对齐 Headroom (200K) + Anthropic Claude SDK (100K)
# 无论模型上下文窗口多大，api_messages 超过此值即触发压缩

def _get_ctx_guard_max_window(model_name: str = "") -> int:
    """按模型自适应上下文保护上限。Claude 模型 100K，其他模型取窗口 60%。

    优先级: 环境变量 CTX_GUARD_MAX_WINDOW > 模型自适应 > 默认 100K
    """
    # 1. 环境变量显式覆盖
    try:
        env_val = os.getenv("CTX_GUARD_MAX_WINDOW")
        if env_val:
            return max(int(env_val), 1)
    except (ValueError, TypeError):
        pass

    # 2. Claude 模型使用 Anthropic 推荐 100K
    if any(kw in model_name.lower() for kw in ("claude", "anthropic")):
        return 100000

    # 3. 其他模型默认 200K (60% of ~333K avg, safe for most models)
    return 200000


def _get_ctx_guard_ratios(model_name: str = "") -> tuple[float, float]:
    """按模型自适应 WARN/COMPRESS 比率。大窗口模型使用更高阈值。"""
    lo = model_name.lower()
    # 1M 窗口模型 (deepseek-v4): 更宽松的阈值
    if "deepseek-v4" in lo or "1m" in lo:
        # 批次 A 2.1：200K guard 窗下 90% 永不触发 Layer1，降至 60%/80%
        return 0.60, 0.80
    # Claude 模型 (200K): 适中阈值
    if any(kw in lo for kw in ("claude", "anthropic")):
        return 0.60, 0.80
    # 默认
    return 0.60, 0.80


# 模块常量: 默认值 (运行时被 call_llm 内动态计算覆盖)
_CTX_GUARD_MAX_WINDOW_DEFAULT = 100000
_CTX_GUARD_WARN_RATIO = 0.60
_CTX_GUARD_COMPRESS_RATIO = 0.80

# ══ 模块级常量 (纯常量，不迁移) ══
_ERROR_KW = frozenset({"error","failed","failure","fatal","critical","panic","exception",
    "traceback","stack trace","segfault","abort","timeout","timed out","crashed","killed",
    "terminated","denied","refused","invalid","unavailable","unreachable","corrupt",
    "corrupted","overflow","deadlock","unauthorized","forbidden","access denied","deprecated"})
CCR_SENTINEL_KEY = "_ccr_dropped"

_GREP_RE = re.compile(r'^(?:\.{0,2}/)?[^\s:]+:\d+:|^\x1b\[[0-9;]*m', re.MULTILINE)
_LOG_RE = re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}|\[\d{4}-\d{2}-\d{2}|\b(ERROR|CRITICAL|FATAL|WARN|WARNING|INFO|DEBUG|TRACE)\b')
_SIG_RE = re.compile(r'^\s*(def |class |fn |function |public |private |protected |export |async def |async fn )')
_STOP_RE = re.compile(r'\b(the|a|an|is|are|was|were|be|been|being|have|has|had|do|does|did|will|would|shall|should|may|might|can|could|to|of|in|for|on|with|at|by|from|as|into|through|during|and|but|or|nor|not|so|yet|both|either|neither|this|that|these|those|it|its|they|them|their)\b', re.IGNORECASE)
_CODE_KW = ("import ","from ","def ","class ","fn ","function ","// ","/*","export ","package ","public ","private ","use ","mod ","struct ","enum ","interface ","impl ")
_IMPORTANCE_RE = re.compile(r'(error|fail|exception|panic|traceback|segfault|abort|timeout|crash|denied|refused|forbidden|deadlock|corrupt|overflow|TODO|FIXME|HACK|BUG|WARNING|DEPRECATED)', re.IGNORECASE)


# ══ F3: ContextCompressor 类 — 会话级上下文压缩器 ══

class ContextCompressor:
    """会话级上下文压缩器。封装压缩缓存 + CCR 存储 + 管道断路器。

    每个 call_llm 调用创建独立实例 → 会话隔离 + 并发安全 + 可测试。
    模块级 _default_compressor 单例提供向后兼容。
    """

    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self._compress_cache: OrderedDict[str, str] = OrderedDict()
        self._ccr_store: OrderedDict[str, str] = OrderedDict()
        self._breaker_failures = 0
        self._breaker_open_until = 0.0
        # F10: 分类型压缩率统计
        self._stats: dict[str, list[float]] = {}
        self._skipped_types: set[str] = set()
        # F4: 会话级文件读取去重表 (content_hash → (round, file_path))。
        # 迁出模块级 _dedup_seen，避免多会话/多租户跨污染 (评审 Blocker #7)。
        self._dedup_seen: dict[str, tuple[int, str]] = {}
        self._list_seen: dict[str, tuple[int, str, int, list[str]]] = {}
        # P1：fold 冷却与 Tier1 noop 高压 escalation（Layer0 budget 收紧）
        self._last_fold_round: int = -100
        self._fold_noop_streak: int = 0
        self.layer0_budget_scale: float = 1.0

    # ── 断路器 ──

    @property
    def breaker_is_open(self) -> bool:
        now = time.monotonic()
        if self._breaker_open_until > 0 and now >= self._breaker_open_until:
            self._breaker_failures = 0
            self._breaker_open_until = 0.0
        return self._breaker_open_until > 0

    def breaker_record_failure(self) -> None:
        self._breaker_failures += 1
        if self._breaker_failures >= _BREAKER_MAX:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN
            logger.warning(f"[CTX-COMPRESS] breaker OPEN — bypass {_BREAKER_COOLDOWN}s")

    def breaker_record_success(self) -> None:
        self._breaker_failures = 0
        self._breaker_open_until = 0.0

    # ── F10: 自适应压缩反馈 ──

    _TYPE_MIN_SIZES = {"json": 20, "search": 30, "log": 40, "code": 512, "text": 4096}
    _TYPE_BASELINES = {"json": 0.50, "search": 0.75, "log": 0.60, "code": 0.40, "text": 0.50}

    def record_compress(self, content_type: str, chars_before: int, chars_after: int) -> None:
        """记录一次压缩效果。仅记录超过类型最小规模的压缩。"""
        min_size = self._TYPE_MIN_SIZES.get(content_type, 0)
        if chars_before < min_size:
            return
        ratio = 1 - (chars_after / max(chars_before, 1))
        window = self._stats.setdefault(content_type, [])
        window.append(ratio)
        if len(window) > 20:  # 滑动窗口
            window.pop(0)
        # 自动恢复: 近 20 次中 ≥3 次 > 基线 → 解除跳过
        if content_type in self._skipped_types:
            baseline = self._TYPE_BASELINES.get(content_type, 0.30)
            successes = sum(1 for r in window[-20:] if r > baseline * 0.5)
            if successes >= 3:
                self._skipped_types.discard(content_type)
                logger.info("[CTX-ADAPT] resumed type={} after recovery", content_type)

    def should_skip_type(self, content_type: str, ctx_pressure: float = 0.0) -> bool:
        """判断是否应跳过该类型的压缩。上下文压力高 (>0.75) 时忽略跳过。"""
        if ctx_pressure > 0.75:
            return False
        if content_type in self._skipped_types:
            return True
        window = self._stats.get(content_type, [])
        if len(window) < 10:
            return False
        recent = window[-10:]
        avg = sum(recent) / len(recent)
        baseline = self._TYPE_BASELINES.get(content_type, 0.30)
        # 平均低于基线的 50% 且全部低于基线 → 跳过
        if avg < baseline * 0.5 and all(r < baseline for r in recent):
            self._skipped_types.add(content_type)
            logger.info("[CTX-ADAPT] skip type={} avg_ratio={:.1%} baseline={:.0%}", content_type, avg, baseline)
            return True
        return False

    # ── 压缩缓存 + 类型路由 + JSON 压缩 ──

    def compress_cached(self, content: str) -> str:
        h = hashlib.sha256(content.encode()).hexdigest()
        if h in self._compress_cache:
            return self._compress_cache[h]
        ct = _detect(content)
        # F10: 自适应跳过
        if self.should_skip_type(ct):
            return content
        before = len(content)
        result = self._dispatch(content, ct)
        if result is not content:
            # F10: 记录压缩统计
            self.record_compress(ct, before, len(result))
            if len(self._compress_cache) >= _CACHE_MAX:
                self._compress_cache.popitem(last=False)
            self._compress_cache[h] = result
        return result

    def _dispatch(self, c: str, t: str) -> str:
        if t == "json":
            return self._json(c)
        if t == "search":
            return _search(c)
        if t == "log":
            return _log(c)
        if t == "code":
            return _code(c)
        if t == "text":
            return _text(c)
        return c

    def _json(self, content: str) -> str:
        try:
            d = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return _trunc(content, 4096)
        if isinstance(d, list):
            n = len(d)
            if n <= _JSON_SMALL:
                return content
            errors = [i for i in d[:min(n, 500)] if isinstance(i, dict)
                      and any(kw in str(i).lower() for kw in ("error", "fail", "exception"))]
            keys = {k for i in d[:min(n, 100)] if isinstance(i, dict) for k in i}
            kept = list(d[:5])
            if errors:
                kept.append({"_pinned_errors": len(errors)})
            kept += list(d[-5:])
            dropped = n - len(kept)
            if dropped > 0:
                h = hashlib.sha256(content.encode()).hexdigest()[:12]
                if len(self._ccr_store) >= _CCR_MAX:
                    self._ccr_store.popitem(last=False)
                self._ccr_store[h] = content
                kept.append({CCR_SENTINEL_KEY: f"ccr:{h} {dropped}_rows {len(content)}B"})
            return json.dumps({"_total": n, "_fields": sorted(keys)[:20], "_sample": kept},
                              ensure_ascii=False)
        if isinstance(d, dict):
            c = {}
            for k, v in d.items():
                if isinstance(v, str) and len(v) > 200:
                    c[k] = v[:200] + "..."
                elif isinstance(v, (list, tuple)) and len(v) > 20:
                    c[k] = f"[{len(v)} items]"
                elif isinstance(v, dict):
                    c[k] = f"{{...{len(v)} keys...}}"
                else:
                    c[k] = v
            return json.dumps(c, ensure_ascii=False)
        return _trunc(content, 4096)


# F3: 模块级单例 — 向后兼容包装器
_default_compressor = ContextCompressor()


def _breaker_is_open() -> bool:
    return _default_compressor.breaker_is_open


def _breaker_record_failure() -> None:
    _default_compressor.breaker_record_failure()


def _breaker_record_success() -> None:
    _default_compressor.breaker_record_success()


def _compress_cached(content: str) -> str:
    return _default_compressor.compress_cached(content)


def _dispatch(c: str, t: str) -> str:
    return _default_compressor._dispatch(c, t)


def _json(content: str) -> str:
    return _default_compressor._json(content)


# ══ F1: 模型级 Tokenizer ══

# 按模型系列编码器缓存 (双重检查锁定, 线程安全)
_EST_ENCODERS_CACHE: dict[str, object] = {}
_EST_ENCODERS_LOCK = threading.Lock()


def _tokenizer_key(model_name: str) -> str:
    """将模型名称映射到 tokenizer 系列键, 用于缓存查找."""
    lo = model_name.lower()
    if any(kw in lo for kw in ("claude", "anthropic")):
        return "claude"  # Anthropic 专有 tokenizer, tiktoken 不兼容 → None
    if "deepseek" in lo:
        return "deepseek"  # cl100k_base 近似, 已知偏差 ~10-30% (DeepSeek 未开源 tokenizer)
    if any(kw in lo for kw in ("gpt-4o", "gpt-4", "o1", "o3")):
        return "o200k_base"
    if any(kw in lo for kw in ("gpt", "qwen")):
        return "cl100k_base"
    return "cl100k_base"  # 默认 fallback


def _get_tokenizer(model_name: str):
    """按模型系列选择 tokenizer, 双重检查锁定 + lazy 加载.

    失败返回 None → 自动回退 chars//3. Claude 系列始终返回 None.
    """
    key = _tokenizer_key(model_name)

    # 第一次检查 (无锁, 快速路径)
    if key in _EST_ENCODERS_CACHE:
        return _EST_ENCODERS_CACHE[key]

    if key == "claude" or not HAS_TIKTOKEN:
        enc = None
    else:
        try:
            enc = tiktoken.get_encoding(key)
        except Exception:
            # 未知编码名 (如 "deepseek" 不在 tiktoken 注册表中) → fallback cl100k_base
            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                enc = None

    # 第二次检查 (加锁, 防止其他线程已写入)
    with _EST_ENCODERS_LOCK:
        if key in _EST_ENCODERS_CACHE:
            return _EST_ENCODERS_CACHE[key]  # 其他线程抢先初始化
        _EST_ENCODERS_CACHE[key] = enc

    return enc


def _img_token_estimate(img_block: dict) -> int:
    """估算 image block token 占用.

    Anthropic 公式: W*H/750. 对于 1024×1024 图像 ≈ 1398 tokens.
    当前使用 800 默认值, 后续可从 source 提取尺寸动态计算.

    兼容两种结构（评审 Blocker #8）:
    - Anthropic 风格: {"type":"image","source":{...}}
    - OpenAI 风格: {"type":"image_url","image_url":{"url":...}}
    两者都视为一张图, 返回固定估算, 避免 CTX-GUARD 低估图像 token 导致爆窗。
    """
    return 800  # TODO: 从 source/image_url 提取宽高用 W*H/750 公式


def _est_tokens(msgs: list, model_name: str = "") -> int:
    """估算上下文 token 数。优先真实 tokenizer，失败回退 chars//3。

    性能优化: 使用 LLMMessage._cached_tokens 避免重复 encode 同一消息。
    首轮计算后，后续轮次仅新增/修改消息需重新编码（通常 1-3 条/轮）。
    """
    encoder = _get_tokenizer(model_name) if model_name else None
    t = 0

    for m in msgs:
        # ── 缓存命中: 直接复用 ──
        cached = getattr(m, '_cached_tokens', None)
        if cached is not None:
            t += cached
            continue

        msg_t = 0
        c = getattr(m, 'content', None)

        if isinstance(c, str):
            msg_t += len(encoder.encode(c)) if encoder else len(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        msg_t += len(encoder.encode(text)) if encoder else len(text)
                    elif block.get("type") in ("image", "image_url"):
                        # Blocker #8: 同时匹配 Anthropic(image) 与 OpenAI(image_url)
                        msg_t += _img_token_estimate(block)

        if tc := getattr(m, 'tool_calls', None):
            s = json.dumps(tc, default=str)
            msg_t += len(encoder.encode(s)) if encoder else (len(s) // 3)

        if dc := getattr(m, 'dynamic_content', None):
            s = str(dc)
            msg_t += len(encoder.encode(s)) if encoder else (len(s) // 3)

        if rc := getattr(m, 'reasoning_content', None):
            s = str(rc)
            msg_t += len(encoder.encode(s)) if encoder else (len(s) // 3)

        # 回退模式: chars//3
        if not encoder:
            msg_t = max(msg_t // 3, 1)

        t += msg_t

        # 写入 per-message 缓存 (仅在 encoder 可用时, 避免 chars//3 污染)
        if encoder:
            try:
                m._cached_tokens = msg_t
            except Exception:
                pass  # dataclass frozen 或 slots 限制时静默跳过

    return max(t, 1)


def _est_tokens_str(s: str, model_name: str = "") -> int:
    """估算单个字符串的 token 数（工具结果压缩触发/never_worse 用）。

    复用 _get_tokenizer 编码器缓存；无 encoder（Claude 系列/未装 tiktoken）
    回退 chars//3，与 _est_tokens 的降级口径一致。
    """
    if not s:
        return 0
    encoder = _get_tokenizer(model_name) if model_name else None
    if encoder is not None:
        try:
            return len(encoder.encode(s))
        except Exception:
            pass
    return max(len(s) // 3, 1)


def _trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... ({len(s) - n} more chars)"


def _work_paths(msgs: list) -> set[str]:
    ps: set[str] = set()
    n = 0
    for m in reversed(msgs):
        if getattr(m, 'role', None) != "assistant":
            continue
        for tc in (getattr(m, 'tool_calls', None) or []):
            a = tc.get("function", {}).get("arguments", "{}")
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except json.JSONDecodeError:
                    continue
            for k in ("path", "file_path", "filePath", "file"):
                if v := a.get(k):
                    ps.add(str(v))
            n += 1
            if n >= 20:
                return ps
    return ps


def _isolate(msg, wp: set[str]) -> bool:
    if getattr(msg, 'role', None) == "system":
        return True
    c = getattr(msg, 'content', None)
    if isinstance(c, str):
        lo = c.lower()
        if any(kw in lo for kw in _ERROR_KW):
            return True
        if wp and any(p in lo for p in wp):
            return True
    return False


def _detect(content: str) -> str:
    s = content.strip()
    if not s:
        return "empty"
    if s[0] in ("{", "["):
        try:
            json.loads(s)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    h = s.split("\n")[:10]
    if sum(1 for L in h if _GREP_RE.search(L)) >= max(len(h) * 0.5, 2):
        return "search"
    ls = s.split("\n")[:30]
    if sum(1 for L in ls if _LOG_RE.search(L)) >= max(len(ls) * 0.3, 3):
        return "log"
    cs = s.split("\n")[:20]
    if sum(1 for L in cs if L.lstrip().startswith(_CODE_KW)) >= 3:
        return "code"
    return "text"


def _search(content: str) -> str:
    lines = content.strip().split("\n")
    if len(lines) <= _SEARCH_MIN:
        return content
    fc, important, last_idx = {}, [], {}
    for i, line in enumerate(lines):
        m = re.match(r'^(?:\.{0,2}/)?([^\s:]+(?:\.[a-zA-Z]+)?):\d+:', line)
        if m:
            fn = m.group(1)
            fc[fn] = fc.get(fn, 0) + 1
            last_idx[fn] = i
        if _IMPORTANCE_RE.search(line):
            important.append(line)
    p = [f"[grep: {len(lines)} matches / {len(fc)} files]",
         "Top: " + ", ".join(
             f"{f}({c})" for f, c in sorted(
                 fc.items(), key=lambda x: (-x[1], -last_idx.get(x[0], 0))
             )[:10]
         )]
    if important:
        p.append(f"\n--- Highlights ({len(important)}) ---")
        p.extend(important[:20])
    p.append("\n--- Head ---")
    p.extend(lines[:5])
    p.append("\n--- Tail ---")
    p.extend(lines[-5:])
    return "\n".join(p)


def _log(content: str) -> str:
    lines = content.strip().split("\n")
    if len(lines) <= _LOG_MIN:
        return content
    im, it = [], False
    for line in lines:
        st = line.lstrip()
        is_t = (len(st) < len(line) or "Traceback" in line
                or st.startswith('File "') or re.match(r'^\s+at\s', line))
        if is_t:
            if not it:
                it = True
            im.append(line)
            continue
        it = False
        if re.search(r'\b(ERROR|CRITICAL|FATAL|WARN|WARNING)\b', line):
            im.append(line)
    r = list(lines[:20])
    r.append(f"\n... ({max(0, len(lines) - 40)} lines omitted) ...\n")
    if im:
        r.append(f"--- Alerts ({len(im)}) ---")
        r.extend(im[:30])
    r.append("--- Last 20 ---")
    r.extend(lines[-20:])
    return "\n".join(r)


def _code(content: str) -> str:
    lines = content.split("\n")
    r, ib, bi = [], False, 0
    for line in lines:
        s = line.strip()
        if not s:
            r.append("")
            continue
        if s.startswith(("import ", "from ", "use ", "require ", "#include", "using ")):
            r.append(line)
            continue
        if _SIG_RE.match(s):
            r.append(line)
            ib = True
            bi = len(line) - len(line.lstrip())
            continue
        if ib:
            cur = len(line) - len(line.lstrip()) if s else 0
            if cur <= bi and s:
                ib = False
                r.append(line)
            elif s.startswith(("return ", "raise ", "yield ", "throw ")):
                r.append(line)
        else:
            r.append(line)
    c = "\n".join(r)
    if len(c) > 4096:
        cl = c.split("\n")
        c = "\n".join(
            cl[:50] + [f"\n... ({max(0, len(cl) - 100)} lines omitted) ...\n"] + cl[-50:]
        )
    return c


def _text(content: str) -> str:
    if len(content) <= _TEXT_MIN:
        return content
    lines = content.strip().split("\n")
    if len(lines) <= 20:
        return _trunc(content, _TEXT_MAX)
    sc = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        v = len(s) + sum(1 for c in s if c.isupper()) * 2
        v += sum(1 for c in s if c in "{}[]()<>|&^%$#@!:=;")
        v -= len(_STOP_RE.findall(s)) * 5
        sc.append((i, max(v, 1), line))
    if not sc:
        return content
    sc.sort(key=lambda x: x[1], reverse=True)
    k = max(int(len(sc) * 0.5), 30)
    kp = sorted(sc[:k], key=lambda x: x[0])
    return _trunc("\n".join(ln for _, _, ln in kp), _TEXT_MAX)


def _ctx_compress(api_messages: list, ctx_window: int, model_name: str = "") -> list:
    """类型感知上下文压缩。model_name 用于精确 token 估算，空字符串回退 chars//3。"""
    from app.services.llm.client import LLMMessage

    if _breaker_is_open() or _est_tokens(api_messages, model_name) <= ctx_window * 0.80:
        return api_messages

    wp = _work_paths(api_messages)
    before = _est_tokens(api_messages, model_name)
    compressed = 0
    fallback = 0
    result = []

    try:
        for msg in api_messages:
            if _isolate(msg, wp):
                result.append(msg)
                continue
            role = getattr(msg, 'role', None)
            content = getattr(msg, 'content', None)
            # retrieve_context 取回的原文必须 verbatim，Layer 1 不得再压
            if role == "tool" and isinstance(content, str) and len(content) > _TOOL_MIN \
                    and "<!-- ccr:retrieved -->" not in content:
                try:
                    nc = _compress_cached(content)
                    if content.strip() and isinstance(nc, str) and not nc.strip():
                        nc = content
                        logger.warning("[CTX-COMPRESS] empty output prevented")
                    if nc is not content:
                        compressed += 1
                        msg._cached_tokens = None  # F1: 内容变更 → 失效缓存
                    result.append(LLMMessage(
                        role="tool", content=nc,
                        tool_call_id=getattr(msg, 'tool_call_id', None),
                    ))
                except Exception:
                    fallback += 1
                    result.append(msg)
            else:
                result.append(msg)

        after = _est_tokens(result, model_name)
        if compressed or fallback:
            logger.info(
                f"[CTX-COMPRESS] {compressed} ok {fallback} fb "
                f"tokens: {before}→{after} "
                f"cache={len(_default_compressor._compress_cache)} ccr={len(_default_compressor._ccr_store)}"
            )
        if fallback == 0:
            _breaker_record_success()
        else:
            _breaker_record_failure()

        return _repair_truncated_messages(result)
    except Exception:
        _breaker_record_failure()
        logger.exception("[CTX-COMPRESS] pipeline failed, returning original")
        return api_messages



def _tool_call_name_map(api_messages: list) -> dict[str, str]:
    """从 assistant tool_calls 建立 call_id → tool_name，供 Layer1 P3 走类型路由。"""
    out: dict[str, str] = {}
    for msg in api_messages:
        if getattr(msg, "role", None) != "assistant":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if not isinstance(tc, dict):
                continue
            tid = (tc.get("id") or "").strip()
            fn = tc.get("function") or {}
            name = (fn.get("name") or "").strip()
            if tid and name:
                out[tid] = name
    return out


def _layer1_compress_tool(
    content: str,
    tool_name: str,
    *,
    model_name: str,
    ctx_window: int,
    session_pressure: float = 0.0,
) -> str:
    """Layer1 P3：content_router 类型感知压缩；Tier1 exclude / 已标记 / 需 CCR 有损则保原文。"""
    from app.services.llm.compression_config import is_tier1_strict
    from app.services.llm.compression_result import requires_ccr
    from app.services.llm.content_router import compress_one_result, is_retrieved
    from app.services.llm.tool_trim import _tool_token_budget

    if is_tier1_strict(tool_name):
        return content
    if "<!-- ctx:trimmed -->" in content or "<!-- ccr:" in content or is_retrieved(content):
        return content

    budget = _tool_token_budget(tool_name, ctx_window)
    pressure = budget / max(ctx_window, 1)
    comp = compress_one_result(
        content,
        tool_name=tool_name,
        budget_tokens=budget,
        model_name=model_name,
        path="layer1",
        ctx_window=ctx_window,
        pressure=pressure,
        session_pressure=session_pressure,
    )
    if not comp.changed:
        return content
    if requires_ccr(comp):
        logger.info(
            "[CTX-CCR] gate skip layer1 tool={} strategy={} reason=requires_ccr_sync",
            tool_name or "unknown",
            comp.strategy,
        )
        return content
    return comp.content


# ══ F2: 多角色分级压缩 ══

def _multi_role_compress(
    api_messages: list,
    ctx_window: int,
    model_name: str = "",
    round_i: int = 0,
    session_id: str = "",
    compressor: "ContextCompressor | None" = None,
    compress_ratio: float = 0.80,
    protect_prefix_count: int = 0,
) -> list:
    """in-loop Layer1 emergency 压缩：仅 live 尾部（protect 之后）可原地改写。

    protect_prefix_count: max(live_start, effective_frozen) — 下标 < 此值一律跳过，
    禁止改写 system dynamic_content，保 prefix cache 字节不变。

    策略:
      P0 (保护)  system     → 仅合并 dynamic_content 重复 reminder
      P1 (摘要)  assistant → 无 tool_calls 纯文本 → _text() 有损, 含错误关键词则 ISOLATE
      P2 (降级)  user      → >2000 字符 → head/tail 保留, 含错误关键词 ISOLATE, 单行→_trunc
      P3 (压缩)  tool      → content_router + exclude/pinning/CCR gate skip

    compressor: 会话级压缩器实例。None 时回退模块级 _default_compressor 单例。
    """
    from app.services.llm.client import LLMMessage

    if protect_prefix_count >= len(api_messages):
        logger.debug("[CTX-MULTI] skip protect>={} msgs={}", protect_prefix_count, len(api_messages))
        return api_messages

    c = compressor or _default_compressor  # F3: 优先会话级，回退模块级单例
    tc_names = _tool_call_name_map(api_messages)
    session_pressure = _est_tokens(api_messages, model_name) / max(ctx_window, 1)
    if c.breaker_is_open or _est_tokens(api_messages, model_name) <= ctx_window * compress_ratio:
        return api_messages

    est_before = _est_tokens(api_messages, model_name)
    wp = _work_paths(api_messages)
    merged_reminders: dict[str, str] = {}
    compressed = 0
    fallback = 0

    for i, msg in enumerate(api_messages):
        # Wave6 zone guard：frozen 前缀（含首条 system）一律不动，保 prefix cache 字节
        if i < protect_prefix_count:
            continue
        role = getattr(msg, 'role', None)
        content = getattr(msg, 'content', None)
        dc = getattr(msg, 'dynamic_content', None)

        # ── P0: system — 收集去重 dynamic_content ──
        if role == "system":
            if dc and isinstance(dc, str):
                merged_reminders[hashlib.md5(dc.encode()).hexdigest()] = dc
            continue

        # ── ISOLATE 守卫: error / 工作路径 ──
        if _isolate(msg, wp):
            continue

        # ── 非字符串内容跳过 ──
        if not isinstance(content, str) or not content:
            continue

        # ── P1: assistant 纯文本 — Stage4 禁止 _text 删行（A10 验收）──
        if role == "assistant" and not getattr(msg, 'tool_calls', None):
            continue

        # ── P2: user 长消息 → 降级压缩 ──
        if role == "user":
            if len(content) > 2000:
                # 含错误关键词 → ISOLATE 保护
                if any(kw in content.lower() for kw in _ERROR_KW):
                    continue
                try:
                    if "\n" not in content:
                        compressed_content = _trunc(content, 2000)
                    else:
                        lines = content.split("\n")
                        if len(lines) <= 20:
                            compressed_content = _trunc(content, 2000)
                        else:
                            head = "\n".join(lines[:10])
                            tail = "\n".join(lines[-10:])
                            omitted = len(content) - len(head) - len(tail)
                            compressed_content = (
                                head + f"\n... [中间 {omitted} 字符已省略] ...\n" + tail
                            )
                    api_messages[i] = LLMMessage(role="user", content=compressed_content)
                    api_messages[i]._cached_tokens = None
                    compressed += 1
                except Exception:
                    fallback += 1
            continue

        # ── P3: tool → content_router（对齐 Tier1 exclude + 禁 _text + CCR gate skip）──
        if role == "tool" and len(content) > _TOOL_MIN:
            tool_name = tc_names.get(getattr(msg, "tool_call_id", None) or "", "")
            try:
                nc = _layer1_compress_tool(
                    content,
                    tool_name,
                    model_name=model_name,
                    ctx_window=ctx_window,
                    session_pressure=session_pressure,
                )
                if content.strip() and isinstance(nc, str) and not nc.strip():
                    nc = content
                    logger.warning("[CTX-COMPRESS] empty output prevented")
                if nc is not content:
                    api_messages[i] = LLMMessage(
                        role="tool", content=nc,
                        tool_call_id=getattr(msg, 'tool_call_id', None),
                    )
                    api_messages[i]._cached_tokens = None
                    compressed += 1
            except Exception:
                fallback += 1

    # ── 将去重后的 dynamic_content 写入首条 system ──
    if merged_reminders:
        for m in api_messages:
            if getattr(m, 'role', None) == "system":
                merged = "\n".join(merged_reminders.values())
                m.dynamic_content = merged
                m._cached_tokens = None
                break

    est_after = _est_tokens(api_messages, model_name)
    if compressed or fallback:
        logger.info(
            f"[CTX-MULTI] {compressed} ok {fallback} fb "
            f"tokens: {est_before}→{est_after} "
            f"({(1 - est_after / max(est_before, 1)) * 100:.0f}%) "
            f"round={round_i} session={session_id[:8]}"
        )

    return _repair_truncated_messages(api_messages)


# ══ F4: 跨轮次文件读取哈希去重 ══

# 会话级内容去重追踪: content_hash → (first_round, file_path)
_dedup_seen: dict[str, tuple[int, str]] = {}


def _dedup_file_tool_results(
    api_messages: list,
    round_i: int = 0,
    dedup_store: dict[str, tuple[int, str]] | None = None,
) -> list:
    """每轮工具结果追加后, 替换重复的 read_file 结果为短引用.

    使用 content sha256 作去重键 (非 file_path), 确保 write_file 修改文件后
    后续 read_file 不被错误去重。O(n) 单遍扫描 + 单遍替换, 不删除消息。

    dedup_store: 会话级去重表 (由 ContextCompressor 持有)。为 None 时回退到
        模块级 _dedup_seen — 仅为向后兼容 (测试/无会话调用), 生产路径应显式传入
        会话表, 避免多会话/多租户跨污染 (评审 Blocker #7)。
    """
    if not api_messages:
        return api_messages

    _seen = dedup_store if dedup_store is not None else _dedup_seen

    # 第一遍: 建立 tool_call_id → file_path 映射 (仅本轮的 assistant 消息)
    _tc_file_map: dict[str, str] = {}
    for msg in api_messages:
        if getattr(msg, 'role', None) != "assistant":
            continue
        for tc in getattr(msg, 'tool_calls', None) or []:
            fn = tc.get("function", {})
            if fn.get("name") != "read_file":
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            fp = args.get("file_path") or args.get("filePath", "")
            if fp:
                _tc_file_map[tc.get("id", "")] = fp

    if not _tc_file_map:
        return api_messages

    # 第二遍: 对 read_file 的 tool 结果做 content hash 去重
    for i, msg in enumerate(api_messages):
        if getattr(msg, 'role', None) != "tool":
            continue
        tc_id = getattr(msg, 'tool_call_id', None)
        if tc_id not in _tc_file_map:
            continue
        content = getattr(msg, 'content', None)
        if not isinstance(content, str) or len(content) <= 1000:
            continue

        fp = _tc_file_map[tc_id]
        ch = hashlib.sha256(content.encode()).hexdigest()[:16]

        if ch in _seen:
            first_round, _first_fp = _seen[ch]
            api_messages[i] = type(msg)(
                role="tool", tool_call_id=tc_id,
                content=(
                    f"[DUPLICATE-READ] 文件 \"{fp}\" — 内容与 Round {first_round} 完全一致, 此处省略."
                ),
            )
            api_messages[i]._cached_tokens = None
        else:
            _seen[ch] = (round_i, fp)

    # LRU 淘汰: 保留最近 500 个 hash
    if len(_seen) > 500:
        oldest = next(iter(_seen))
        del _seen[oldest]

    return api_messages


def _dedup_list_tool_results(
    api_messages: list,
    round_i: int = 0,
    list_store: dict[str, tuple[int, str, int, list[str]]] | None = None,
) -> list:
    """相邻轮 list_files 结果去重，替换为可解析摘要。"""
    if not api_messages or list_store is None:
        return api_messages

    _tc_map: dict[str, tuple[str, dict]] = {}
    for msg in api_messages:
        if getattr(msg, "role", None) != "assistant":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            fn = tc.get("function", {})
            if fn.get("name") != "list_files":
                continue
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            fp = args.get("path") or args.get("filePath", "")
            if fp:
                _tc_map[tc.get("id", "")] = (fp, args)

    if not _tc_map:
        return api_messages

    from app.plugins.clawith_acp.list_dedup import normalize_list_args, summarize_listing_for_dedup

    for i, msg in enumerate(api_messages):
        if getattr(msg, "role", None) != "tool":
            continue
        tc_id = getattr(msg, "tool_call_id", None)
        if tc_id not in _tc_map:
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, str) or len(content) < 8:
            continue
        fp, args = _tc_map[tc_id]
        depth, limit = normalize_list_args(args)
        key = f"{fp}:{depth}:{limit}"
        if key in list_store:
            first_round, first_path, count, names = list_store[key]
            if round_i - first_round > 1:
                continue
            api_messages[i] = type(msg)(
                role="tool",
                tool_call_id=tc_id,
                content=(
                    f'[DUPLICATE-LIST] 目录 "{first_path}" depth={args.get("depth", 3)} '
                    f"与 Round {first_round} 相同；共 {count} 项，示例: {', '.join(names[:5])}"
                ),
            )
            api_messages[i]._cached_tokens = None
        else:
            count, names = summarize_listing_for_dedup(content)
            list_store[key] = (round_i, fp, count, names)

    if len(list_store) > 500:
        oldest = next(iter(list_store))
        del list_store[oldest]

    return api_messages
