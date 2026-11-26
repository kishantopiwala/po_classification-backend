from contextlib import asynccontextmanager

from fastapi import FastAPI

from .chat import router as chat_router
from .core.config import Settings
from .database import Database
from .extraction.jobs import JobManager
from .extraction.router import router
from .extraction.service import extract
from .mcp_server import create_mcp
from .po.chat import router as po_chat_router
from .po.router import router as po_router
from .po.service import POService


def create_app(settings=None, extractor=extract):
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = config
        config.upload_dir.mkdir(parents=True, exist_ok=True)
        database = Database(config.database_url) if config.database_url else None
        app.state.database = database
        app.state.po_service = POService(database, config) if database else None
        manager = JobManager(config, extractor, app.state.po_service)
        app.state.jobs = manager
        await manager.start()
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await manager.close()
            if database:
                await database.close()

    application = FastAPI(title="PO Classification API", lifespan=lifespan)
    application.include_router(router, prefix="/api/v1")
    application.include_router(chat_router, prefix="/api/v1")
    application.include_router(po_router, prefix="/api/v1")
    application.include_router(po_chat_router, prefix="/api/v1")

    @application.get("/health", tags=["health"])
    @application.get("/", include_in_schema=False)
    async def health():
        return {"message": "ok"}

    mcp = create_mcp(config, service_provider=lambda: application.state.po_service)
    mcp_app = mcp.streamable_http_app()
    # Mount last so API routes take precedence; MCP keeps its exact /mcp path.
    application.mount("/", mcp_app)

    return application


app = create_app()
