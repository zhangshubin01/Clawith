"""上下文压缩策略配置真源。

本模块只放确定性策略：工具排除名单、阈值和小型 helper。这样 ACP、
Layer0、Layer1 后续不会各自维护一份漂移的名单。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re


# Tier1：ground truth — 默认 verbatim；会话高压且超 budget 时 Layer0 可走 CCR 有损（批次 A 2.1）。
TIER1_STRICT_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_document",
    "jina_read",
    "read_webpage",
    "write_file",
    "write_document",
    "edit_file",
    "retrieve_context",
})

# Tier2：导航/检索 — 禁止有损 _text，允许 lossless compact；超 budget 可走 list/search 路由。
TIER2_LOSSLESS_TOOLS: frozenset[str] = frozenset({
    "list_files",
    "list_dir",
    "find_files",
    "search_files",
    "search_file",
    "search_codebase",
    "search_symbol",
    "feishu_doc_search",
})

DEFAULT_EXCLUDE_TOOLS: frozenset[str] = TIER1_STRICT_TOOLS | TIER2_LOSSLESS_TOOLS

# Tier1 在会话 token 占比超过此阈值且结果超 budget 时，允许 Layer0 有损+CCR。
TIER1_SESSION_PRESSURE_THRESHOLD = 0.55

READ_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "read_document", "jina_read", "read_webpage",
})

MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "write_file", "write_document", "edit_file", "move_file", "delete_file",
})

LOSSLESS_MIN_SAVINGS = 0.15
FIRST_FRACTION = 0.30
LAST_FRACTION = 0.15
VARIANCE_THRESHOLD = 2.0
MAX_ITEMS_AFTER_CRUSH = 15
MIN_SIZE_BYTES = 512
LAYER1_PROTECT_ROUNDS = 10
PROTECT_ERROR_CHARS = 8192
CAP_ERRORS = 20
CAP_WARNINGS = 10
CAP_LIST = 20
CAP_INVENTORY = 50

JSON_PROTECT_KEYS: frozenset[str] = frozenset({
    "error", "errors", "message", "status", "code", "id", "name", "path", "file_path", "line", "column",
})

MUST_KEEP_RE = re.compile(
    r"(error|fail|exception|panic|traceback|segfault|abort|timeout|crash|"
    r"denied|refused|forbidden|deadlock|corrupt|overflow|TODO|FIXME|HACK|BUG|"
    r"WARNING|DEPRECATED)",
    re.IGNORECASE,
)


@dataclass
class ReadLifecycleConfig:
    enabled: bool = True
    compress_stale: bool = True
    compress_superseded: bool = True
    min_size_bytes: int = MIN_SIZE_BYTES


def _settings_min_ratio_relaxed() -> float:
    try:
        from app.config import get_settings
        return float(getattr(get_settings(), "CTX_MIN_RATIO_RELAXED", 0.85))
    except Exception:
        return 0.85


def _settings_min_ratio_aggressive() -> float:
    try:
        from app.config import get_settings
        return float(getattr(get_settings(), "CTX_MIN_RATIO_AGGRESSIVE", 0.65))
    except Exception:
        return 0.65


def reduced(original_len: int, compressed_len: int) -> float:
    if original_len <= 0:
        return 1.0
    return compressed_len / original_len


def adaptive_min_ratio(pressure: float) -> float:
    relaxed = _settings_min_ratio_relaxed()
    aggressive = _settings_min_ratio_aggressive()
    if pressure <= 0.5:
        return relaxed
    if pressure >= 0.9:
        return aggressive
    t = (pressure - 0.5) / 0.4
    return relaxed - t * (relaxed - aggressive)




@lru_cache(maxsize=32)
def _normalized_exclude_tools(extra_csv: str = "") -> frozenset[str]:
    extra = {item.strip() for item in (extra_csv or "").split(",") if item.strip()}
    return frozenset(tool.lower() for tool in (DEFAULT_EXCLUDE_TOOLS | extra))


def exclude_tier(tool_name: str) -> int:
    """headroom 式 exclude 分层：0=可压，1=Tier1 strict，2=Tier2 lossless-only。"""
    name = (tool_name or "").lower()
    try:
        from app.config import get_settings

        extra_csv = getattr(get_settings(), "CTX_EXCLUDE_TOOLS", "")
    except Exception:
        extra_csv = ""
    extra = {item.strip().lower() for item in (extra_csv or "").split(",") if item.strip()}
    if name in TIER1_STRICT_TOOLS:
        return 1
    if name in TIER2_LOSSLESS_TOOLS or name in extra:
        return 2
    return 0


def is_tier1_strict(tool_name: str) -> bool:
    """Tier1：读/写/改/retrieve — Layer1 P3 与 Layer2 offload 仍保 verbatim。"""
    return exclude_tier(tool_name) == 1


def is_tier2_lossless_only(tool_name: str) -> bool:
    """Tier2：list/find/search 导航 — 仅 lossless，超 budget 可走类型路由。"""
    return exclude_tier(tool_name) == 2


def is_tool_excluded(tool_name: str) -> bool:
    """Tier1+Tier2 均视为 exclude 保护（向后兼容）。"""
    return exclude_tier(tool_name) >= 1


def tier1_session_pressure_threshold() -> float:
    """Tier1 允许有损压缩的会话窗压阈值。"""
    try:
        from app.config import get_settings

        return float(getattr(get_settings(), "CTX_TIER1_PRESSURE_THRESHOLD", TIER1_SESSION_PRESSURE_THRESHOLD))
    except Exception:
        return TIER1_SESSION_PRESSURE_THRESHOLD

def read_lifecycle_config_from_settings() -> ReadLifecycleConfig:
    try:
        from app.config import get_settings
        s = get_settings()
        return ReadLifecycleConfig(
            enabled=bool(getattr(s, "CTX_READ_LIFECYCLE_ENABLED", True)),
            min_size_bytes=MIN_SIZE_BYTES,
        )
    except Exception:
        return ReadLifecycleConfig()

def layer1_compress_threshold_ratio(round_i: int, base_ratio: float) -> float:
    """@deprecated：P1 后 pre_round 入口改读 pre_round_budget()；保留供旧测试/日志兼容。"""
    from app.config import get_settings

    s = get_settings()
    start = int(getattr(s, "CTX_DEEP_ROUND_START", 8) or 8)
    deep = float(getattr(s, "CTX_DEEP_LAYER1_RATIO", 0.45) or 0.45)
    if round_i >= start:
        return min(base_ratio, deep)
    return base_ratio



@dataclass
class PreRoundBudget:
    """in-loop pre_round 管道决策（单一真源，避免 caller 与 compress 双处漂移）。"""

    should_enter: bool
    should_fold: bool
    should_layer1: bool
    action: str
    skip_reason: str
    fold_high: float
    fold_low: float
    layer1_emergency: float


def _pre_round_thresholds() -> tuple[float, float, float, int, bool]:
    """从 settings 读取 fold/emergency 阈值与 cooldown。"""
    try:
        from app.config import get_settings

        s = get_settings()
        return (
            float(getattr(s, "CTX_FOLD_HIGH_WATER", 0.60)),
            float(getattr(s, "CTX_FOLD_LOW_WATER", 0.50)),
            float(getattr(s, "CTX_LAYER1_EMERGENCY", 0.85)),
            int(getattr(s, "CTX_FOLD_COOLDOWN_ROUNDS", 3) or 3),
            bool(getattr(s, "CTX_FOLD_ENABLED", True)),
        )
    except Exception:
        return 0.60, 0.50, 0.85, 3, True


def pre_round_budget(
    pressure: float,
    *,
    fold_enabled: bool | None = None,
    retrieve_avail: bool,
    cache_read: int = 0,
    round_i: int = 0,
    last_fold_round: int = -100,
    fold_cooldown_rounds: int | None = None,
) -> PreRoundBudget:
    """fold 前预算：决定是否进入 pre_round 管道及是否尝试 fold。"""
    fold_high, fold_low, emergency, cooldown, fold_on = _pre_round_thresholds()
    if fold_enabled is not None:
        fold_on = fold_enabled
    if fold_cooldown_rounds is not None:
        cooldown = fold_cooldown_rounds

    skip = ""
    should_fold = False
    if not fold_on:
        skip = "fold_disabled"
    elif not retrieve_avail:
        skip = "no_retrieve_tool"
    elif pressure < fold_high:
        skip = "below_fold_high"
    elif last_fold_round >= 0 and round_i - last_fold_round < cooldown:
        skip = "fold_cooldown"
    else:
        should_fold = True

    should_enter = pressure >= fold_high or pressure >= emergency
    action = "fold" if should_fold else ("enter" if should_enter else "noop")
    if skip and should_enter and not should_fold:
        action = "enter"

    return PreRoundBudget(
        should_enter=should_enter,
        should_fold=should_fold,
        should_layer1=False,
        action=action,
        skip_reason=skip,
        fold_high=fold_high,
        fold_low=fold_low,
        layer1_emergency=emergency,
    )


def pre_round_budget_post_fold(
    pressure: float,
    *,
    tokens_after_fold: int,
    ctx_window: int,
    cache_read: int = 0,
    fold_ran: bool = False,
    fold_failed: bool = False,
    retrieve_avail: bool = True,
) -> PreRoundBudget:
    """fold 后二次预算：仅 emergency 或 fold 后仍超 fold_low 时允许 Layer1。"""
    fold_high, fold_low, emergency, _, _fold_on = _pre_round_thresholds()
    skip = ""
    should_layer1 = False

    if fold_failed:
        skip = "fold_failed"
    elif cache_read > 0 and pressure < emergency:
        skip = "cache_safe_below_emergency"
    elif fold_ran and tokens_after_fold <= ctx_window * fold_low:
        skip = "fold_sufficient"
    elif pressure >= emergency:
        should_layer1 = True
    elif fold_ran and tokens_after_fold > ctx_window * fold_low:
        should_layer1 = True
    else:
        skip = "below_emergency"

    action = "layer1" if should_layer1 else "noop"
    return PreRoundBudget(
        should_enter=True,
        should_fold=False,
        should_layer1=should_layer1,
        action=action,
        skip_reason=skip,
        fold_high=fold_high,
        fold_low=fold_low,
        layer1_emergency=emergency,
    )
