"""FastAPI application: Twilio webhooks (``notify.router``), the owner dashboard and ``/healthz``.

Run it with ``python main.py serve`` (web only) or ``python main.py schedule`` (scheduler +
web). The dashboard is imported defensively so the Twilio webhooks keep working even if the
dashboard module cannot be imported.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import RedirectResponse

import db
import notify
from config import get_settings

log = logging.getLogger(__name__)


def _dashboard_router() -> APIRouter | None:
    try:
        import dashboard
    except Exception as exc:  # a broken dashboard must not take the approval webhooks down
        log.warning("dashboard unavailable, serving without it: %s: %s", type(exc).__name__, exc)
        return None
    router = getattr(dashboard, "router", None)
    if not isinstance(router, APIRouter):
        log.warning("dashboard module has no APIRouter named 'router'; serving without it")
        return None
    return router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_settings().ensure_dirs()
    db.init_db()
    yield


def create_app() -> FastAPI:
    """Build the app: ``/twilio/*``, ``/dashboard/*`` (when importable), ``/healthz``, ``/`` redirect."""
    app = FastAPI(title="Auto Shorts Agent", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(notify.router)
    dashboard = _dashboard_router()
    if dashboard is not None:
        app.include_router(dashboard)
    app.state.dashboard = dashboard is not None

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "schema_version": db.schema_version(), "dashboard": app.state.dashboard}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard/" if app.state.dashboard else "/healthz")

    return app


app = create_app()
