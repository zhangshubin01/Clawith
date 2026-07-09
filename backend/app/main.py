"""Clawith Backend — FastAPI Application Entry Point."""

from contextlib import asynccontextmanager
from pathlib import Path
import shutil

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config import get_settings
from app.core.security import get_current_admin
from app.core.events import close_redis
from app.core.logging_config import configure_logging, intercept_standard_logging
from app.core.middleware import TraceIdMiddleware
from app.schemas.schemas import HealthResponse
from app.services.realtime import realtime_router

settings = get_settings()


def _process_roles() -> set[str]:
    raw = (settings.PROCESS_ROLE or "all").strip().lower()
    if not raw:
        return {"all"}
    roles = {part.strip() for part in raw.split(",") if part.strip()}
    return roles or {"all"}


def _role_enabled(*required: str) -> bool:
    roles = _process_roles()
    if "all" in roles:
        return True
    return any(role in roles for role in required)


def _log_bwrap_startup_status() -> None:
    """Emit a startup diagnostic for bubblewrap availability.

    We only warn when bwrap is missing so deployments can still start. Local
    source runs may explicitly allow a reduced-isolation fallback, while
    containerized deployments should keep fail-closed behavior.
    """
    in_container = Path("/.dockerenv").exists()
    bwrap_path = shutil.which("bwrap")

    if bwrap_path:
        location = "container" if in_container else "host"
        logger.info(f"[Startup] bubblewrap detected at {bwrap_path} ({location})")
        return

    if in_container:
        logger.warning(
            "[Startup] bubblewrap (bwrap) is not installed in the backend container. "
            "The service will still start, but execute_code will fail closed unless "
            "SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true is explicitly set."
        )
        return

    if settings.SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING:
        logger.warning(
            "[Startup] bubblewrap (bwrap) is not installed on the host. "
            "Local execute_code will use the reduced-isolation fallback."
        )
    else:
        logger.warning(
            "[Startup] bubblewrap (bwrap) is not installed on the host. "
            "execute_code will fail closed unless SANDBOX_ALLOW_UNSAFE_FALLBACK_WHEN_BWRAP_MISSING=true is set."
        )


