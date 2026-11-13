"""Weekly strategy review: stats digest -> one Sonnet call -> guarded strategy update.

Flow of :func:`weekly_review`:

1. Build a compact, plain-text **digest** of the period in code (:func:`build_digest`):
   per-clip rows, aggregates by topic / source type / posting slot / length bucket /
   hook style, channel trend, LLM spend and cost per 1k views, upload outcomes,
   source pipeline counts and the running experiment's control-vs-variant
   comparison with sample sizes. The model never sees raw tables. The digest is
   capped at :data:`MAX_DIGEST_CHARS`.
2. **Cold start**: with too little data (:data:`MIN_TRACKED_CLIPS` /
   :data:`MIN_PERIOD_VIEWS`) the LLM is not called at all; the report says so.
3. One Sonnet call (``purpose="strategist.review"``). The system prompt is the
   stable instructions plus the current strategy (cached); the digest goes in
   the user turn. Structured output ``{"report", "strategy", "experiment"}``.
4. **Guardrails in plain code** (:func:`check_proposal`): merge onto the current
   strategy, clamp numeric fields, enforce exactly one well-formed experiment,
   hard-refuse licence-unsafe allowlist edits (:func:`licence_problems`), then
   ``strategy.validate_strategy``. ``strategy.diff_major_changes`` decides what is
   major (niche, topic areas, source types, budget, any allowlist/blocklist edit).
5. Invalid proposal -> keep the current strategy, log an ``error`` event, notify.
   No major changes -> ``strategy.save_strategy`` now. Major changes ->
   ``notify.request_approval`` and nothing is saved until :func:`apply_decision`
   runs on the approved decision.
6. The (speakable) report goes out via ``notify.notify("weekly_report", ...)``.

``weekly_review`` does not raise for budget, model or validation problems: it
logs, notifies and returns a :class:`StrategyReview` with ``applied_version=None``.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import budget
import db
import llm
import strategy as strategy_mod
from config import get_settings
from strategy import MajorChange, Strategy

log = logging.getLogger(__name__)

MODULE = "strategist"
PURPOSE = "strategist.review"

MAX_DIGEST_CHARS = 12_000     # hard cap on the user-turn digest (~3k tokens)
MAX_CLIP_ROWS = 40            # per-clip rows shown before trimming to best/worst
MIN_TRACKED_CLIPS = 3         # fewer clips with stats than this -> cold start, no LLM call
MIN_PERIOD_VIEWS = 50         # fewer views in the period than this -> cold start
MIN_ARM_SAMPLES = 5           # experiment arms below this are "too thin to conclude"
REVIEW_MAX_TOKENS = 8192      # full strategy echo + report + adaptive-thinking headroom
MAX_REPORT_CHARS = 1200

SHORTS_MAX_SEC = 180          # YouTube Shorts length limit (also strategy schema limit)
MIN_CLIP_SEC = 5
MAX_EXPERIMENT_DAYS = 28
MAX_WINDOWS_TO_ESCALATE = 10
MAX_CLIPS_PER_SOURCE = 5
MIN_WINDOWS_PER_SOURCE = 5
MAX_WINDOWS_PER_SOURCE = 100

# Publishers that must never be admitted whatever the model says (see licence policy).
NEVER_ALLOW = ("c-span", "cspan", "forbes", "associated press", "reuters", "fox news", "cnn", "nbc",
               "abc news", "cbs news", "msnbc", "pbs newshour", "bloomberg", "guardian news")
YOUTUBE_DOMAINS = ("youtube.com", "youtu.be")

EXPERIMENT_VARIABLES = ("hook_style", "title_style", "caption_style", "clip_length", "posting_time",
                        "topic_selection", "source_type")
SUCCESS_METRICS = ("avg_view_pct", "avg_view_duration_sec", "views", "subscribers_gained",
                   "likes_per_1k_views")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class StrategyReview:
    report: str
    proposed: dict
    major_changes: list[MajorChange]
    decision_id: int | None
    applied_version: int | None
    # extensions (not in the original contract; defaults keep it compatible)
    status: str = "unchanged"   # cold_start | applied | unchanged | approval_requested | invalid | budget | llm_error
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class StrategistError(RuntimeError):
    """Raised only for programming/IO errors; review problems are reported, not raised."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(v: Any, nd: int = 1) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if math.isnan(v):
            return "-"
        if abs(v) >= 100 or v == int(v):
            return str(int(round(v)))
        return f"{v:.{nd}f}"
    return str(v)


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(statistics.median(xs)) if xs else None


