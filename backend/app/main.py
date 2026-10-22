import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware

from .api import make_router
from .config import get_settings
from .console_auth import ConsoleAuthMiddleware
from .db import ConfigEntry, SessionLocal, init_db
from .llm import LLMAdapter
from .knowledge import KnowledgeGatewayClient
from .ocr import OCRGatewayClient
from .platform import PlatformGatewayClient
from .runtime import RuntimeManager
from .service import DSHService, EventBroker
from sqlalchemy import select


settings = get_settings()
runtime_manager = RuntimeManager(settings.runtime_idle_ttl_seconds)
broker = EventBroker()
llm = LLMAdapter(settings)
ocr = OCRGatewayClient(settings)
knowledge = KnowledgeGatewayClient(settings)
platform = PlatformGatewayClient(settings)
service = DSHService(runtime_manager, llm, broker, ocr, knowledge, platform)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Code-only restarts must not migrate or reseed the existing database.
    if settings.database_init_enabled:
        await init_db()
    await service.skill_catalog.invalidate()
    # Re-apply operator-managed live settings after every container restart.
    # The DB/Redis URLs remain restart-only because their pools are constructed
    # before the application lifespan begins.
    async with SessionLocal() as db:
        entries = list((await db.execute(select(ConfigEntry).where(ConfigEntry.scope == "system"))).scalars().all())
        await service.apply_config_entries(entries)
    stop = asyncio.Event()

    async def sweeper() -> None:
        while not stop.is_set():
            await asyncio.sleep(30)
            await runtime_manager.sweep()

    async def audit_sweeper() -> None:
        while not stop.is_set():
            with suppress(Exception):
                await service.purge_expired_audit()
            interval = max(60, int(service.settings.audit_cleanup_interval_seconds))
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(sweeper())
    audit_task = asyncio.create_task(audit_sweeper()) if settings.audit_cleanup_enabled else None
    yield
    stop.set()
    task.cancel()
    if audit_task:
        audit_task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    if audit_task:
        with suppress(asyncio.CancelledError):
            await audit_task
    await service.skill_catalog.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "NMA AI Assistant API. Customer-facing answers preserve the same evidence, "
        "facts, constraints, and uncertainty across supported response languages."
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list or ["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(ConsoleAuthMiddleware, get_password=lambda: service.console_password)
app.include_router(make_router(service))


def custom_openapi():
    """Expose the WebSocket event contract alongside the generated OpenAPI schema."""

    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["x-websocket-events"] = {
        "/api/v1/ws": {
            "description": (
                "Authenticated JSON WebSocket used by the Customer AI Chatbot. "
                "The assistant.message event may include profileAction when the "
                "request must be answered in another authorized Profile."
            ),
            "authentication": {
                "frame": {"type": "auth", "umctoken": "string"},
                "requirements": ["UMC bearer token", "owned conversation"],
            },
            "assistantMessage": {
                "profileAction": {
                    "type": "open_profile_menu",
                    "code": [
                        "profile_selection_required",
                        "profile_switch_required",
                        "selected_profile_not_available",
                        "no_results_current_profile",
                    ],
                    "targetProfile": {
                        "profileId": "authorized profile identifier",
                        "profileName": "display name",
                    },
                },
                "scope": (
                    "targetProfile is present only for an authorized profile resolved "
                    "from the current account context; clients must still match it "
                    "against their refreshed authorized profile list before switching."
                ),
            },
        },
    }
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "service": settings.app_name,
        "runtimeMode": "embedded-lease-mvp",
        "umcPortal": settings.umc_portal_name,
        "umcBaseUrl": settings.umc_base_url,
        "ocrGateway": settings.ocr_gateway_url,
        "knowledgeGateway": settings.knowledge_gateway_url,
        "platformGateway": settings.platform_gateway_url,
    }


@app.get("/")
async def root():
    return {"service": settings.app_name, "docs": "/docs", "health": "/healthz"}