async def _start_ss_local() -> None:
    """Start ss-local SOCKS5 proxy for Discord API calls. Tries nodes in priority order."""
    import asyncio, json, os, shutil, tempfile
    if not shutil.which("ss-local"):
        logger.info("[Startup] ss-local not found — Discord proxy disabled")
        return
    # Load proxy nodes from config file (gitignored, mounted as Docker volume)
    import json as _json
    cfg_file = os.environ.get("SS_CONFIG_FILE", "/data/ss-nodes.json")
    if os.path.isfile(cfg_file):
        # Guard against empty or malformed config file — both produce a clear
        # warning and a clean exit rather than an unhandled JSONDecodeError.
        try:
            raw = open(cfg_file).read().strip()
            if not raw:
                logger.warning(f"[Startup] {cfg_file} exists but is empty — skipping proxy")
                return
            nodes = _json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(f"[Startup] Failed to parse {cfg_file}: {exc} — skipping proxy")
            return
        logger.info(f"[Startup] Loaded {len(nodes)} node(s) from {cfg_file}")
        if not nodes:
            logger.info("[Startup] No nodes configured — skipping proxy")
            return
    elif os.environ.get("SS_SERVER") and os.environ.get("SS_PASSWORD"):
        nodes = [{"server": os.environ["SS_SERVER"], "port": int(os.environ.get("SS_PORT", "1080")),
                  "password": os.environ["SS_PASSWORD"], "method": os.environ.get("SS_METHOD", "chacha20-ietf-poly1305"), "label": "env"}]
    else:
        logger.info(f"[Startup] {cfg_file} not found and SS_SERVER not set — skipping proxy")
        return
    for node in nodes:
        cfg = {"server": node["server"], "server_port": node["port"], "local_address": "127.0.0.1",
               "local_port": 1080, "password": node["password"], "method": node["method"], "timeout": 10}
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cfg, tf); tf.close()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss-local", "-c", tf.name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            await asyncio.sleep(2)
            if proc.returncode is None:
                os.environ["DISCORD_PROXY"] = "socks5h://127.0.0.1:1080"
                logger.info(f"[Startup] ss-local → {node['label']} ({node['server']}:{node['port']})")
                return
            err = (await proc.stderr.read()).decode()[:120]
            logger.warning(f"[Startup] {node['label']} failed: {err}")
        except Exception as e:
            logger.error(f"[Startup] {node['label']} error: {e}")
    logger.warning("[Startup] All SS nodes failed — Discord API calls will run without proxy")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events."""
    # Configure logging first
    configure_logging()
    intercept_standard_logging()
    logger.info("[Startup] Logging configured")
    _log_bwrap_startup_status()

    # Warn about default JWT secrets in production
    if "change-me" in settings.SECRET_KEY.lower() or "change-me" in settings.JWT_SECRET_KEY.lower():
        logger.warning(
            "[Startup] WARNING: SECRET_KEY or JWT_SECRET_KEY contains default 'change-me' value. "
            "This is insecure for production. Set unique secrets in your .env file."
        )

    import asyncio
    import sys
    import os

    logger.info("[Startup] Config: LOG_FORMAT=%s LOG_LEVEL=%s", settings.LOG_FORMAT, settings.LOG_LEVEL)
    logger.info("[Startup] Config: SANDBOX_DEFAULT_TIMEOUT=%d SANDBOX_MAX_TIMEOUT=%d",
                settings.SANDBOX_DEFAULT_TIMEOUT, settings.SANDBOX_MAX_TIMEOUT)
    logger.info("[Startup] Config: PASSWORD_RESET_TOKEN_EXPIRE=%dm EMAIL_VERIFY_EXPIRE=%dm",
                settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES, settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES)
    logger.info("[Startup] Config: ACP_LLM_TIMEOUT=%ds ACP_CTX_WINDOW=%s",
                os.getenv("ACP_LLM_TIMEOUT_SECONDS", "600"), os.getenv("ACP_CTX_WINDOW_TOKENS", "131072"))

    from app.services.trigger_daemon import start_trigger_daemon
    from app.services.tool_seeder import seed_builtin_tools
    from app.services.template_seeder import seed_agent_templates
    from app.services.feishu_ws import feishu_ws_manager
    from app.services.dingtalk_stream import dingtalk_stream_manager
    from app.services.wecom_stream import wecom_stream_manager
    from app.services.wechat_channel import wechat_poll_manager
    from app.services.discord_gateway import discord_gateway_manager

    if _role_enabled("all", "bootstrap"):
        # ── Step 0: Ensure all DB tables exist (idempotent, safe to run on every startup) ──
        try:
            from app.database import Base, engine
            # Import all models so Base.metadata is fully populated
            import app.models.user           # noqa
            import app.models.agent          # noqa
            import app.models.task           # noqa
            import app.models.llm            # noqa
            import app.models.tool           # noqa
            import app.models.audit          # noqa
            import app.models.skill          # noqa
            import app.models.channel_config  # noqa
            import app.models.schedule       # noqa
            import app.models.plaza          # noqa
            import app.models.activity_log   # noqa
            import app.models.org            # noqa
            import app.models.system_settings  # noqa
            import app.models.invitation_code  # noqa
            import app.models.tenant         # noqa
            import app.models.tenant_setting  # noqa
            import app.models.participant    # noqa
            import app.models.chat_session   # noqa
            import app.models.trigger        # noqa
            import app.models.trigger_execution  # noqa
            import app.models.focus          # noqa
            import app.models.notification   # noqa
            import app.models.gateway_message # noqa
            import app.models.agent_credential  # noqa
            import app.models.okr            # noqa
            import app.models.onboarding     # noqa

            import app.models.identity       # noqa
            import app.models.ctx_ccr         # noqa  # CCR 原文归档表（保真上下文压缩）
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("[Startup] Database tables ready")
        except Exception as e:
            logger.warning(f"[Startup] create_all failed: {e}")
        logger.info("[Startup] seeding...")

        try:
            from app.models.tenant import Tenant
            from app.database import async_session as _session
            from sqlalchemy import select as _select, update as _update
            async with _session() as _db:
                _existing = await _db.execute(_select(Tenant).where(Tenant.slug == "default"))
                if not _existing.scalar_one_or_none():
                    _db.add(Tenant(name="Default", slug="default", im_provider="web_only"))
                    await _db.commit()
                    logger.info("[Startup] Default company created")

        except Exception as e:
            logger.warning(f"[Startup] Default company seed or A2A enable failed: {e}")

        try:
            import shutil
            from pathlib import Path as _Path
            from app.config import get_settings
            from app.models.tenant import Tenant as _T
            from app.database import async_session as _ses
            from sqlalchemy import select as _sel
            _data_dir = _Path(get_settings().AGENT_DATA_DIR)
            _old_dir = _data_dir / "enterprise_info"
            if _old_dir.exists() and any(_old_dir.iterdir()):
                async with _ses() as _db:
                    _first = await _db.execute(_sel(_T).order_by(_T.created_at).limit(1))
                    _tenant = _first.scalar_one_or_none()
                    if _tenant:
                        _new_dir = _data_dir / f"enterprise_info_{_tenant.id}"
                        if not _new_dir.exists():
                            shutil.copytree(str(_old_dir), str(_new_dir))
                            print(f"[Startup] ✅ Migrated enterprise_info → enterprise_info_{_tenant.id}", flush=True)
                        else:
                            print(f"[Startup] ℹ️ enterprise_info_{_tenant.id} already exists, skipping migration", flush=True)
        except Exception as e:
            print(f"[Startup] ⚠️ enterprise_info migration failed: {e}", flush=True)

        try:
            from app.services.tool_seeder import seed_builtin_tools, clean_orphaned_mcp_tools
            await seed_builtin_tools()
            await clean_orphaned_mcp_tools()
        except Exception as e:
            logger.warning(f"[Startup] Builtin tools seed or cleanup failed: {e}")

        try:
            from app.services.tool_seeder import seed_atlassian_rovo_config, get_atlassian_api_key
            await seed_atlassian_rovo_config()
            _rovo_key = await get_atlassian_api_key()
            if _rovo_key:
                from app.services.resource_discovery import seed_atlassian_rovo_tools
                await seed_atlassian_rovo_tools(_rovo_key)
        except Exception as e:
            logger.warning(f"[Startup] Atlassian tools seed failed: {e}")

        try:
            await seed_agent_templates()
        except Exception as e:
            logger.warning(f"[Startup] Agent templates seed failed: {e}")

        try:
            from app.services.skill_seeder import seed_skills, push_default_skills_to_existing_agents
            await seed_skills()
            await push_default_skills_to_existing_agents()
        except Exception as e:
            logger.warning(f"[Startup] Skills seed failed: {e}")

        try:
            from app.services.agent_seeder import seed_default_agents
            await seed_default_agents()
        except Exception as e:
            logger.warning(f"[Startup] Default agents seed failed: {e}")

        try:
            from app.services.agent_seeder import seed_okr_agent
            await seed_okr_agent()
        except Exception as e:
            logger.warning(f"[Startup] OKR Agent seed failed: {e}")

        try:
            from app.services.agent_seeder import patch_existing_okr_agent
            await patch_existing_okr_agent()
        except Exception as e:
            logger.warning(f"[Startup] OKR Agent patch failed: {e}")
    else:
        logger.info(f"[Startup] bootstrap skipped for PROCESS_ROLE={settings.PROCESS_ROLE}")

    if _role_enabled("all", "api"):
        try:
            from app.api.websocket import manager as ws_manager
            await realtime_router.start(ws_manager.deliver_pubsub_message)
            logger.info("[Startup] realtime router subscriber started")
        except Exception as e:
            logger.error(f"[Startup] realtime router start failed: {e}")

    try:
        logger.info("[Startup] starting background tasks...")
        from app.services.audit_logger import write_audit_log
        await write_audit_log("server_startup", {"pid": os.getpid()})

        def _bg_task_error(t):
            """Callback to surface background task exceptions."""
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc:
                logger.error(f"[Startup] Background task {t.get_name()} CRASHED: {exc}")
                import traceback
                traceback.print_exception(type(exc), exc, exc.__traceback__)

        task_specs = []
        if _role_enabled("all", "worker"):
            task_specs.append(("trigger_daemon", start_trigger_daemon()))
        if _role_enabled("all", "connector"):
            task_specs.extend([
                ("feishu_ws", feishu_ws_manager.start_all()),
                ("dingtalk_stream", dingtalk_stream_manager.start_all()),
                ("wecom_stream", wecom_stream_manager.start_all()),
                ("wechat_poll", wechat_poll_manager.start_all()),
                ("discord_gw", discord_gateway_manager.start_all()),
            ])

        for name, coro in task_specs:
            task = asyncio.create_task(coro, name=name)
            task.add_done_callback(_bg_task_error)
            logger.info(f"[Startup] created bg task: {name}")

        from app.services.llm.ccr_maintenance import ccr_maintenance_loop
        _ccr_task = asyncio.create_task(ccr_maintenance_loop(), name="ccr_maintenance")
        _ccr_task.add_done_callback(_bg_task_error)
        logger.info("[Startup] created bg task: ccr_maintenance")

        logger.info("[Startup] all background tasks created!")
    except Exception as e:
        logger.error(f"[Startup] Background tasks failed: {e}")
        import traceback
        traceback.print_exc()

    # Start ss-local SOCKS5 proxy for Discord API calls (non-fatal)
    ss_task = asyncio.create_task(_start_ss_local(), name="ss-local-proxy")
    ss_task.add_done_callback(_bg_task_error)

    yield

    # Shutdown
    await realtime_router.stop()
    await close_redis()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# Add TraceIdMiddleware first so it's executed for all requests
app.add_middleware(TraceIdMiddleware)

# CORS
_cors_origins = settings.CORS_ORIGINS
_allow_creds = "*" not in _cors_origins  # CORS spec forbids credentials with wildcard
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routes
from app.api.auth import router as auth_router
from app.api.agents import router as agents_router
from app.api.tasks import router as tasks_router
from app.api.files import router as files_router
from app.api.websocket import router as ws_router
from app.api.feishu import router as feishu_router
from app.api.sso import router as sso_router
from app.api.organization import router as org_router
from app.api.enterprise import router as enterprise_router
from app.api.advanced import router as advanced_router
from app.api.upload import router as upload_router
from app.api.relationships import router as relationships_router
from app.api.files import upload_router as files_upload_router, enterprise_kb_router
from app.api.activity import router as activity_router
from app.api.messages import router as messages_router
from app.api.tenants import router as tenants_router
from app.api.schedules import router as schedules_router
from app.api.tools import router as tools_router
from app.api.plaza import router as plaza_router
from app.api.skills import router as skills_router
from app.api.users import router as users_router
from app.api.chat_sessions import router as chat_sessions_router
from app.api.slack import router as slack_router
from app.api.discord_bot import router as discord_router
from app.api.dingtalk import router as dingtalk_router
from app.api.google_workspace import router as google_workspace_router
from app.api.wecom import router as wecom_router
from app.api.wechat import router as wechat_router
from app.api.teams import router as teams_router
from app.api.triggers import router as triggers_router
from app.api.focus import router as focus_router

from app.api.atlassian import router as atlassian_router

from app.api.webhooks import router as webhooks_router
from app.api.notification import router as notification_router
from app.api.gateway import router as gateway_router
from app.api.admin import router as admin_router
from app.api.pages import router as pages_router, public_router as pages_public_router
from app.api.agent_credentials import router as credentials_router
from app.api.agentbay_control import router as agentbay_control_router
from app.api.okr import router as okr_router
from app.api.ide_plugin import router as ide_plugin_router
from app.plugins.clawith_acp.router import router as acp_router
from app.api.frontend_log import router as frontend_log_router
from app.api.onboarding import router as onboarding_router

app.include_router(auth_router, prefix=settings.API_PREFIX)
app.include_router(agents_router, prefix=settings.API_PREFIX)
app.include_router(tasks_router, prefix=settings.API_PREFIX)
app.include_router(files_router, prefix=settings.API_PREFIX)
app.include_router(feishu_router, prefix=settings.API_PREFIX)
app.include_router(sso_router, prefix=settings.API_PREFIX)
app.include_router(org_router, prefix=settings.API_PREFIX)
app.include_router(enterprise_router, prefix=settings.API_PREFIX)
app.include_router(advanced_router, prefix=settings.API_PREFIX)
app.include_router(upload_router, prefix=settings.API_PREFIX)
app.include_router(relationships_router, prefix=settings.API_PREFIX)
app.include_router(activity_router, prefix=settings.API_PREFIX)
app.include_router(messages_router, prefix=settings.API_PREFIX)
app.include_router(tenants_router, prefix=settings.API_PREFIX)
app.include_router(schedules_router, prefix=settings.API_PREFIX)
app.include_router(tools_router, prefix=settings.API_PREFIX)
app.include_router(files_upload_router, prefix=settings.API_PREFIX)
app.include_router(enterprise_kb_router, prefix=settings.API_PREFIX)
app.include_router(skills_router, prefix=settings.API_PREFIX)
app.include_router(users_router, prefix=settings.API_PREFIX)
app.include_router(slack_router, prefix=settings.API_PREFIX)
app.include_router(discord_router, prefix=settings.API_PREFIX)
app.include_router(dingtalk_router, prefix=settings.API_PREFIX)
app.include_router(google_workspace_router, prefix=settings.API_PREFIX)
app.include_router(wecom_router, prefix=settings.API_PREFIX)
app.include_router(wechat_router, prefix=settings.API_PREFIX)
app.include_router(teams_router, prefix=settings.API_PREFIX)

app.include_router(atlassian_router, prefix=settings.API_PREFIX)

app.include_router(triggers_router, prefix=settings.API_PREFIX)
app.include_router(focus_router, prefix=settings.API_PREFIX)
app.include_router(chat_sessions_router, prefix=settings.API_PREFIX)
app.include_router(plaza_router, prefix=settings.API_PREFIX)
app.include_router(notification_router, prefix=settings.API_PREFIX)
app.include_router(webhooks_router)  # Public endpoint, no API prefix
app.include_router(ws_router)
app.include_router(gateway_router, prefix=settings.API_PREFIX)
app.include_router(admin_router, prefix=settings.API_PREFIX)
app.include_router(pages_router, prefix=settings.API_PREFIX)
app.include_router(pages_public_router)  # Public endpoint for /p/{short_id}, no API prefix
app.include_router(credentials_router, prefix=settings.API_PREFIX)
app.include_router(agentbay_control_router, prefix=settings.API_PREFIX)
app.include_router(okr_router)  # OKR — self-prefixed at /api/okr
app.include_router(ide_plugin_router)  # IDE Plugin — self-prefixed at /api/ide-plugin
app.include_router(acp_router)  # ACP WebSocket endpoint — self-prefixed at /ws/acp
app.include_router(frontend_log_router)  # Frontend log — self-prefixed at /api/log
app.include_router(onboarding_router, prefix=settings.API_PREFIX)


@app.get("/api/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Health check endpoint."""
    return HealthResponse(status="ok", version=settings.APP_VERSION)


@app.get("/api/health/ctx", tags=["health"])
async def ctx_health_check(_admin=Depends(get_current_admin)):
    """上下文压缩健康快照；需 admin 认证；只暴露计数和配置。"""
    from app.services.llm.ccr_store import get_ccr_metrics_snapshot

    return {"status": "ok", **get_ccr_metrics_snapshot()}


# ── Version endpoint (public, no auth required) ──
def _load_version_info() -> dict[str, str]:
    """Read version + commit hash once at startup."""
    import os, subprocess
    version = "unknown"
    for candidate in ["../frontend/VERSION", "frontend/VERSION", "VERSION"]:
        try:
            version = open(candidate).read().strip()
            break
        except FileNotFoundError:
            continue
    commit = ""
    for commit_file in ["../COMMIT", "COMMIT", "../frontend/COMMIT"]:
        try:
            commit = open(commit_file).read().strip()
            break
        except FileNotFoundError:
            continue
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode().strip()
        except Exception:
            pass
    return {"version": version, "commit": commit}

_version_cache = _load_version_info()

@app.get("/api/version", tags=["system"])
async def get_version():
    """Return current Clawith version and commit hash."""
    return _version_cache