def _clean(text: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip().replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def hook_style_of(hook: str | None) -> str:
    """Classify hook text in code: question | number | claim | none."""
    h = (hook or "").strip()
    if not h:
        return "none"
    first = h.split()[0].lower().strip("\"'")
    if h.endswith("?") or first in {"what", "why", "how", "who", "when", "where", "which", "is", "are",
                                    "can", "could", "would", "did", "do", "does", "will", "should"}:
        return "question"
    if re.search(r"\d", h):
        return "number"
    return "claim"


def length_bucket(sec: float) -> str:
    if sec < 30:
        return "<30s"
    if sec < 45:
        return "30-44s"
    if sec < 60:
        return "45-59s"
    return "60s+"


def _strip_comments(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------

def _slot(row: Mapping[str, Any], tz: ZoneInfo) -> str:
    iso = row.get("publish_at") or row.get("uploaded_at")
    if not iso:
        return "?"
    return db.parse_iso(iso).astimezone(tz).strftime("%H:%M")


def collect_clip_rows(since_day: date, strategy: Mapping[str, Any],
                      until_day: date | None = None) -> list[dict[str, Any]]:
    """Per-clip rows with the stats earned in ``[since_day, until_day]``, enriched for the digest.

    Clips uploaded before the period are included when they earned views in it.
    """
    tz = ZoneInfo(strategy.get("timezone") or get_settings().timezone)
    rows = []
    for r in db.clip_performance(since_day, until_day):
        length = float(r["end_sec"] - r["start_sec"])
        rows.append({
            **r,
            "topic": r["topic"] or "?",
            "length_sec": length,
            "length_bucket": length_bucket(length),
            "hook_style": hook_style_of(r.get("hook_text")),
            "slot": _slot(r, tz),
            "subs_net": int(r["subscribers_gained"]) - int(r["subscribers_lost"]),
        })
    return rows


def _aggregate(rows: list[dict[str, Any]], key: str) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[str(r[key])].append(r)
    out = []
    for name, rs in sorted(groups.items(), key=lambda kv: -sum(x["views"] for x in kv[1])):
        views = [x["views"] for x in rs]
        out.append(f"{_clean(name, 40)} | n={len(rs)} | views={sum(views)} | median={_num(_median(views))}"
                   f" | avg%={_num(_mean([x['avg_view_pct'] for x in rs]))}"
                   f" | subs={sum(x['subscribers_gained'] for x in rs)}")
    return out


def _metric(rows: list[dict[str, Any]], metric: str) -> float | None:
    if not rows:
        return None
    if metric == "likes_per_1k_views":
        v = sum(r["views"] for r in rows)
        return (sum(r["likes"] for r in rows) / v * 1000) if v else None
    return _mean([r.get(metric) for r in rows])


def experiment_summary(strategy: Mapping[str, Any]) -> dict[str, Any] | None:
    """Control-vs-variant comparison for the strategy's experiment (all uploads so far)."""
    exp = strategy.get("experiment")
    if not exp:
        return None
    metric = exp.get("success_metric") or "avg_view_pct"
    rows = [r for r in db.clip_performance(None) if r.get("experiment_id") == exp["id"]]
    arms = {}
    for arm in ("control", "variant"):
        rs = [r for r in rows if r.get("experiment_variant") == arm]
        arms[arm] = {"n": len(rs), "metric": _metric(rs, metric),
                     "mean_views": _mean([r["views"] for r in rs])}
    c, v = arms["control"], arms["variant"]
    if min(c["n"], v["n"]) < MIN_ARM_SAMPLES:
        verdict = f"too thin to conclude (need >= {MIN_ARM_SAMPLES} clips per arm)"
        lift = None
    elif not c["metric"] or v["metric"] is None:
        verdict, lift = "no usable metric values yet", None
    else:
        lift = (v["metric"] - c["metric"]) / abs(c["metric"])
        verdict = "inconclusive (<10% difference)" if abs(lift) < 0.10 else (
            "variant ahead" if lift > 0 else "control ahead")
    today = db.local_today().isoformat()
    state = "not started" if today < exp["start"] else ("ended" if today > exp["end"] else "running")
    return {"id": exp["id"], "variable": exp.get("variable"), "metric": metric, "state": state,
            "start": exp["start"], "end": exp["end"], "arms": arms, "lift": lift, "verdict": verdict}


def _spend_by_purpose(since_day: date, today: date) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    d = since_day
    while d <= today:
        for r in db.llm_usage_summary(d):
            out[r["purpose"]] += float(r["cost_usd"] or 0)
        d += timedelta(days=1)
    return dict(out)


def gather(days: int = 7, strategy: Strategy | None = None) -> dict[str, Any]:
    """All numbers the digest is built from (also used for the cold-start check)."""
    strat = strategy or strategy_mod.load_strategy()
    today = db.local_today()
    since = today - timedelta(days=days)
    start_iso = db.local_day_bounds(since)[0]
    end_iso = db.local_day_bounds(today)[1]
    rows = collect_clip_rows(since, strat)
    prev_rows = db.clip_performance(since - timedelta(days=days), since - timedelta(days=1))
    period_views = sum(s.views for s in db.stats_between(since, today))
    spend = db.llm_spend_between(start_iso, end_iso)
    uploads = [u for u in db.list_uploads(include_dry_run=True) if u.created_at >= start_iso]
    up_counts: dict[str, int] = defaultdict(int)
    for u in uploads:
        up_counts[("dry_run_" if u.dry_run else "") + u.status] += 1
    channel = db.list_channel_stats(limit=days + 1)
    return {
        "strategy": strat, "days": days, "since": since, "today": today, "rows": rows,
        "prev_rows": prev_rows, "period_views": period_views, "spend": spend,
        "spend_by_purpose": _spend_by_purpose(since, today), "uploads": dict(up_counts),
        "channel": channel, "experiment": experiment_summary(strat),
        "pipeline": db.count_sources_by_status(),
        "tracked_clips": sum(1 for r in rows if r["days_tracked"] > 0),
        "uploaded_in_period": sum(1 for r in rows if r["uploaded_at"] >= start_iso),
    }


def build_digest(data: Mapping[str, Any] | None = None, *, days: int = 7,
                 max_chars: int = MAX_DIGEST_CHARS) -> str:
    """Compact plain-text digest for the model (never raw tables). Capped at ``max_chars``."""
    d = data if data is not None else gather(days)
    rows: list[dict[str, Any]] = d["rows"]
    head: list[str] = [f"PERIOD {d['since'].isoformat()}..{d['today'].isoformat()} ({d['days']} days, local dates;"
                       " analytics lag ~2 days so the newest clips have little data)"]

    # overview + trend
    views = sum(r["views"] for r in rows)
    prev = d["prev_rows"]
    head.append(f"OVERVIEW clips_active={len(rows)} uploaded_in_period={d.get('uploaded_in_period', len(rows))} "
                f"clips_with_stats={d['tracked_clips']} "
                f"period_views_of_these={views} views_in_period_all_videos={d['period_views']} "
                f"subs_gained={sum(r['subscribers_gained'] for r in rows)} "
                f"copyright_claims={sum(1 for r in rows if r['copyright_claim'])}")
    if prev:
        head.append(f"PREVIOUS PERIOD clips={len(prev)} median_views={_num(_median([r['views'] for r in prev]))} "
                    f"(this period median={_num(_median([r['views'] for r in rows]))})")
    ch = d["channel"]
    if ch:
        first, last = ch[0], ch[-1]
        sub_d = (last.subscribers - first.subscribers) if None not in (last.subscribers, first.subscribers) else None
        view_d = (last.total_views - first.total_views) if None not in (last.total_views, first.total_views) else None
        head.append(f"CHANNEL {first.day}->{last.day} subscribers {_num(first.subscribers)}->{_num(last.subscribers)}"
                    f" (Δ{_num(sub_d)}) total_views Δ{_num(view_d)} videos={_num(last.video_count)}")
    else:
        head.append("CHANNEL no channel stats yet")

    spend = d["spend"]
    per_k = f"${spend / d['period_views'] * 1000:.3f}" if d["period_views"] else "n/a"
    by_p = ", ".join(f"{p}=${c:.3f}" for p, c in sorted(d["spend_by_purpose"].items(), key=lambda x: -x[1]))
    head.append(f"LLM SPEND ${spend:.3f} in period, cost per 1k views {per_k}; by purpose: {by_p or 'none'}")
    up = d["uploads"]
    head.append("UPLOADS " + (", ".join(f"{k}={v}" for k, v in sorted(up.items())) or "none") +
                f" | sources by status: " + ", ".join(f"{k}={v}" for k, v in sorted(d["pipeline"].items())))

    exp = d["experiment"]
    if exp:
        a = exp["arms"]
        lift = "-" if exp["lift"] is None else f"{exp['lift'] * 100:+.0f}%"
        head.append(
            f"EXPERIMENT {exp['id']} variable={exp['variable']} {exp['state']} {exp['start']}..{exp['end']} "
            f"metric={exp['metric']}: control n={a['control']['n']} {exp['metric']}={_num(a['control']['metric'])}; "
            f"variant n={a['variant']['n']} {exp['metric']}={_num(a['variant']['metric'])}; lift={lift}; "
            f"verdict: {exp['verdict']}")
    else:
        head.append("EXPERIMENT none configured")

    aggs: list[str] = []
    if rows:
        for title, key in (("BY TOPIC", "topic"), ("BY SOURCE TYPE", "source_type"),
                           ("BY POSTING SLOT (local)", "slot"), ("BY LENGTH", "length_bucket"),
                           ("BY HOOK STYLE", "hook_style")):
            aggs.append(title + " (group | n | views | median views | avg view % | subs gained)")
            aggs += ["  " + x for x in _aggregate(rows, key)]

    def clip_lines(rs: list[dict[str, Any]]) -> list[str]:
        out = ["CLIPS (title | topic | type | len s | hook | slot | arm | views | avg% | avg dur s | likes | subs | days)"]
        for r in rs:
            out.append(" | ".join([
                _clean(r["title"], 60), _clean(r["topic"], 30), r["source_type"], _num(r["length_sec"], 0),
                _clean(r["hook_text"], 50) or "-", r["slot"], r.get("experiment_variant") or "-",
                str(r["views"]), _num(r["avg_view_pct"]), _num(r["avg_view_duration_sec"]), str(r["likes"]),
                str(r["subscribers_gained"]), str(r["days_tracked"]),
            ]))
        return out

    ranked = sorted(rows, key=lambda r: -r["views"])
    shown = ranked
    omitted = 0
    if len(ranked) > MAX_CLIP_ROWS:
        half = MAX_CLIP_ROWS // 2
        shown, omitted = ranked[:half] + ranked[-half:], len(ranked) - 2 * half

    def render(clips: list[dict[str, Any]], omitted_n: int) -> str:
        parts = head + aggs
        if clips:
            parts += clip_lines(clips)
        if omitted_n:
            parts.append(f"({omitted_n} middle-ranked clips omitted; aggregates include all clips)")
        return "\n".join(parts)

    text = render(shown, omitted)
    while len(text) > max_chars and shown:
        # drop clips from the middle of the ranking first (best and worst are most informative)
        mid = len(shown) // 2
        shown = shown[:mid] + shown[mid + 1:]
        omitted += 1
        text = render(shown, omitted)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n(digest truncated)"
    return text


# ---------------------------------------------------------------------------
# model I/O
# ---------------------------------------------------------------------------

INSTRUCTIONS = """\
You are the strategist for a small, fully automated YouTube Shorts channel. Once a week you read a \
performance digest and decide how next week's strategy should change. The pipeline clips footage that is \
safe to reuse - US federal government works (NASA, official House and Senate committee feeds) and CC BY \
YouTube videos - and plain code does everything else using the strategy you return. Your proposal is \
applied automatically when it contains only minor changes; for anything major the owner is phoned and \
must approve it first.

How to decide:
- Be evidence-led. Base every change on numbers in the digest. Shorts metrics are noisy: with fewer than \
about five clips in a group, or clips only a day or two old, treat differences as noise. When the data is \
too thin to conclude, say so plainly and change little or nothing - returning the strategy unchanged is a \
good answer.
- Make at most a few changes per week so next week's digest can show what each one did.
- Run exactly one experiment, varying one variable, with a clear success metric and an end date. If the \
current experiment is still running, return it unchanged (same id and fields) unless it already has a \
clear result. A new experiment gets a new id, a start date no earlier than today, an end date 7 to 21 days \
later, uses the current setting as control, and changes only that one variable.
- Licence safety is fixed, not something to trade for growth. Keep source types within the current \
licence-safe set and return the allowlist and blocklist exactly as given. Never add channels, domains or \
any_channel to the allowlist and never remove blocklist entries; news outlets and C-SPAN re-uploads are \
not government works. If you think a publisher should be added, say so in the report instead.
- These changes phone the owner and need strong evidence: changing the niche, adding a topic area, adding \
a source type, or raising daily_llm_budget_usd. Do not raise the budget to do more of the same; prefer \
cutting low-value spend.
- Topic queries are search terms. You may add, remove or reword them within existing topic areas; drop \
ones that keep underperforming.
- posting_times are local "HH:MM" in the strategy timezone and uploads_per_day may not exceed their count \
or the limit given in the digest. clip_length_sec must satisfy 0 < min <= target <= max <= 180.

The report is read aloud to the owner over the phone. Write three to six short spoken sentences in plain \
words: how the week went (one or two rounded numbers), what you changed and why, and where the experiment \
stands. No markdown, lists, URLs or ids; under 900 characters.

Return the complete strategy with every field, including unchanged ones. "allowlist" is a list with one \
entry per source type."""


def editable_view(strategy: Mapping[str, Any]) -> dict[str, Any]:
    """The strategy as the model sees and returns it: no version/updated_at/experiment/comments,
    allowlist as a list of ``{source_type, any_channel, channels, domains}``."""
    s = _strip_comments(dict(strategy))
    allow = []
    for st, entry in s.get("allowlist", {}).items():
        allow.append({
            "source_type": st,
            "any_channel": bool(entry.get("any_channel", False)),
            "channels": [{"name": c.get("name", ""), "handle": c.get("handle"), "channel_id": c.get("channel_id"),
                          "note": c.get("note") or ""} for c in entry.get("channels", [])],
            "domains": list(entry.get("domains", [])),
        })
    out = {k: v for k, v in s.items() if k not in ("version", "updated_at", "experiment", "allowlist")}
    out["allowlist"] = allow
    out.setdefault("default_hashtags", [])
    out.setdefault("notes", "")
    return out


def _from_view(view: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the model's editable view onto ``current`` (keeps version, comments, unknown-to-model keys)."""
    merged = json.loads(json.dumps(dict(current)))
    for k, v in view.items():
        if k == "allowlist":
            continue
        merged[k] = json.loads(json.dumps(v))
    if "allowlist" in view:
        old_allow = current.get("allowlist", {})
        new_allow: dict[str, Any] = {k: v for k, v in old_allow.items() if k.startswith("_")}
        for entry in view["allowlist"]:
            e = dict(entry)
            st = e.pop("source_type")
            new_allow[st] = e
        merged["allowlist"] = new_allow
    return merged


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

EXPERIMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": _STR, "hypothesis": _STR,
        "variable": {"type": "string", "enum": list(EXPERIMENT_VARIABLES)},
        "control": _STR, "variant": _STR,
        "start": {"type": "string", "description": "YYYY-MM-DD local"},
        "end": {"type": "string", "description": "YYYY-MM-DD local"},
        "success_metric": {"type": "string", "enum": list(SUCCESS_METRICS)},
    },
    "required": ["id", "hypothesis", "variable", "control", "variant", "start", "end", "success_metric"],
}

STRATEGY_VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "niche": _STR,
        "topic_areas": _STR_LIST,
        "topics": {"type": "array", "items": {
            "type": "object",
            "properties": {"query": _STR, "area": _STR, "source_types": _STR_LIST},
            "required": ["query", "area", "source_types"]}},
        "source_types": _STR_LIST,
        "allowlist": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "source_type": _STR, "any_channel": {"type": "boolean"},
                "channels": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"name": _STR, "handle": _NULLABLE_STR, "channel_id": _NULLABLE_STR, "note": _STR},
                    "required": ["name", "handle", "channel_id", "note"]}},
                "domains": _STR_LIST},
            "required": ["source_type", "any_channel", "channels", "domains"]}},
        "blocklist_channels": _STR_LIST,
        "hook_style": _STR,
        "title_style": _STR,
        "caption_style": {
            "type": "object",
            "properties": {
                "font": _STR, "font_size": {"type": "integer"}, "primary_color": _STR, "highlight_color": _STR,
                "outline_color": _STR, "outline_px": {"type": "integer"}, "max_words_per_line": {"type": "integer"},
                "position": {"type": "string", "enum": ["top", "center", "lower_third"]},
                "uppercase": {"type": "boolean"},
                "animation": {"type": "string", "enum": ["word_pop", "karaoke", "none"]},
                "framing": {"type": "string", "enum": list(strategy_mod.CAPTION_STYLE_CHOICES["framing"])}},
            "required": ["font", "font_size", "primary_color", "highlight_color", "outline_color", "outline_px",
                         "max_words_per_line", "position", "uppercase", "animation"]},
        "clip_length_sec": {"type": "object",
                            "properties": {"min": {"type": "number"}, "target": {"type": "number"},
                                           "max": {"type": "number"}},
                            "required": ["min", "target", "max"]},
        "min_haiku_score": {"type": "number"},
        "windows_to_escalate": {"type": "integer"},
        "max_clips_per_source": {"type": "integer"},
        "max_windows_per_source": {"type": "integer"},
        "uploads_per_day": {"type": "integer"},
        "posting_times": _STR_LIST,
        "timezone": _STR,
        "daily_llm_budget_usd": {"type": "number"},
        "default_hashtags": _STR_LIST,
        "notes": _STR,
    },
    "required": ["niche", "topic_areas", "topics", "source_types", "allowlist", "blocklist_channels",
                 "hook_style", "title_style", "caption_style", "clip_length_sec", "min_haiku_score",
                 "windows_to_escalate", "max_clips_per_source", "uploads_per_day", "posting_times", "timezone",
                 "daily_llm_budget_usd", "default_hashtags", "notes"],
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "report": {"type": "string", "maxLength": 1500},
        "strategy": STRATEGY_VIEW_SCHEMA,
        "experiment": EXPERIMENT_SCHEMA,
    },
    "required": ["report", "strategy", "experiment"],
}


