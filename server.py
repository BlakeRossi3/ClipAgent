"""FastAPI application: Twilio webhooks (``notify.router``), the owner dashboard and ``/healthz``.

Run it with ``python main.py serve`` (web only) or ``python main.py schedule`` (scheduler +
web). The dashboard is imported defensively so the Twilio webhooks keep working even if the
dashboard module cannot be imported.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import RedirectResponse

import db
import notify
from config import get_settings

log = logging.getLogger(__name__)

_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"]*", re.IGNORECASE)


class RedactTokenFilter(logging.Filter):
    """Blank ``?token=...`` in log records (uvicorn's access log prints the full request path, and
    the dashboard accepts its token as a query parameter once to set the session cookie)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_TOKEN_IN_URL.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str) and "token=" in record.msg.lower():
            record.msg = _TOKEN_IN_URL.sub(r"\1[redacted]", record.msg)
        return True


def _install_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in lg.filters):
            lg.addFilter(RedactTokenFilter())


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


def _setup_router() -> APIRouter | None:
    try:
        import setup_web
    except Exception as exc:  # the wizard is optional; webhooks + dashboard keep working without it
        log.warning("setup wizard unavailable: %s: %s", type(exc).__name__, exc)
        return None
    return setup_web.router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    _install_log_redaction()
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
    setup = _setup_router() if dashboard is not None else None
    if setup is not None:
        app.include_router(setup)
    if dashboard is not None:
        try:  # desktop modes / Claude history / Twilio check pages (optional, like the wizard)
            from assistant import toolkit_web

            app.include_router(toolkit_web.router)
        except Exception as exc:
            log.warning("toolkit pages unavailable: %s: %s", type(exc).__name__, exc)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "schema_version": db.schema_version(), "dashboard": app.state.dashboard}

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        if setup is not None and not get_settings().setup_complete:
            return RedirectResponse("/setup/")
        return RedirectResponse("/dashboard/" if app.state.dashboard else "/healthz")

    return app


app = create_app()
