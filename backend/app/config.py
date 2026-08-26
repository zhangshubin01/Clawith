"""Application configuration."""

from functools import lru_cache
import os
from pathlib import Path
import socket
from typing import Self
import uuid

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from app.services.sandbox.config import (
    CODE_EXECUTION_DEFAULT_TIMEOUT_SECONDS,
    CODE_EXECUTION_MAX_TIMEOUT_SECONDS,
    SandboxConfig,
    SandboxType,
)


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


def _default_allow_unsafe_bwrap_fallback() -> bool:
    """Fail closed by default.

    Running untrusted code directly on the host (no bubblewrap) exposes the
    full host environment — env vars, filesystem, network — to the code.
    Opt in explicitly with
    SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true.
    """
    return False


def _read_version() -> str:
    """Read version from local VERSION file, fallback to root."""
    for candidate in [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path(__file__).resolve().parent.parent.parent / "VERSION",
        Path("/app/VERSION"),
        Path("/VERSION"),
    ]:
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
    DATABASE_AUTO_CREATE_TABLES: bool = False
    # Connection budget: the SQLAlchemy pool and the checkpoint pool share one
    # PostgreSQL max_connections limit with per-session MCP runtimes. Keep the
    # primary pool small — chat latency does not scale with pool size, and an
    # oversized base pool idles out the whole database.
    DB_POOL_SIZE: int = 8
    DB_MAX_OVERFLOW: int = 4
    # Connections reserved for consumers the backend does not own (per-session
    # MCP runtimes, admin tooling, migration jobs). Used by the startup budget
    # check to warn before PostgreSQL max_connections is exhausted.
    DB_RESERVED_CONNECTIONS: int = 20
    # Process-level shared checkpoint pool (LangGraph AsyncPostgresSaver).
    CHECKPOINT_POOL_MIN_SIZE: int = 1
    CHECKPOINT_POOL_MAX_SIZE: int = 4
    CHECKPOINT_POOL_TIMEOUT_SECONDS: int = 10

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    INSTANCE_ID: str = _default_instance_id()

    # JWT
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
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
    APP_WORKERS: int = 1
    BCRYPT_WORKERS: int = 4
    LOGIN_SLOW_LOG_THRESHOLD_MS: int = 1000

    # Agent Runtime
    AGENT_RUNTIME_V2_ENABLED: bool = True
    AGENT_RUNTIME_V2_AGENT_IDS: str = ""
    AGENT_RUNTIME_V2_SOURCE_TYPES: str = "task"
    AGENT_RUNTIME_GRAPH_NAME: str = "clawith_agent_runtime"
    AGENT_RUNTIME_GRAPH_VERSION: str = "v1"
    LANGGRAPH_CHECKPOINT_DATABASE_URL: str | None = None
    LANGGRAPH_AES_KEY: str | None = None
    # Maximum number of Agent Run commands executed concurrently by one
    # Runtime worker process. Thread/lane locks still serialize conflicting
    # Runs; this is the shared capacity across all eligible Agents.
    AGENT_RUNTIME_COMMAND_CONCURRENCY: int = Field(default=10, gt=0, le=100)
    AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS: int = Field(default=60, gt=0)
    AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS: int = Field(default=20, gt=0)
    # Consecutive claim-renewal failures a command worker tolerates before its
    # heartbeat gives up. A transient DB/event-loop hiccup should survive; a
    # genuinely lost claim (another worker took over) should stop the heartbeat
    # rather than spam errors for the rest of a long graph execution.
    AGENT_RUNTIME_COMMAND_HEARTBEAT_MAX_FAILURES: int = Field(default=3, ge=0)
    # Command-daemon liveness watchdog. A daemon that stops completing run_once
    # for STALL_SECONDS is reported with a coroutine stack dump (it may be a
    # long-running command, or a stuck DB-session acquisition). The supervisor
    # scans every SCAN_SECONDS and emits an "alive" heartbeat every
    # HEARTBEAT_SECONDS while all daemons are healthy.
    AGENT_RUNTIME_COMMAND_STALL_SECONDS: float = Field(default=300.0, gt=0)
    AGENT_RUNTIME_COMMAND_SUPERVISOR_SCAN_SECONDS: float = Field(default=30.0, gt=0)
    AGENT_RUNTIME_COMMAND_HEARTBEAT_SECONDS: float = Field(default=300.0, gt=0)
    # Maximum LangGraph recursion steps per Agent Run. Raised from the
    # library default (25) to absorb long tool-call loops; override via
    # AGENT_RUNTIME_RECURSION_LIMIT when a tenant needs more headroom.
    AGENT_RUNTIME_RECURSION_LIMIT: int = Field(default=200, gt=0)
    AGENT_RUNTIME_COMMAND_MAX_ATTEMPTS: int = Field(default=5, gt=0)
    # Safe-read fence defer: a Command that hits an active Tool fence is
    # released and becomes reclaimable only after the fence lease expires.
    # MAX = total defer window before the wait is declared a stall. It is
    # pinned to 3x the Tool lease TTL, which is the RuntimeToolStepService
    # constant 300s — change both together if the lease TTL ever moves.
    # JITTER = random spread so concurrent daemons do not wake together when
    # several commands share one lease deadline. A fence without a usable
    # lease deadline (e.g. reconciliation still settling) retries with
    # jitter alone, never the full MAX window.
    AGENT_RUNTIME_COMMAND_FENCE_DEFER_MAX_SECONDS: int = Field(default=900, gt=0)
    AGENT_RUNTIME_COMMAND_FENCE_DEFER_JITTER_SECONDS: float = Field(default=5.0, ge=0)
    AGENT_RUNTIME_ASYNC_TOOL_POLL_SCAN_SECONDS: float = Field(default=0.25, gt=0)
    AGENT_RUNTIME_TOOL_LEASE_RECONCILE_SCAN_SECONDS: float = Field(default=1.0, gt=0)
    AGENT_RUNTIME_CHANNEL_DELIVERY_CLAIM_TTL_SECONDS: int = Field(default=120, gt=0)
    AGENT_RUNTIME_CHANNEL_DELIVERY_MAX_ATTEMPTS: int = Field(default=8, gt=0)
    AGENT_RUNTIME_CHANNEL_DELIVERY_SCAN_SECONDS: float = Field(default=0.5, gt=0)
    AGENT_RUNTIME_SUMMARY_THRESHOLD_RATIO: float = Field(default=0.85, gt=0, le=1)
    AGENT_RUNTIME_SESSION_RECENT_MESSAGES: int = Field(default=20, gt=0)
    AGENT_RUNTIME_SESSION_COMPACT_MESSAGE_THRESHOLD: int | None = Field(default=None, gt=0)
    AGENT_RUNTIME_SESSION_COMPACT_SCAN_SECONDS: float = Field(default=5.0, gt=0)
    AGENT_RUNTIME_SESSION_COMPACT_SCAN_BATCH_SIZE: int = Field(default=50, gt=0, le=500)
    AGENT_RUNTIME_RUN_COMPACT_MESSAGE_THRESHOLD: int | None = Field(default=None, gt=0)
    AGENT_RUNTIME_RUN_COMPACT_TOOL_RESULT_BYTES: int | None = Field(default=None, gt=0)
    AGENT_RUNTIME_VERIFY_REPAIR_COMPACT_ROUNDS: int | None = Field(default=None, gt=0)
    AGENT_RUNTIME_MODEL_CAPABILITY_REFRESH_SECONDS: int = Field(default=86400, gt=0)
    AGENT_RUNTIME_WEB_STREAMING_ENABLED: bool = True
    AGENT_RUNTIME_FALLBACK_CONTEXT_WINDOW_TOKENS: int = Field(default=131072, gt=0)
    MULTI_AGENT_COMPACT_MODEL_ID: uuid.UUID | None = None
    MULTI_AGENT_PLANNING_MODEL_ID: uuid.UUID | None = None
    GROUP_CONTEXT_ANNOUNCEMENT_MAX_CHARS: int = Field(default=12000, gt=0)
    GROUP_CONTEXT_MEMORY_MAX_CHARS: int = Field(default=12000, gt=0)
    GROUP_CONTEXT_WORKSPACE_MAX_ENTRIES: int = Field(default=100, gt=0)
    AGENT_RUNTIME_CHECKPOINT_RETENTION_DAYS: int = Field(default=30, gt=0)
    AGENT_RUNTIME_EVENT_PAYLOAD_MAX_BYTES: int = Field(default=16384, gt=0)
    AGENT_RUNTIME_TOOL_RESULT_INLINE_MAX_BYTES: int = Field(default=8192, gt=0)
    MAX_AGENT_CYCLE_COUNT: int = Field(default=5, gt=0)

    # Observability (Langfuse trace-level tracing; a no-op unless enabled + configured)
    OBSERVABILITY_ENABLED: bool = False
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = ""  # self-hosted base URL; empty = Langfuse Cloud
    # Multi-tenant isolation: JSON map of tenant_id -> {public_key, secret_key}.
    # When set, traces for a tenant with a configured key go to that tenant's
    # Langfuse project; unmatched tenants fall back to LANGFUSE_PUBLIC_KEY.
    LANGFUSE_TENANT_KEYS: str = ""
    # Hard cap for file-upload endpoints (all of them buffer the whole body
    # into memory before writing). Without it a single authenticated request
    # can exhaust the process (see P0 fix plan D3). Override via env if a
    # tenant legitimately needs larger uploads.
    MAX_UPLOAD_BYTES: int = Field(default=50 * 1024 * 1024, gt=0)

    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "clawith_network"
    OPENCLAW_IMAGE: str = "openclaw:local"
    OPENCLAW_GATEWAY_PORT: int = 18789

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    # Lark 国际版: API 网关固定用 open.larksuite.com（开发者后台网页才是 open.larkoffice.com，
    # 不能拿 larkoffice 调 API）；飞书国内版用 open.feishu.cn。
    # 注意: WS 端点(/callback/ws/endpoint)按 app 迁移进度分流，部分 app 只认 open.larkoffice.com，
    # 需在 channel_configs.extra_config.domain 里按 agent 覆盖（feishu_ws.py 已支持）。
    FEISHU_DOMAIN: str = "https://open.larksuite.com"
    PUBLIC_BASE_URL: str = ""
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""
    NO_PROXY: str = ""

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
    SANDBOX_DEFAULT_TIMEOUT: int = CODE_EXECUTION_DEFAULT_TIMEOUT_SECONDS
    SANDBOX_MAX_TIMEOUT: int = CODE_EXECUTION_MAX_TIMEOUT_SECONDS
    SANDBOX_HTTP_PROXY: str = ""
    SANDBOX_HTTPS_PROXY: str = ""
    SANDBOX_NO_PROXY: str = ""

    @field_validator(
        "LANGGRAPH_CHECKPOINT_DATABASE_URL",
        "LANGGRAPH_AES_KEY",
        "MULTI_AGENT_COMPACT_MODEL_ID",
        "MULTI_AGENT_PLANNING_MODEL_ID",
        "AGENT_RUNTIME_SESSION_COMPACT_MESSAGE_THRESHOLD",
        "AGENT_RUNTIME_RUN_COMPACT_MESSAGE_THRESHOLD",
        "AGENT_RUNTIME_RUN_COMPACT_TOOL_RESULT_BYTES",
        "AGENT_RUNTIME_VERIFY_REPAIR_COMPACT_ROUNDS",
        mode="before",
    )
    @classmethod
    def _blank_optional_runtime_values(cls, value: object) -> object | None:
        """Treat blank optional environment variables as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("AGENT_RUNTIME_GRAPH_NAME", "AGENT_RUNTIME_GRAPH_VERSION")
    @classmethod
    def _nonempty_runtime_identifiers(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Runtime graph name and version must not be blank")
        return normalized

    @model_validator(mode="after")
    def _claim_renewal_precedes_expiry(self) -> Self:
        if self.AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS >= self.AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS:
            raise ValueError(
                "AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS must be less than AGENT_RUNTIME_COMMAND_CLAIM_TTL_SECONDS"
            )
        return self

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
        http_proxy=settings.SANDBOX_HTTP_PROXY or settings.HTTP_PROXY or None,
        https_proxy=settings.SANDBOX_HTTPS_PROXY or settings.HTTPS_PROXY or None,
        no_proxy=settings.SANDBOX_NO_PROXY or settings.NO_PROXY or None,
    )