def system_blocks(strategy: Mapping[str, Any]) -> list[str]:
    """Stable system prompt: instructions + current strategy (cached by llm.complete)."""
    view = editable_view(strategy)
    exp = strategy.get("experiment")
    block = (f"CURRENT STRATEGY (version {int(strategy['version'])}):\n"
             + json.dumps(view, ensure_ascii=False, indent=1)
             + "\n\nCURRENT EXPERIMENT:\n" + json.dumps(exp, ensure_ascii=False, indent=1))
    return [INSTRUCTIONS, block]


def user_prompt(digest: str, strategy: Mapping[str, Any]) -> str:
    s = get_settings()
    return (f"Today is {db.local_today().isoformat()} (local). Limits: uploads_per_day <= "
            f"{s.max_uploads_per_day}; daily_llm_budget_usd hard ceiling {s.max_daily_llm_budget_usd:.2f}; "
            f"experiments last at most {MAX_EXPERIMENT_DAYS} days.\n\n"
            f"<digest>\n{digest}\n</digest>\n\n"
            "Review the week and return the report, the full strategy and the one experiment.")


# ---------------------------------------------------------------------------
# guardrails
# ---------------------------------------------------------------------------

@dataclass
class ProposalCheck:
    strategy: dict[str, Any] | None   # merged, clamped, ready for save_strategy (None if invalid)
    problems: list[str]
    notes: list[str]                   # clamps/normalisations applied
    major: list[MajorChange]


