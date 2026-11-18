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
    try:  # one-time: the old default name Sterling -> Cecil in config.env
        from assistant.settings import migrate_legacy_name

        migrate_legacy_name()
    except Exception as exc:
        log.warning("assistant name migration skipped: %s", exc)
    try:  # the assistant's warm brain: connect now so the first question needs no process start
        from assistant import brain

        brain.warm()
    except Exception as exc:
        log.warning("voice brain not warmed: %s", exc)
    yield
    try:
        from assistant import brain

        if brain.running():
            brain.get_brain().stop()
    except Exception as exc:
        log.debug("ignored error (best effort): %r", exc)


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
        try:  # voice status/mute API (voice/web.py); optional
            from voice.web import router as voice_router

            app.include_router(voice_router)
        except Exception as exc:
            log.warning("voice API unavailable: %s", exc)
    if dashboard is not None:
        try:  # desktop modes / Claude history / Twilio check pages (optional, like the wizard)
            from assistant import toolkit_web

            app.include_router(toolkit_web.router)
        except Exception as exc:
            log.warning("toolkit pages unavailable: %s: %s", type(exc).__name__, exc)
        try:  # Chrome "Cecil Page Reader" extension posts the page he clicked on (assistant/page_reader.py)
            from assistant import page_reader

            app.include_router(page_reader.router)
        except Exception as exc:
            log.warning("page reader unavailable: %s: %s", type(exc).__name__, exc)

    @app.exception_handler(Exception)
    async def _unhandled(request: Any, exc: Exception) -> Any:
        """A page that crashes is a failure like any other: log it as an error event (so the needs list and
        self-repair see it, with the cause), then answer 500. Before this, a broken page left no trace."""
        import traceback

        from fastapi.responses import PlainTextResponse

        tb = traceback.extract_tb(exc.__traceback__)
        where = [f"{f.filename.replace(chr(92), '/').rsplit('/', 2)[-1]}:{f.lineno} {f.name}" for f in tb[-4:]]
        try:
            db.log_event("server", f"page failed: {request.method} {request.url.path}: {type(exc).__name__}",
                         level="error", data={"error": repr(exc)[:500], "where": where})
        except Exception as log_exc:   # the database itself may be what broke
            log.error("page failed (%r) and could not be logged: %r", exc, log_exc)
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return PlainTextResponse(f"Internal Server Error: {type(exc).__name__}: {str(exc)[:300]}\n"
                                 f"(logged; Cecil's self-repair will look at it)", status_code=500)

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
