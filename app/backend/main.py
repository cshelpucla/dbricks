"""Backend entry point: JSON API under /api, static UI from frontend/public."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import config
from .routes import chat, clusters, control, ops, taxonomy

PUBLIC = Path(__file__).resolve().parent.parent / "frontend" / "public"


class GZipExceptStreams:
    """GZip, except for server-sent event endpoints (paths ending in /stream): gzip holds every event back until the
    stream ends. Older Starlette versions, such as the one installed in the app environment, compress event streams."""

    def __init__(self, app, **options):
        self.app, self.gzip = app, GZipMiddleware(app, **options)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].endswith("/stream"):
            return await self.app(scope, receive, send)
        return await self.gzip(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.DATA_MODE == "demo":
        from .services.demo import DemoSource
        from .services.costs import DemoCosts
        from .services.pipelines import DemoPipelines
        from .services.control import DemoControl
        app.state.source, app.state.pipelines, app.state.costs = DemoSource(), DemoPipelines(), DemoCosts()
        app.state.control = DemoControl()
        from .services.geo import DemoGeo
        app.state.geo = DemoGeo()
        from .services.taxonomy import DemoTaxonomy
        app.state.taxonomy = DemoTaxonomy()
    else:
        from .services.costs import LiveCosts
        from .services.pipelines import LivePipelines
        from .services.auto import AutoSource
        from .services.demo import DemoSource
        from .services.warehouse import WarehouseSource
        # The map falls back to labelled demo data until post_map and post_enriched have rows.
        app.state.source = AutoSource(WarehouseSource(), DemoSource())
        from .services.control import LiveControl
        app.state.pipelines, app.state.costs = LivePipelines(), LiveCosts()
        app.state.control = LiveControl(app.state.pipelines)
        from .services.geo import LiveGeo
        app.state.geo = LiveGeo()
        from .services.taxonomy import LiveTaxonomy
        app.state.taxonomy = LiveTaxonomy()
    from .services.chatstore import DeltaChatStore, SqliteChatStore
    app.state.chat_store = SqliteChatStore(config.CHAT_DB) if config.DATA_MODE == "demo" else DeltaChatStore()
    yield


app = FastAPI(title="Bluesky Analytics", lifespan=lifespan)
app.add_middleware(GZipExceptStreams, minimum_size=1000)
app.include_router(clusters.router)
app.include_router(ops.router)
app.include_router(control.router)
app.include_router(chat.router)
app.include_router(taxonomy.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": config.DATA_MODE}


app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="ui")