def licence_problems(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """Licence-unsafe allowlist / blocklist edits that are refused outright, approval or not.

    (``any_channel`` on a government source type is rejected by ``strategy.validate_strategy``;
    every *other* allowlist / blocklist / source-type edit is a major change found by
    ``strategy.diff_major_changes`` and goes to the owner.)
    """
    hard: list[str] = []
    na = {k: v for k, v in new.get("allowlist", {}).items() if not k.startswith("_")}
    old_block = {b.casefold() for b in old.get("blocklist_channels", [])}
    new_block = {b.casefold() for b in new.get("blocklist_channels", [])}
    for st, entry in na.items():
        if st == "cc_by_youtube":
            doms = [d.lower() for d in entry.get("domains", [])]
            if any(d not in YOUTUBE_DOMAINS for d in doms):
                hard.append("allowlist[cc_by_youtube] may only list YouTube domains")
        for c in entry.get("channels", []):
            nm = (c.get("name") or "").casefold()
            if nm in old_block or nm in new_block or any(bad in nm for bad in NEVER_ALLOW):
                hard.append(f"allowlist[{st}] adds blocklisted/rebroadcaster channel {c.get('name')!r}")
        for dom in entry.get("domains", []):
            dl = dom.lower()
            if st != "cc_by_youtube" and any(dl == y or dl.endswith("." + y) for y in YOUTUBE_DOMAINS):
                hard.append(f"allowlist[{st}] adds YouTube domain {dom!r} (would admit any uploader)")
    removed_block = sorted(old_block - new_block)
    if removed_block:
        hard.append(f"blocklist entries removed: {removed_block}")
    return hard


def _check_experiment(exp: Any, current: Mapping[str, Any], today: date, problems: list[str],
                      notes: list[str]) -> dict[str, Any] | None:
    if not isinstance(exp, Mapping):
        problems.append("exactly one experiment object is required")
        return None
    missing = [k for k in strategy_mod.EXPERIMENT_KEYS if k not in exp]
    if missing:
        problems.append(f"experiment missing {missing}")
        return None
    exp = {k: exp[k] for k in strategy_mod.EXPERIMENT_KEYS}
    cur = current.get("experiment") or {}
    if cur and exp["id"] == cur.get("id"):
        changed = [k for k in strategy_mod.EXPERIMENT_KEYS if k != "end" and exp[k] != cur.get(k)]
        if changed:
            notes.append(f"kept running experiment {cur['id']} unchanged (model edited {changed})")
            exp = {**{k: cur.get(k) for k in strategy_mod.EXPERIMENT_KEYS}, "end": exp["end"]}
    else:
        used = {(v.json or {}).get("experiment", {}).get("id") for v in db.list_strategy_versions(limit=200)
                if isinstance(v.json, Mapping) and v.json.get("experiment")}
        if exp["id"] in used:
            problems.append(f"experiment id {exp['id']!r} was already used; a new experiment needs a new id")
        if isinstance(exp["start"], str) and exp["start"] < today.isoformat():
            problems.append("a new experiment cannot start in the past")
    if exp.get("variable") not in EXPERIMENT_VARIABLES:
        problems.append(f"experiment.variable must be one of {list(EXPERIMENT_VARIABLES)}")
    if exp.get("success_metric") not in SUCCESS_METRICS:
        problems.append(f"experiment.success_metric must be one of {list(SUCCESS_METRICS)}")
    if not str(exp.get("control", "")).strip() or not str(exp.get("variant", "")).strip():
        problems.append("experiment needs non-empty control and variant")
    elif str(exp["control"]).strip() == str(exp["variant"]).strip():
        problems.append("experiment control and variant are identical")
    try:
        s, e = date.fromisoformat(exp["start"]), date.fromisoformat(exp["end"])
    except (TypeError, ValueError):
        problems.append("experiment start/end must be YYYY-MM-DD")
        return exp
    if e < s:
        problems.append("experiment ends before it starts")
    elif (e - s).days + 1 > MAX_EXPERIMENT_DAYS:
        problems.append(f"experiment longer than {MAX_EXPERIMENT_DAYS} days")
    if e < today:
        if cur and exp["id"] == cur.get("id"):
            notes.append(f"experiment {exp['id']} has ended and no new experiment was proposed")
        else:
            problems.append("experiment end date is in the past")
    return exp


def _clamp(new: dict[str, Any], current: Mapping[str, Any], notes: list[str], problems: list[str]) -> None:
    s = get_settings()
    # posting times: valid HH:MM only, unique, sorted
    pts = new.get("posting_times")
    if isinstance(pts, list):
        good = sorted({p for p in pts if isinstance(p, str) and _HHMM.match(p)})
        if len(good) != len(pts):
            notes.append(f"dropped invalid/duplicate posting times {[p for p in pts if p not in good or pts.count(p) > 1]}")
        if not good:
            problems.append("no valid posting times")
        new["posting_times"] = good
    # uploads/day <= config max and <= slots
    upd = new.get("uploads_per_day")
    if isinstance(upd, int) and not isinstance(upd, bool):
        cap = min(s.max_uploads_per_day, len(new.get("posting_times") or []))
        v = max(0, min(upd, cap))
        if v != upd:
            notes.append(f"uploads_per_day clamped {upd} -> {v}")
        new["uploads_per_day"] = v
    # clip lengths within Shorts limits, ordered
    cl = new.get("clip_length_sec")
    if isinstance(cl, Mapping) and all(isinstance(cl.get(k), (int, float)) and not isinstance(cl.get(k), bool)
                                       for k in ("min", "target", "max")):
        vals = [min(max(float(cl[k]), MIN_CLIP_SEC), SHORTS_MAX_SEC) for k in ("min", "target", "max")]
        vals.sort()
        fixed = {k: (int(v) if v == int(v) else round(v, 1)) for k, v in zip(("min", "target", "max"), vals)}
        if fixed != {k: cl[k] for k in ("min", "target", "max")}:
            notes.append(f"clip_length_sec normalised {dict(cl)} -> {fixed}")
        new["clip_length_sec"] = fixed
    # scores / counts
    for key, lo, hi in (("min_haiku_score", 1, 10), ("windows_to_escalate", 1, MAX_WINDOWS_TO_ESCALATE),
                        ("max_clips_per_source", 1, MAX_CLIPS_PER_SOURCE),
                        ("max_windows_per_source", MIN_WINDOWS_PER_SOURCE, MAX_WINDOWS_PER_SOURCE)):
        v = new.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            c = min(max(v, lo), hi)
            if c != v:
                notes.append(f"{key} clamped {v} -> {c}")
            new[key] = c
    # budget: never above the configured hard ceiling (increase itself is flagged major by diff)
    b = new.get("daily_llm_budget_usd")
    if isinstance(b, (int, float)) and not isinstance(b, bool):
        c = min(float(b), s.max_daily_llm_budget_usd)
        if c <= 0:
            problems.append("daily_llm_budget_usd must be > 0")
        elif c != b:
            notes.append(f"daily_llm_budget_usd clamped {b} -> {c}")
            new["daily_llm_budget_usd"] = c


def check_proposal(current: Strategy, view: Mapping[str, Any], experiment: Any) -> ProposalCheck:
    """Plain-code guardrails on the model's proposal. Pure apart from reading strategy history."""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(view, Mapping):
        return ProposalCheck(None, ["strategy must be an object"], notes, [])
    try:
        new = _from_view(view, current)
    except (KeyError, TypeError, AttributeError) as exc:
        return ProposalCheck(None, [f"malformed strategy: {exc}"], notes, [])
    new["version"] = current.version
    new["updated_at"] = current["updated_at"]
    if new.get("timezone") != current.get("timezone"):
        # posting slots, budgets and caps are reasoned about in one local day; not the model's call
        notes.append(f"kept timezone {current.get('timezone')!r} (model proposed {new.get('timezone')!r})")
        new["timezone"] = current.get("timezone")
    _clamp(new, current, notes, problems)
    new["experiment"] = _check_experiment(experiment, current, db.local_today(), problems, notes)
    problems += licence_problems(current, new)
    problems += strategy_mod.validate_strategy(new)
    if problems:
        return ProposalCheck(None, problems, notes, [])
    return ProposalCheck(new, [], notes, strategy_mod.diff_major_changes(current, new))


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    strip = lambda d: {k: v for k, v in d.items() if k not in ("version", "updated_at")}  # noqa: E731
    return json.dumps(strip(dict(a)), sort_keys=True) == json.dumps(strip(dict(b)), sort_keys=True)


# ---------------------------------------------------------------------------
# notifications (notify.py is a sibling module; imported late)
# ---------------------------------------------------------------------------

def _notify(kind: str, text: str, *, urgent: bool = False) -> None:
    try:
        import notify
        notify.notify(kind, text, urgent=urgent)
    except Exception as exc:  # never let a notification failure lose the review
        log.exception("notify failed")
        db.log_event(MODULE, f"could not send {kind} notification: {exc}", level="error")


def _speakable(text: str) -> str:
    t = re.sub(r"[*#`_>|]+", " ", text or "")
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > MAX_REPORT_CHARS:
        cut = t[:MAX_REPORT_CHARS]
        t = cut[: cut.rfind(". ") + 1] or cut
    return t


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def weekly_review(days: int = 7) -> StrategyReview:
    """Run the weekly review. See the module docstring for the full flow."""
    current = strategy_mod.load_strategy()
    data = gather(days, current)

    # 1. cold start: don't spend tokens reviewing nothing
    if data["tracked_clips"] < MIN_TRACKED_CLIPS or data["period_views"] < MIN_PERIOD_VIEWS:
        report = (f"Weekly review: not enough data yet to judge the strategy. In the last {days} days "
                  f"{data['tracked_clips']} clips had analytics and they drew {data['period_views']} views. "
                  "I kept the strategy unchanged and skipped the AI review to save budget.")
        db.log_event(MODULE, "weekly review skipped: cold start", data={
            "tracked_clips": data["tracked_clips"], "period_views": data["period_views"], "days": days})
        _notify("weekly_report", report)
        return StrategyReview(report, dict(current), [], None, None, status="cold_start")

    digest = build_digest(data)
    try:
        res = llm.complete("sonnet", system=system_blocks(current), user=user_prompt(digest, current),
                           purpose=PURPOSE, max_tokens=REVIEW_MAX_TOKENS, json_schema=REVIEW_SCHEMA)
    except budget.BudgetExceeded as exc:
        report = "Weekly review skipped: today's AI budget is used up. The strategy is unchanged; I will try next week."
        db.log_event(MODULE, f"weekly review skipped: {exc}", level="warning")
        _notify("weekly_report", report)
        return StrategyReview(report, dict(current), [], None, None, status="budget", problems=[str(exc)])
    except llm.LLMError as exc:
        report = "Weekly review failed: the AI review could not be completed. The strategy is unchanged."
        db.log_event(MODULE, f"weekly review LLM call failed: {exc}", level="error",
                     data={"error": type(exc).__name__})
        _notify("weekly_report", report)
        return StrategyReview(report, dict(current), [], None, None, status="llm_error", problems=[str(exc)])

    out = res.data or {}
    model_report = _speakable(str(out.get("report", "")))
    chk = check_proposal(current, out.get("strategy"), out.get("experiment"))

    # 2. invalid -> keep current
    if chk.strategy is None:
        db.log_event(MODULE, "strategist proposal rejected by guardrails; strategy unchanged", level="error",
                     data={"problems": chk.problems[:20], "notes": chk.notes})
        report = (model_report + " However, the proposed strategy failed safety checks, so nothing was changed."
                  ).strip()
        _notify("weekly_report", report)
        return StrategyReview(report, dict(out.get("strategy") or {}), [], None, None,
                              status="invalid", problems=chk.problems, notes=chk.notes)

    proposed = chk.strategy
    if chk.notes:
        db.log_event(MODULE, "strategist proposal adjusted by guardrails", level="warning",
                     data={"notes": chk.notes})

    # 3. major -> phone approval, nothing saved
    if chk.major:
        import notify

        kind = chk.major[0].kind if len(chk.major) == 1 else "strategy_change"
        summary = ("The weekly strategy review proposes: " + ". ".join(m.summary for m in chk.major)
                   + ". Do you approve?")
        payload = {"action": "apply_strategy", "strategy": proposed, "report": model_report,
                   "base_version": current.version,
                   "major_changes": [{"kind": m.kind, "summary": m.summary, "detail": m.detail} for m in chk.major]}
        decision_id = notify.request_approval(kind, summary, payload)
        db.log_event(MODULE, f"strategy change needs approval (decision {decision_id})",
                     data={"decision_id": decision_id, "kinds": [m.kind for m in chk.major]})
        report = model_report + " Some changes need your approval, so I will ask you separately before applying them."
        _notify("weekly_report", report)
        return StrategyReview(report, proposed, chk.major, decision_id, None,
                              status="approval_requested", notes=chk.notes)

    # 4. minor (or none) -> apply now
    if _same(proposed, current):
        db.log_event(MODULE, "weekly review: no strategy changes")
        _notify("weekly_report", model_report)
        return StrategyReview(model_report, proposed, [], None, None, status="unchanged", notes=chk.notes)
    saved = strategy_mod.save_strategy(proposed, report=model_report)
    db.log_event(MODULE, f"weekly review applied strategy version {saved.version}", data={"version": saved.version})
    _notify("weekly_report", model_report)
    return StrategyReview(model_report, dict(saved), [], None, saved.version, status="applied", notes=chk.notes)


def apply_decision(decision_id: int) -> bool:
    """Apply an approved ``apply_strategy`` decision. Returns True only if this call saved a version.

    Refuses (returns False, logs) when the decision is missing, not approved (pending / rejected /
    expired), not an ``apply_strategy`` payload, already applied (idempotent), fails re-validation,
    or the live strategy has moved on since the proposal in a way that touches the same fields.
    Non-conflicting moves are rebased: the proposal's changed fields are applied onto the live strategy.
    """
    d = db.get_decision(decision_id)
    if d is None:
        db.log_event(MODULE, f"apply_decision: decision {decision_id} not found", level="warning")
        return False
    payload = d.payload if isinstance(d.payload, Mapping) else {}
    if payload.get("action") != "apply_strategy":
        db.log_event(MODULE, f"apply_decision: decision {decision_id} is not a strategy change", level="warning")
        return False
    if d.status != "approved":
        db.log_event(MODULE, f"apply_decision: decision {decision_id} is {d.status}, not applying",
                     level="warning" if d.status == "pending" else "info")
        return False
    if db.strategy_version_for_decision(decision_id) is not None:
        db.log_event(MODULE, f"apply_decision: decision {decision_id} already applied")
        return False
    lock = f"strategist:applying:{decision_id}"
    if not db.kv_set_if_absent(lock, {"at": db.now_iso()}):
        db.log_event(MODULE, f"apply_decision: decision {decision_id} already being applied")
        return False
    try:
        proposed = payload.get("strategy")
        if not isinstance(proposed, Mapping):
            db.log_event(MODULE, f"apply_decision: decision {decision_id} has no strategy", level="error")
            return False
        current = strategy_mod.load_strategy()
        base_version = payload.get("base_version")
        target = dict(proposed)
        if base_version is not None and current.version != base_version:
            base = db.get_strategy_version(int(base_version))
            if base is None or not isinstance(base.json, Mapping):
                db.log_event(MODULE, f"apply_decision: base version {base_version} unknown; refusing",
                             level="error", data={"decision_id": decision_id})
                _notify("strategy_conflict", "An approved strategy change could not be applied because "
                        "the strategy changed since it was proposed. Nothing was changed.")
                return False
            ignore = {"version", "updated_at"}
            keys = (set(proposed) | set(base.json) | set(current)) - ignore
            prop_changed = {k for k in keys if proposed.get(k) != base.json.get(k)}
            cur_changed = {k for k in keys if current.get(k) != base.json.get(k)}
            conflict = sorted(prop_changed & cur_changed)
            if conflict:
                db.log_event(MODULE, f"apply_decision: strategy moved on since decision {decision_id}; conflicting "
                             f"fields {conflict}", level="error", data={"decision_id": decision_id,
                                                                        "base_version": base_version,
                                                                        "current_version": current.version})
                _notify("strategy_conflict", "An approved strategy change could not be applied because the "
                        "strategy changed since it was proposed. Nothing was changed.")
                return False
            target = json.loads(json.dumps(dict(current)))
            for k in prop_changed:
                if k in proposed:
                    target[k] = proposed[k]
                else:
                    target.pop(k, None)
        # re-validate (licence hard rules + schema) against the live strategy
        errs = licence_problems(current, target) + strategy_mod.validate_strategy({**target, "version": current.version,
                                                     "updated_at": current["updated_at"]})
        if errs:
            db.log_event(MODULE, f"apply_decision: decision {decision_id} failed re-validation", level="error",
                         data={"problems": errs[:20]})
            return False
        saved = strategy_mod.save_strategy(target, report=payload.get("report"), decision_id=decision_id)
        db.log_event(MODULE, f"approved strategy change applied as version {saved.version}",
                     data={"decision_id": decision_id, "version": saved.version})
        return True
    finally:
        db.kv_delete(lock)
