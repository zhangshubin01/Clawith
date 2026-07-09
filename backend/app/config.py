"""Application configuration."""

from functools import lru_cache
import os
from pathlib import Path
import socket
import uuid

from pydantic import model_validator
from pydantic_settings import BaseSettings

from app.services.sandbox.config import SandboxConfig, SandboxType


def _running_in_container() -> bool:
    """Best-effort container runtime detection."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True

    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False

    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return any(token in content for token in ("docker", "containerd", "kubepods", "podman"))


def _default_agent_data_dir() -> str:
    """Use Docker path in containers, user-writable path on local hosts."""
    if _running_in_container():
        return "/data/agents"
    return str(Path.home() / ".clawith" / "data" / "agents")


def _default_instance_id() -> str:
    """Generate a stable-enough per-process instance identifier."""
    host = socket.gethostname() or "unknown"
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:8]
    return f"{host}-{pid}-{suffix}"


def _default_agent_template_dir() -> str:
    """Locate the agent template directory for both Docker and source deployments.

    In a Docker container the backend source is copied to /app, so the template
    lives at /app/agent_template.  In a source deployment it sits next to the
    backend/ package root, i.e. <repo>/backend/agent_template.
    """
    if _running_in_container():
        return "/app/agent_template"
    # Source layout: backend/app/config.py -> ../.. = backend/ -> agent_template
    source_path = Path(__file__).resolve().parent.parent / "agent_template"
    return str(source_path)


def _default_log_dir() -> str:
    """Docker mode returns empty (managed by json-file driver), local returns ~/.clawith/data/log."""
    if _running_in_container():
        return ""
    return str(Path.home() / ".clawith" / "data" / "log")


def _default_allow_unsafe_bwrap_fallback() -> bool:
    """Allow local source runs to work without bubblewrap by default."""
    return not _running_in_container()


def _read_version() -> str:
    """Read version from local VERSION file, fallback to root."""
    for candidate in [Path(__file__).resolve().parent.parent / "VERSION",
                      Path(__file__).resolve().parent.parent.parent / "VERSION",
                      Path("/app/VERSION"), Path("/VERSION")]:
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App
    APP_NAME: str = "Clawith"
    APP_VERSION: str = _read_version()
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://clawith:clawith@localhost:5432/clawith"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    INSTANCE_ID: str = _default_instance_id()

    # JWT — 生产环境必须通过 JWT_SECRET_KEY 环境变量覆盖
    JWT_SECRET_KEY: str = "change-me-jwt-secret-dev-only"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    # refresh 端点允许在 exp 过期后仍续期的宽限天数（IDE 周末挂机场景）
    JWT_REFRESH_GRACE_DAYS: int = 7
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 60  # 1 hour
    EMAIL_VERIFICATION_REQUIRED: bool = False  # Require email verification for login

    # File Storage
    STORAGE_BACKEND: str = "local"
    AGENT_DATA_DIR: str = _default_agent_data_dir()
    AGENT_TEMPLATE_DIR: str = _default_agent_template_dir()
    STORAGE_LOCAL_ROOT: str = _default_agent_data_dir()
    STORAGE_LOCAL_FALLBACK_ENABLED: bool = True
    S3_BUCKET: str = ""
    S3_REGION: str = ""
    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_PREFIX: str = "agents"
    S3_PRESIGN_TTL_SECONDS: int = 3600
    S3_MAX_POOL_CONNECTIONS: int = 50
    S3_WRITE_WORKERS: int = 32

    # Process role
    PROCESS_ROLE: str = "all"

    # Logging
    LOG_DIR: str = _default_log_dir()
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "color"  # "color" (human-readable) or "json" (structured for log aggregators)
    LOG_ROTATION: str = "00:00"
    LOG_RETENTION: str = "30 days"
    LOG_COMPRESSION: str = "gz"
    LOG_DIAGNOSE: bool = False  # Production-safe: don't expose variable values in tracebacks

    # 上下文压缩总开关（Layer 1 轮次压缩）。
    # off 时 call_llm 每轮 _ctx_compress 透传；Layer 0 工具硬天花板始终保留（防 OOM）。
    # 生产 incident 时一条 env 即可快速旁路轮内压缩。
    CTX_COMPRESS_ENABLED: bool = True

    # 仅允许真无损压缩（禁止 head+tail / row-drop 等一切有损）。
    # true 时 Layer 0 工具压缩超预算直接回退原文（配合硬天花板防 OOM）。
    CTX_LOSSLESS_ONLY: bool = False
    # 追加排除压缩的工具名 CSV。默认排除名单在 compression_config.py，避免多处漂移。
    CTX_EXCLUDE_TOOLS: str = ""
    # 后续 content_router 使用的压力自适应阈值；Stage1 先暴露配置面。
    CTX_MIN_RATIO_RELAXED: float = 0.85
    CTX_MIN_RATIO_AGGRESSIVE: float = 0.65
    # Tier1 read/write 在会话窗压超过此值且超 budget 时允许 Layer0 有损+CCR
    CTX_TIER1_PRESSURE_THRESHOLD: float = 0.55
    # Read lifecycle / Layer2 offload 的开关先落配置，后续 Stage 接线。
    CTX_READ_LIFECYCLE_ENABLED: bool = True
    CTX_FROZEN_PREFIX_MSGS: int = 2
    # F2 无损历史适配：token 预算内先压老轮 tool，仍超则 offload 丢弃前缀（见 history_hydrate）
    # rtk never_worse 日志灰度 path CSV；如 acp,ws
    CTX_RTK_INVARIANT_PATHS: str = ""
    # CCR 归档条目 TTL（小时）。retrieve 超时视为 miss；purge_expired 清理。
    CTX_CCR_TTL_HOURS: int = 24
    # 单会话 PG CCR 行数上限，超限 LRU 驱逐最旧（≠ 进程内全局读缓存上限）。
    CTX_CCR_MAX_PER_SESSION: int = 500
    # CCR 多轮跟踪 + 预展开提示（headroom context_tracker 等价）
    CTX_TRACKER_ENABLED: bool = True
    # CCR purge / metrics 后台周期（秒）
    CTX_CCR_PURGE_INTERVAL_SEC: int = 3600
    CTX_CCR_METRICS_LOG: bool = True
    # search/log 相关性拆分：零依赖 BM25，关闭后退回现有压缩路径
    CTX_RELEVANCE_SPLIT_ENABLED: bool = True
    CTX_RELEVANCE_THRESHOLD: float = 0.25
    CTX_RELEVANCE_MAX_RECORDS: int = 200
    CTX_RELEVANCE_ADAPTIVE_THRESHOLD: bool = True
    # 阶段4 feedback：默认关闭，待 stats 数据积累后再灰度
    CTX_COMPRESSION_FEEDBACK_ENABLED: bool = False
    CTX_FEEDBACK_MIN_EVENTS: int = 20
    CTX_FEEDBACK_MAX_THRESHOLD_DELTA: float = 0.15
    # 阶段6 output shaper：独立 initiative，默认关闭
    CTX_OUTPUT_SHAPER_ENABLED: bool = False
    CTX_OUTPUT_SHAPER_PATHS: str = "acp,ws,feishu"
    CTX_OUTPUT_SHAPER_MAX_SUFFIX_CHARS: int = 500
    # DB 历史 tool 结果加载上限（字符）。0=按 per-tool token budget×4；>0=全局顶。
    CTX_HISTORY_TOOL_MAX_CHARS: int = 0
    # persist 时超过此长度写入 CCR 并在 payload 记录 ccr_hash。
    CTX_HISTORY_CCR_THRESHOLD: int = 2048
    # F6：单 prompt 深 loop — 超过此轮次（0-based）逐步收紧 Layer0 budget
    CTX_DEEP_ROUND_START: int = 8
    CTX_DEEP_ROUND_STEP: float = 0.05
    CTX_DEEP_ROUND_BUDGET_FLOOR: float = 0.25
    # F6：深 loop 时 Layer1 提前触发阈值（相对 ctx_window）。@deprecated：P1 后不再作 pre_round 入口。
    CTX_DEEP_LAYER1_RATIO: float = 0.45

    # ── Wave6 Cache-Safe Compaction（§15：消除 round97 式 cache 断崖）──
    # 边界折叠总开关。true=in-loop 用 reactive_fold（一次性边界折叠 + CCR）替代旧 scatter offload；
    # false=Layer1 in-loop 仅透传（不折叠、不 scatter），生产 incident 时可一键旁路。
    CTX_FOLD_ENABLED: bool = True
    # 触发折叠的高水位（相对 ctx_window）。P1 v2：0.60 对齐 warn，消除 60–65% 空档。
    CTX_FOLD_HIGH_WATER: float = 0.60
    # 折叠目标低水位（迟滞）。high>low 形成迟滞带，避免每轮反复折叠（ping-pong）。
    CTX_FOLD_LOW_WATER: float = 0.50
    # in-loop Layer1 仅在此紧急水位以上触发（或 fold 后仍超 fold_low）。
    CTX_LAYER1_EMERGENCY: float = 0.85
    # 相邻两次 fold 的最小轮次间隔，减轻 cache ping-pong。
    CTX_FOLD_COOLDOWN_ROUNDS: int = 3
    # live_zone 保留最近轮数（此前散落于 LAYER1_PROTECT_ROUNDS=10）。
    # 该区仅 Layer0 + read_lifecycle 可动；折叠只作用于 frozen 与 live 之间的 compressible 区。
    CTX_LIVE_ZONE_ROUNDS: int = 10


    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "clawith_network"
    OPENCLAW_IMAGE: str = "openclaw:local"
    OPENCLAW_GATEWAY_PORT: int = 18789

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    PUBLIC_BASE_URL: str = ""
    HTTP_PROXY: str = ""

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Jina AI (Reader + Search APIs)
    JINA_API_KEY: str = ""

    # Exa AI (Search API)
    EXA_API_KEY: str = ""


    # Sandbox configuration
    SANDBOX_TYPE: SandboxType = SandboxType.SUBPROCESS
    SANDBOX_API_KEY: str = ""
    SANDBOX_API_URL: str = ""
    SANDBOX_CPU_LIMIT: str = "0.5"
    SANDBOX_MEMORY_LIMIT: str = "256m"
    SANDBOX_ALLOW_NETWORK: bool = False
    SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING: bool = _default_allow_unsafe_bwrap_fallback()
    SANDBOX_DEFAULT_TIMEOUT: int = 30
    SANDBOX_MAX_TIMEOUT: int = 60

    @model_validator(mode='after')
    def validate_secrets(self):
        """生产环境检查：拒绝使用默认密钥启动。DEBUG 模式下仅警告，不阻止启动。"""
        if self.DEBUG:
            return self
        if self.SECRET_KEY == "change-me-in-production":
            raise ValueError(
                "SECRET_KEY 仍为默认值 'change-me-in-production'，"
                "请设置 SECRET_KEY 环境变量"
            )
        if self.JWT_SECRET_KEY == "change-me-jwt-secret-dev-only":
            raise ValueError(
                "JWT_SECRET_KEY 仍为默认值 'change-me-jwt-secret-dev-only'，"
                "请设置 JWT_SECRET_KEY 环境变量"
            )
        return self

    def get_agent_workspace_path(self, agent_id: str) -> Path:
        """Return the workspace directory path for a given agent.

        Args:
            agent_id: The agent's unique identifier (string or UUID)

        Returns:
            Path object pointing to the agent's workspace directory
        """
        return Path(self.AGENT_DATA_DIR) / str(agent_id)

    model_config = {
        "env_file": [".env", "../.env"],
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()


def get_sandbox_config() -> SandboxConfig:
    """Create SandboxConfig from application settings."""
    settings = get_settings()
    return SandboxConfig(
        type=settings.SANDBOX_TYPE,
        enabled=True,
        api_key=settings.SANDBOX_API_KEY,
        api_url=settings.SANDBOX_API_URL,
        cpu_limit=settings.SANDBOX_CPU_LIMIT,
        memory_limit=settings.SANDBOX_MEMORY_LIMIT,
        allow_network=settings.SANDBOX_ALLOW_NETWORK,
        allow_unsafe_fallback_when_bwrap_missing=settings.SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING,
        default_timeout=settings.SANDBOX_DEFAULT_TIMEOUT,
        max_timeout=settings.SANDBOX_MAX_TIMEOUT,
    )
