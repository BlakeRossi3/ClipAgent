"""Sterling's acceptance suite: "is everything working?" answered with real tool calls on real data.

Run it from the CLI (``python main.py assistant selfcheck``), the Settings page ("Run self-check")
or by asking Sterling (the ``selfcheck_run`` tool). Each line is a plain-English pass / warn / fail /
skip about one capability, exercised the way he uses it:

    hours      log hours for a past week, then undo it (admin undo)
    pay        read this week's pay from the ledger
    staff      rename a staff member and back (admin undo)
    voice      switch the voice and back (admin undo)
    history    read a past conversation
    memory     search memory
    open_url   open a URL - dry: the tool runs with a stand-in browser, nothing opens
    map        put a map panel on the Jarvis screen; its tiles pass the image proxy's guard
    studio     studio status
    render     render two seconds of an existing clip to a temporary file (no clips -> "no clips yet")
    upload     a DRY_RUN upload: the exact request YouTube would get, built and checked, nothing sent
    twilio     Twilio diagnose (read-only)
    usage      Claude plan usage

Safety: every change it makes goes through ``assistant.admin`` and is undone with ``app_undo``; it
never sends a text, a call or an upload, and it verifies that afterwards (notifications, outbound
messages and uploads are counted before and after, and any of its admin changes left un-undone is a
failure). Nothing here raises: a broken capability is a "fail" line.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import db

VIA = "selfcheck"
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
MAP_POINT = {"lat": 38.8977, "lon": -77.0365}       # a fixed, public point: no geocoding needed


@dataclass
class Line:
    check: str
    status: str
    detail: str
    ms: int = 0

    def text(self) -> str:
        return f"{self.status.upper():<4}  {self.check:<9} {self.detail}"


@dataclass
class Report:
    lines: list[Line] = field(default_factory=list)
    started: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not any(x.status == FAIL for x in self.lines)

    def counts(self) -> dict[str, int]:
        out = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for x in self.lines:
            out[x.status] = out.get(x.status, 0) + 1
        return out

    def summary(self) -> str:
        c = self.counts()
        head = "All checks passed" if self.ok and not c[WARN] else ("Passed with warnings" if self.ok else
                                                                   f"{c[FAIL]} check{'s' if c[FAIL] != 1 else ''} failed")
        return (f"{head}: {c[PASS]} passed, {c[WARN]} warnings, {c[FAIL]} failed, {c[SKIP]} skipped "
                f"in {self.seconds:.1f}s.")

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "summary": self.summary(), "counts": self.counts(), "started": self.started,
                "lines": [asdict(x) for x in self.lines]}

    def text(self) -> str:
        return "\n".join([x.text() for x in self.lines] + [self.summary()])


class Skip(Exception):
    pass


class Warn(Exception):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dispatch(name: str, args: dict[str, Any]) -> Any:
    """A real tool call through the registry, as Sterling makes it."""
    from assistant import tools

    tools.load_extensions()
    ctx = tools.ToolContext(channel="system", agent="main", source=VIA)
    res = tools.dispatch(name, args, ctx, allowed={name})
    if not res.ok:
        raise RuntimeError(res.content[:300])
    try:
        return json.loads(res.content)
    except ValueError:
        return res.content


def _undo(change_id: int) -> None:
    from assistant import admin

    admin.app_undo(change_id)


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {p: (p.read_bytes() if p.is_file() else None) for p in paths}


def _restore_exact(snap: dict[Path, bytes | None]) -> None:
    """After an undo, put the files back byte for byte (on Windows a text rewrite turns LF into CRLF, which
    would read as a change to his checkout)."""
    for p, data in snap.items():
        cur = p.read_bytes() if p.is_file() else None
        if cur == data:
            continue
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(data)


def _usd(x: Any) -> str:
    try:
        from assistant import ledger

        return ledger.fmt_usd(x, whole=False)
    except Exception:
        return f"${float(x or 0):.2f}"


# ---------------------------------------------------------------------------
# the checks (each returns its detail line; Skip / Warn / any other exception decide the status)
# ---------------------------------------------------------------------------

def check_hours() -> str:
    from assistant import admin, store

    jobs = store.list_jobs()
    if not jobs:
        raise Skip("No job is set up yet, so there are no hours to log.")
    job = jobs[0]
    week = store.week_start_of(db.local_today() - timedelta(days=21))
    before = store.get_work_log(job.id, week)
    hours = round((before.hours if before else 0) + 1.5, 2)
    res = admin.hours_log(hours, week=week.isoformat(), job=job.name, via=VIA)
    mid = store.get_work_log(job.id, week)
    _undo(res["change_id"])
    after = store.get_work_log(job.id, week)
    if mid is None or abs(mid.hours - hours) > 1e-6:
        raise RuntimeError("the hours did not land in the work log")
    if (after.hours if after else None) != (before.hours if before else None):
        raise RuntimeError("undo did not put the week back as it was")
    return f"Logged {hours:g} hours for the week of {week:%b %d} ({job.name}), then undid it."


def check_pay() -> str:
    from assistant import ledger

    s = ledger.summary(weeks=1)
    w = s["this_week"]
    if not s["jobs"]:
        raise Warn("No job or pay rate is set, so this week's pay reads as zero.")
    return f"This week's pay so far: {_usd(w.get('job_gross', 0))} for {w.get('hours', 0):g} hours."


def check_staff() -> str:
    from assistant import admin

    staff = [x["name"] for x in admin.staff_list()["staff"] if x["name"] != "main"]
    if not staff:
        raise Skip("There is no staff member besides Sterling to rename.")
    old = staff[0]
    new = f"{old}-check"
    agents_dir = admin._agents_dir()
    snap = _snapshot(sorted(agents_dir.glob("*.md")) + [agents_dir / f"{new}.md"])
    res = admin.staff_rename(old, new, via=VIA)
    names = {x["name"] for x in admin.staff_list()["staff"]}
    _undo(res["change_id"])
    _restore_exact(snap)
    back = {x["name"] for x in admin.staff_list()["staff"]}
    if new not in names:
        raise RuntimeError("the rename did not take")
    if old not in back or new in back:
        raise RuntimeError("undo did not restore the original name")
    return f"Renamed {old} to {new} and back."


def check_voice() -> str:
    from assistant import admin

    try:
        from voice.settings import KOKORO_VOICES, get_voice_settings, reset_voice_settings
    except ImportError:
        raise Skip("The voice package is not installed.") from None
    import config_store

    cur = get_voice_settings().tts_voice
    other = next(v for v in ("bm_lewis", "bm_george", "bm_daniel") if v != cur and v in KOKORO_VOICES)
    snap = _snapshot([config_store.path()])
    res = admin.config_set("VOICE_TTS_VOICE", other, via=VIA)
    reset_voice_settings()
    mid = get_voice_settings().tts_voice
    _undo(res["change_id"])
    _restore_exact(snap)
    config_store.reload()
    reset_voice_settings()
    after = get_voice_settings().tts_voice
    if mid != other:
        raise RuntimeError(f"the voice did not switch (still {mid})")
    if after != cur:
        raise RuntimeError(f"undo left the voice on {after}, not {cur}")
    return f"Switched the voice from {cur} to {other} and back."


def check_history() -> str:
    out = _dispatch("conversation_history", {"days": 0, "limit": 5})
    if not isinstance(out, dict):
        raise RuntimeError("unexpected answer")
    convs = out.get("conversations") or []
    if not convs and not out.get("claude_history"):
        raise Warn("The history reads, but there are no past conversations yet.")
    if not convs:
        return f"Read {len(out['claude_history'])} imported Claude chats (no conversations here yet)."
    last = convs[0]
    said = (last.get("owner_said") or last.get("sterling_said") or [""])[0]
    return (f"Read the {last['channel']} conversation of {last['date']} ({last['messages']} message{'s' if last['messages'] != 1 else ''})"
            + (f": \"{said[:60]}\"" if said else "") + ".")


def check_memory() -> str:
    import config

    q = config.get_settings().owner_name or "the"
    out = _dispatch("memory_search", {"query": q, "limit": 5})
    hits = out if isinstance(out, list) else []
    return f"Searched memory for \"{q}\": {len(hits)} note{'s' if len(hits) != 1 else ''} found."


def check_open_url() -> str:
    from assistant import tools

    opened: list[str] = []
    prev = tools._url_opener
    tools.set_url_opener(lambda u: opened.append(u) is None)
    try:
        out = _dispatch("open_url", {"url": "https://www.youtube.com/"})
    finally:
        tools.set_url_opener(prev if prev is not tools._default_url_opener else None)
    if opened != ["https://www.youtube.com/"] or not out.get("opened"):
        raise RuntimeError("the URL was not resolved")
    return "Resolved https://www.youtube.com/ for his browser (dry: nothing was opened)."


def check_map() -> str:
    from assistant import hud

    out = _dispatch("show_map", {**MAP_POINT, "zoom": 15, "title": "SELF-CHECK MAP", "hold": "turn"})
    z, lat, lon = 15, MAP_POINT["lat"], MAP_POINT["lon"]
    import math

    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    hud.vet_url(f"https://{hud.TILE_HOST}/{z}/{x}/{y}.png", resolve=False)
    if not out.get("on_screen"):
        raise RuntimeError("the panel was not published")
    return "A map panel is on the Jarvis screen, and its tiles pass the image proxy's guard."


def check_studio() -> str:
    out = _dispatch("studio_status", {"period_days": 7})
    if not isinstance(out, dict):
        raise RuntimeError("unexpected answer")
    return (f"Studio status read: {out.get('period_views', 0):,} views across {out.get('period_clips_tracked', 0)} "
            f"clips in 7 days; {out.get('uploads_today', 0)} of {out.get('upload_cap')} uploads used today.")


def _clip_with_source(statuses: tuple[str, ...]) -> Iterator[tuple[Any, Any]]:
    for c in reversed(db.list_clips(list(statuses))):   # newest first
        s = db.get_source(c.source_id)
        if s is not None:
            yield c, s


def check_render() -> str:
    import config
    import edit

    clips = db.list_clips(None, limit=1)
    if not clips:
        raise Skip("No clips yet.")
    pick = next(((c, s) for c, s in _clip_with_source(("rendered", "uploaded", "scheduled", "picked"))
                 if s.video_path and Path(s.video_path).is_file()), None)
    if pick is None:
        raise Skip("Clips exist, but none has its source video on this machine to render from.")
    if not shutil.which(config.get_settings().ffmpeg_bin) and not Path(config.get_settings().ffmpeg_bin).is_file():
        raise Skip("ffmpeg is not installed, so nothing can be rendered.")
    clip, src = pick
    s = config.get_settings()
    start = float(clip.start_sec)
    end = min(float(clip.end_sec), start + 2.0)
    with tempfile.TemporaryDirectory(prefix="clipagent-selfcheck-") as td:
        ass = Path(td) / "check.ass"
        out = Path(td) / "check.mp4"
        info = edit.probe_media(Path(src.video_path), s)
        ass.write_text(edit.build_ass([], start, edit.merged_caption_style(None), hook_text=clip.hook_text or "",
                                      credit_text=clip.credit_text or "", duration=end - start), encoding="utf-8")
        edit.run_ffmpeg(edit.build_ffmpeg_cmd(Path(src.video_path), ass, out, start, end, s, info=info), timeout=300)
        size = out.stat().st_size if out.is_file() else 0
    if size <= 0:
        raise RuntimeError("ffmpeg produced no file")
    return f"Rendered two seconds of clip {clip.id} ({size // 1024} KB) to a temporary file and deleted it."


def check_upload() -> str:
    import config
    import upload

    pick = next(_clip_with_source(("rendered", "scheduled", "uploaded", "picked")), None)
    if pick is None:
        raise Skip("No clips yet.")
    clip, src = pick
    s = config.get_settings().replace(dry_run=True)
    slots = upload.next_publish_slots(1)
    body = upload.build_metadata(clip, src, settings=s, publish_at=slots[0] if slots else None)
    title = body["snippet"]["title"]
    if "#shorts" not in (title + body["snippet"].get("description", "")).lower():
        raise RuntimeError("the request would not mark the video as a Short")
    when = db.parse_iso(db.to_iso(slots[0])).astimezone(db.local_tz()).strftime("%a %H:%M") if slots else "now"
    left = upload.strategy_mod.upload_cap(upload._load_strategy()) - upload.uploads_today()
    return (f"Dry run: clip {clip.id} \"{title[:50]}\" would go up {body['status']['privacyStatus']} at {when}; "
            f"{max(0, left)} upload slot{'s' if left != 1 else ''} left today. Nothing was sent.")


def check_twilio() -> str:
    out = _dispatch("twilio_diagnose", {})
    if not isinstance(out, dict):
        raise RuntimeError("unexpected answer")
    if not out.get("ok"):
        raise Warn("Twilio check ran: " + str(out.get("summary") or "problems found")[:200])
    return "Twilio check ran: " + str(out.get("summary") or "healthy")[:200]


def check_usage() -> str:
    out = _dispatch("usage_status", {})
    if not isinstance(out, dict):
        raise RuntimeError("unexpected answer")
    left = [f"{k} {v['left_pct']}% left" for k, v in (out.get("windows") or {}).items() if v.get("left_pct") is not None]
    line = f"{out.get('runs_today', 0)} of {out.get('runs_cap', 0)} runs today"
    if out.get("limited"):
        raise Warn(f"Plan usage read: limited until {out.get('limited_text') or 'later'}; {line}.")
    return "Plan usage read: " + (", ".join(left) + "; " if left else "") + line + "."


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("hours", check_hours), ("pay", check_pay), ("staff", check_staff), ("voice", check_voice),
    ("history", check_history), ("memory", check_memory), ("open_url", check_open_url), ("map", check_map),
    ("studio", check_studio), ("render", check_render), ("upload", check_upload), ("twilio", check_twilio),
    ("usage", check_usage),
]


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    with db.transaction() as c:
        for name, sql in (("notifications", "SELECT COUNT(*) FROM notifications WHERE status != 'dry_run'"),
                          ("uploads", "SELECT COUNT(*) FROM uploads"),
                          ("outbound", "SELECT COUNT(*) FROM as_messages WHERE direction = 'out' AND channel IN "
                                       "('sms', 'whatsapp', 'call')")):
            try:
                out[name] = int(c.execute(sql).fetchone()[0])
            except Exception:
                out[name] = -1
    return out


def run(only: list[str] | None = None) -> Report:
    """Run every check (or ``only`` those names). Never raises."""
    from assistant import admin, store

    store.init_store()
    rep = Report(started=db.now_iso())
    t0 = time.monotonic()
    before = _counts()
    seq_before = int(db.kv_get(admin.KV_SEQ, 0) or 0)
    for name, fn in CHECKS:
        if only and name not in only:
            continue
        s0 = time.monotonic()
        try:
            status, detail = PASS, fn()
        except Skip as exc:
            status, detail = SKIP, str(exc)
        except Warn as exc:
            status, detail = WARN, str(exc)
        except Exception as exc:  # a broken capability is a line, not a crash
            status, detail = FAIL, f"{type(exc).__name__}: {str(exc)[:240]}"
        rep.lines.append(Line(name, status, detail, round((time.monotonic() - s0) * 1000)))
    after = _counts()
    sent = [k for k in before if after.get(k, 0) > before[k] >= 0]
    left: list[int] = []
    for cid in range(seq_before + 1, int(db.kv_get(admin.KV_SEQ, 0) or 0) + 1):
        ch = admin.get_change(cid)
        if ch and ch.get("via") == VIA and not ch.get("undone"):
            left.append(cid)
    if sent or left:
        rep.lines.append(Line("safety", FAIL, ("Something left the building: " + ", ".join(sent) if sent else "")
                              + (f"; changes not undone: {left}" if left else "")))
    else:
        rep.lines.append(Line("safety", PASS, "No texts, calls or uploads were sent, and every change was undone."))
    rep.seconds = round(time.monotonic() - t0, 2)
    db.log_event("assistant", "self-check: " + rep.summary(), level="info" if rep.ok else "warning",
                 data={"counts": rep.counts(), "failed": [x.check for x in rep.lines if x.status == FAIL]})
    return rep


def register_tools(register: Callable[..., None]) -> None:
    from assistant.tools import Tool, _obj

    def _run(ctx: Any, a: dict[str, Any]) -> Any:
        only = a.get("only")
        return run(only if isinstance(only, list) and only else None).to_dict()

    register(Tool("selfcheck_run", "Run your own acceptance suite against his real app data, safely: log and undo "
                                   "hours, read pay, rename and restore a staff member, switch voice and back, read "
                                   "history, search memory, resolve a URL, show a map, studio status, render a test "
                                   "slice, a dry-run upload, Twilio diagnose and plan usage. Nothing is sent and every "
                                   "change is undone. Use when he asks whether everything is working; report the "
                                   "summary and any failures in a sentence or two.",
                  _obj({"only": {"type": "array", "items": {"type": "string", "enum": [n for n, _ in CHECKS]}}}),
                  _run, owner="assistant.selfcheck"), replace=True)


def main(argv: list[str] | None = None) -> int:
    rep = run()
    print(rep.text())
    return 0 if rep.ok else 1
