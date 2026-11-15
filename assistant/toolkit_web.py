"""Dashboard pages for the assistant toolkit, linked from Settings.

* ``/dashboard/modes`` - desktop modes: a button per mode, the mode YAML editor (validated on save).
* ``/dashboard/history`` - Claude history import: paste the export manifest (hands-free), upload any
  number of export zips, progress, distil a batch now.
* ``/dashboard/twilio-check`` - Twilio doctor results with "Fix it" buttons.
* ``/dashboard/approvals`` - standing approvals ("always allow this kind"): review and revoke.
* ``/dashboard/skills`` - the trusted skill installer: installed, available from trusted sources, review cards.

Auth, CSRF and page chrome come from :mod:`dashboard` (same token/cookie as every dashboard page).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/dashboard", tags=["toolkit"])
MAX_MANIFEST_CHARS = 200_000


def _dash() -> Any:
    import dashboard

    return dashboard


def _page(request: Request, page: str, ctx: dict[str, Any], status: int = 200) -> Response:
    defaults = dict.fromkeys(("run", "run_error", "modes_error", "saved", "save_error", "yaml_draft", "error",
                              "distil", "manifest", "results", "fixed", "fix_error", "diag", "summary", "last",
                              "rules", "revoked"))
    return _dash()._render(request, "toolkit.html", {**defaults, "page": page, **ctx}, status_code=status,
                           nav="settings")


async def _post(request: Request, *, max_part: int = 2 * 1024 * 1024) -> tuple[Response | None, Any]:
    d = _dash()
    resp, method = d._guard(request)
    if resp is not None:
        return resp, None
    form = await request.form(max_files=50, max_part_size=max_part)
    if not d._csrf_ok(request, method, form):
        return d._render(request, "error.html", {"title": "Forbidden", "message": d._FLASH["csrf"][1]},
                         status_code=403), None
    return None, form


def _get_guard(request: Request) -> Response | None:
    resp, _ = _dash()._guard(request)
    return resp


# ---------------------------------------------------------------------------
# desktop modes
# ---------------------------------------------------------------------------

def _modes_ctx(**extra: Any) -> dict[str, Any]:
    from assistant import desktop_modes as dm

    body, sha = dm.read_note_text()
    try:
        yaml_text = dm.extract_yaml(body).strip()
    except dm.ModeError:
        yaml_text = ""
    try:
        modes, err = dm.parse_modes(body), None
    except dm.ModeError as exc:
        modes, err = [], str(exc)
    return {"modes": [m.to_dict() for m in modes], "modes_error": err, "yaml_text": yaml_text, "sha": sha,
            "windows": dm.is_windows(), "note_path": dm.NOTE_PATH, **extra}


@router.get("/modes", include_in_schema=False)
def modes_page(request: Request) -> Response:
    if (r := _get_guard(request)) is not None:
        return r
    return _page(request, "modes", _modes_ctx())


@router.post("/modes/run", include_in_schema=False)
async def modes_run(request: Request) -> Response:
    from assistant import desktop_modes as dm

    err, form = await _post(request)
    if err is not None:
        return err
    try:
        run = await run_in_threadpool(dm.run_mode, str(form.get("name") or ""))
        result = run.to_dict()
    except dm.ModeError as exc:
        return _page(request, "modes", _modes_ctx(run_error=str(exc)), 400)
    return _page(request, "modes", _modes_ctx(run=result))


@router.post("/modes/save", include_in_schema=False)
async def modes_save(request: Request) -> Response:
    from assistant import desktop_modes as dm
    from assistant import vault

    err, form = await _post(request)
    if err is not None:
        return err
    text = str(form.get("yaml") or "")
    try:
        dm.save_yaml(text, base_sha=str(form.get("sha") or "") or None)
    except dm.ModeError as exc:
        return _page(request, "modes", _modes_ctx(save_error=str(exc), yaml_draft=text), 400)
    except vault.NoteConflict:
        return _page(request, "modes", _modes_ctx(save_error="The note was edited elsewhere (Obsidian?) since you "
                                                             "opened this page. Reload and try again.",
                                                  yaml_draft=text), 409)
    return _page(request, "modes", _modes_ctx(saved=True))


# ---------------------------------------------------------------------------
# Claude history import
# ---------------------------------------------------------------------------

def _history_ctx(**extra: Any) -> dict[str, Any]:
    from assistant import history_import as hi

    return {"progress": hi.progress(), "downloads": str(hi.downloads_dir()), **extra}


@router.get("/history", include_in_schema=False)
def history_page(request: Request) -> Response:
    if (r := _get_guard(request)) is not None:
        return r
    return _page(request, "history", _history_ctx())


@router.post("/history/upload", include_in_schema=False)
async def history_upload(request: Request) -> Response:
    from assistant import history_import as hi
    from assistant.toolkit_settings import get_toolkit_settings

    err, form = await _post(request)
    if err is not None:
        return err
    cap = get_toolkit_settings().history_max_upload_mb * 1024 * 1024
    files = []
    for up in form.getlist("files"):
        if hasattr(up, "read"):
            data = await up.read(cap + 1)
            if len(data) > cap:
                return _page(request, "history", _history_ctx(error=f"{up.filename} is too large."), 400)
            if data:
                files.append((up.filename or "upload.zip", data))
    if not files:
        return _page(request, "history", _history_ctx(error="Choose one or more export files (.zip or .json)."), 400)
    results = await run_in_threadpool(hi.import_bytes, files)
    return _page(request, "history", _history_ctx(results=results))


@router.post("/history/manifest", include_in_schema=False)
async def history_manifest(request: Request) -> Response:
    from assistant import history_import as hi

    err, form = await _post(request)
    if err is not None:
        return err
    text = str(form.get("manifest") or "")[:MAX_MANIFEST_CHARS]
    try:
        report = await run_in_threadpool(hi.import_from_manifest, text)
    except hi.HistoryImportError as exc:
        return _page(request, "history", _history_ctx(error=str(exc)), 400)
    return _page(request, "history", _history_ctx(manifest=report))


@router.post("/history/distil", include_in_schema=False)
async def history_distil(request: Request) -> Response:
    from assistant import history_import as hi

    err, form = await _post(request)
    if err is not None:
        return err
    res = await run_in_threadpool(hi.distil_pending)
    return _page(request, "history", _history_ctx(distil=asdict(res)))


@router.get("/history/status.json", include_in_schema=False)
def history_status(request: Request) -> Response:
    d = _dash()
    resp, _ = d._guard(request, api=True)
    if resp is not None:
        return resp
    from assistant import history_import as hi

    p = hi.progress()
    return d._json({k: p[k] for k in ("conversations", "done", "skipped", "pending", "failed", "pct", "facts_added",
                                      "watch")})


# ---------------------------------------------------------------------------
# Twilio check
# ---------------------------------------------------------------------------

def _last_twilio() -> Any:
    import db

    v = db.kv_get("twilio_doctor:last")
    try:
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return None


@router.get("/twilio-check", include_in_schema=False)
def twilio_page(request: Request) -> Response:
    if (r := _get_guard(request)) is not None:
        return r
    return _page(request, "twilio", {"diag": None, "last": _last_twilio()})


@router.post("/twilio-check/run", include_in_schema=False)
async def twilio_run(request: Request) -> Response:
    from assistant import twilio_doctor as td

    err, form = await _post(request)
    if err is not None:
        return err
    d = await run_in_threadpool(td.diagnose)
    return _page(request, "twilio", {"diag": d.to_dict(), "summary": d.summary(), "last": _last_twilio()})


@router.post("/twilio-check/fix", include_in_schema=False)
async def twilio_fix(request: Request) -> Response:
    from assistant import twilio_doctor as td

    err, form = await _post(request)
    if err is not None:
        return err
    action = str(form.get("action") or "")
    try:
        msg = await run_in_threadpool(td.apply_fix, action)
    except td.FixError as exc:
        return _page(request, "twilio", {"diag": None, "fix_error": str(exc), "last": _last_twilio()}, 400)
    except Exception as exc:  # Twilio API error: show it, keep the page
        return _page(request, "twilio", {"diag": None, "fix_error": f"Twilio refused the change ({type(exc).__name__}: "
                                                                     f"{str(exc)[:200]})", "last": _last_twilio()}, 502)
    d = await run_in_threadpool(td.diagnose)
    return _page(request, "twilio", {"diag": d.to_dict(), "summary": d.summary(), "fixed": msg, "last": _last_twilio()})



# ---------------------------------------------------------------------------
# standing approvals
# ---------------------------------------------------------------------------

def _approvals_ctx(**extra: Any) -> dict[str, Any]:
    from assistant import approvals

    rules = approvals.list_rules(include_revoked=True)
    return {"rules": [r for r in rules if not r.get("revoked_at")],
            "old_rules": [r for r in rules if r.get("revoked_at")][:20], **extra}


@router.get("/approvals", include_in_schema=False)
def approvals_page(request: Request) -> Response:
    if (r := _get_guard(request)) is not None:
        return r
    return _page(request, "approvals", _approvals_ctx())


@router.post("/approvals/revoke", include_in_schema=False)
async def approvals_revoke(request: Request) -> Response:
    from assistant import approvals

    err, form = await _post(request)
    if err is not None:
        return err
    try:
        rid = int(str(form.get("rule_id") or ""))
    except ValueError:
        return _page(request, "approvals", _approvals_ctx(error="No such rule."), 400)
    ok = approvals.revoke_rule(rid, via="dashboard")
    return _page(request, "approvals", _approvals_ctx(revoked=rid if ok else None,
                                                      error=None if ok else "That rule was already revoked."))


# ---------------------------------------------------------------------------
# skills: the trusted skill installer (assistant/skill_catalog.py)
# ---------------------------------------------------------------------------

def _skills_ctx(q: str = "", *, refresh: bool = False, **extra: Any) -> dict[str, Any]:
    from assistant import skill_catalog as sc

    cat = sc.catalog(refresh=refresh)
    words = [w for w in q.lower().split() if w]
    entries = [e for e in cat["entries"]
               if all(w in f"{e['name']} {e['description']} {e.get('collection', '')}".lower() for w in words)]
    reviews = [sc.card(r) for r in sc.list_reviews()]
    return {"repos": cat["repos"], "entries": entries, "q": q, "installed": sc.installed(),
            "open_reviews": [r for r in reviews if r["status"] in sc.OPEN_STATUSES or r["status"] == "refused"
                             and not r.get("resolved_at")],
            "past_reviews": [r for r in reviews if r["status"] in ("installed", "rejected", "failed")][:8],
            "trusted": sc.trusted_repos(), **extra}


@router.get("/skills", include_in_schema=False)
async def skills_page(request: Request) -> Response:
    if (r := _get_guard(request)) is not None:
        return r
    q = str(request.query_params.get("q") or "")[:100]
    ctx = await run_in_threadpool(_skills_ctx, q)
    return _dash()._render(request, "skills.html", {"msg": None, "error": None, **ctx}, nav="settings")


async def _skills_action(request: Request, fn: Any) -> Response:
    from assistant import skill_catalog as sc

    err, form = await _post(request)
    if err is not None:
        return err
    try:
        msg = await run_in_threadpool(fn, form)
    except (sc.CatalogError, sc.sv.VettingError) as exc:
        page = await run_in_threadpool(_skills_ctx, "")
        return _dash()._render(request, "skills.html", {**page, "msg": None, "error": str(exc)}, status_code=400,
                               nav="settings")
    page = await run_in_threadpool(_skills_ctx, "", refresh=bool(form.get("refresh")))
    return _dash()._render(request, "skills.html", {**page, "msg": msg, "error": None}, nav="settings")


def _confirmed(form: Any) -> bool:
    return str(form.get("confirm") or "") in ("yes", "on", "1", "true")


@router.post("/skills/refresh", include_in_schema=False)
async def skills_refresh(request: Request) -> Response:
    return await _skills_action(request, lambda form: "Catalog re-read from GitHub.")


@router.post("/skills/review", include_in_schema=False)
async def skills_review(request: Request) -> Response:
    from assistant import skill_catalog as sc

    def act(form: Any) -> str:
        c = sc.request_install(str(form.get("name") or ""), str(form.get("repo") or "") or None,
                               requested_by="owner (dashboard)")
        if c["status"] == "awaiting_summary":
            sc.ask_cecil(c["id"])
            return f"{c['name']} is in quarantine and scanned. Cecil is reading it now; its card is below."
        return f"{c['name']}: review card below."
    return await _skills_action(request, act)


@router.post("/skills/summarise", include_in_schema=False)
async def skills_summarise(request: Request) -> Response:
    from assistant import skill_catalog as sc

    def act(form: Any) -> str:
        c = sc.get_review(str(form.get("review_id") or ""))
        sc.ask_cecil(c["id"])
        return f"Asked Cecil to read {c['name']} again."
    return await _skills_action(request, act)


@router.post("/skills/approve", include_in_schema=False)
async def skills_approve(request: Request) -> Response:
    from assistant import skill_catalog as sc

    def act(form: Any) -> str:
        c = sc.start_approve(str(form.get("review_id") or ""), via="dashboard", confirm=_confirmed(form))
        return (f"Installing {c['name']}" + (f" and {', '.join(c['pip_installs'])}" if c["pip_installs"] else "")
                + ": tests, then a commit. It starts restricted. Reload in a minute.")
    return await _skills_action(request, act)


@router.post("/skills/reject", include_in_schema=False)
async def skills_reject(request: Request) -> Response:
    from assistant import skill_catalog as sc

    return await _skills_action(request, lambda form: (sc.reject(str(form.get("review_id") or ""), via="dashboard"),
                                                       "Dismissed; the quarantined copy was deleted.")[1])


@router.post("/skills/promote", include_in_schema=False)
async def skills_promote(request: Request) -> Response:
    from assistant import skill_catalog as sc

    def act(form: Any) -> str:
        name = str(form.get("name") or "")
        sc.promote(name, via="dashboard", confirm=_confirmed(form))
        return f"{name} is promoted: its restrictions are lifted until its files change."
    return await _skills_action(request, act)


@router.post("/skills/restrict", include_in_schema=False)
async def skills_restrict(request: Request) -> Response:
    from assistant import skill_catalog as sc

    def act(form: Any) -> str:
        name = str(form.get("name") or "")
        sc.restrict(name, via="dashboard")
        return f"{name} is restricted again."
    return await _skills_action(request, act)


@router.post("/skills/uninstall", include_in_schema=False)
async def skills_uninstall(request: Request) -> Response:
    from assistant import skill_catalog as sc

    def act(form: Any) -> str:
        res = sc.uninstall(str(form.get("name") or ""), via="dashboard")
        return f"Uninstalled {res['uninstalled']} (archived; UNDO restores it). {res['commit_result']}"
    return await _skills_action(request, act)
