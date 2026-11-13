"""The strategy file: schema, validation, versioned saves and major-change detection.

``strategy.json`` drives all daily behaviour. The weekly strategist rewrites it;
every save bumps ``version`` and is recorded in the ``strategy_versions`` table.
Changes flagged by :func:`diff_major_changes` must be approved by the owner by
phone (a ``decisions`` row) *before* :func:`save_strategy` is called.

Schema (all keys required unless marked optional; keys starting with ``_`` are
comments and ignored):

``version`` int >= 1
    Managed by :func:`save_strategy`; never hand-edit after the first commit.
``updated_at`` str
    UTC ISO timestamp of the last save.
``niche`` str
    One-sentence description of the channel's niche. Changing it is *major*.
``topic_areas`` list[str]
    Broad areas (e.g. ``space``). Adding one is *major* (new niche).
``topics`` list[{query, area, source_types}]
    Search terms for ``scout``; ``area`` must be in ``topic_areas`` and every
    ``source_types`` entry must be in the top-level ``source_types``.
``source_types`` list[str]
    Allowed source kinds; known ones are in :data:`KNOWN_SOURCE_TYPES`.
    Adding or removing one is *major*.
``allowlist`` {source_type: {any_channel, channels[{name, handle, channel_id, note}], domains[]}}
    Publishers whose footage is eligible, per source type. ``any_channel: true`` is only
    allowed for :data:`ANY_CHANNEL_SOURCE_TYPES` (CC BY, where the per-video licence is the
    gate); a government type must name its official publishers. Any edit is *major*.
``blocklist_channels`` list[str]
    Channel names that are never eligible (rebroadcasters such as C-SPAN). Any edit is *major*.
``hook_style`` / ``title_style`` str
    Prose guidance passed to Sonnet.
``caption_style`` dict
    Rendering parameters for ``edit``; known keys and types in :data:`CAPTION_STYLE_KEYS`.
``clip_length_sec`` {min, target, max}
    Seconds, ``0 < min <= target <= max <= 180``.
``min_haiku_score`` number 1-10
    Windows scoring below this are never escalated to Sonnet.
``windows_to_escalate`` int >= 1
    How many top-scoring windows per source go to Sonnet.
``max_clips_per_source`` int >= 1
``max_windows_per_source`` int >= 1 (optional, default :data:`DEFAULT_MAX_WINDOWS_PER_SOURCE`)
    Cap on Haiku-scored transcript windows per source (long hearings are sampled evenly).
``uploads_per_day`` int >= 0
    Further capped by ``MAX_UPLOADS_PER_DAY`` (see :func:`upload_cap`); must be
    <= ``len(posting_times)``.
``posting_times`` list["HH:MM"]
    Local publish slots in ``timezone``.
``timezone`` str (IANA)
``daily_llm_budget_usd`` number > 0
    Overrides ``DAILY_LLM_BUDGET_USD`` but never exceeds ``MAX_DAILY_LLM_BUDGET_USD``
    (see ``budget.daily_cap_usd``). An increase is *major*.
``default_hashtags`` list[str] (optional)
``experiment`` object or null
    ``{id, hypothesis, variable, control, variant, start, end, success_metric}``;
    ``start``/``end`` are local ``YYYY-MM-DD`` dates (inclusive).
``notes`` str (optional)

Licence policy (enforced by ``fetch.check_licence``; repeated here because the
strategy's allowlist must respect it):

* US federal government works are public domain (17 U.S.C. 105) **only when
  produced by the government itself**: NASA, official House/Senate committee
  feeds (house.gov / senate.gov and the committees' own channels), DVIDS, NARA,
  etc. C-SPAN's own broadcasts are copyrighted by C-SPAN and news-outlet
  re-uploads are not government works - never use them. NASA/JPL videos can
  contain third-party material (music, contractor footage, Caltech notices):
  reject if the description asserts copyright.
* Creative Commons content must be CC BY (YouTube's "Creative Commons
  Attribution licence (reuse allowed)"). NC, ND and SA variants are rejected.
* Every clip records its source URL and licence, and credits the source on
  screen and in the description.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db
from config import get_settings

KNOWN_SOURCE_TYPES = frozenset({"nasa", "congress", "cc_by_youtube", "dvids", "nara", "us_gov_other"})
# Source types whose allowlist may set any_channel (the per-video CC BY licence check is the gate).
ANY_CHANNEL_SOURCE_TYPES = frozenset({"cc_by_youtube"})
GOV_SOURCE_TYPES = KNOWN_SOURCE_TYPES - ANY_CHANNEL_SOURCE_TYPES

DEFAULT_MAX_WINDOWS_PER_SOURCE = 40

CAPTION_STYLE_KEYS: dict[str, type | tuple[type, ...]] = {
    "font": str,
    "font_size": int,
    "primary_color": str,
    "highlight_color": str,
    "outline_color": str,
    "outline_px": int,
    "max_words_per_line": int,
    "position": str,          # top | center | lower_third
    "uppercase": bool,
    "animation": str,         # word_pop | karaoke | none
    "framing": str,           # auto | center_crop | blur_fill | fit (optional; default auto)
}
CAPTION_STYLE_CHOICES: dict[str, tuple[str, ...]] = {
    "position": ("top", "center", "lower_third"),
    "animation": ("word_pop", "karaoke", "none"),
    "framing": ("auto", "center_crop", "blur_fill", "fit"),
}

REQUIRED_KEYS = (
    "version", "updated_at", "niche", "topic_areas", "topics", "source_types", "allowlist",
    "blocklist_channels", "hook_style", "title_style", "caption_style", "clip_length_sec",
    "min_haiku_score", "windows_to_escalate", "max_clips_per_source", "uploads_per_day",
    "posting_times", "timezone", "daily_llm_budget_usd", "experiment",
)
OPTIONAL_KEYS = ("default_hashtags", "notes", "max_windows_per_source")
EXPERIMENT_KEYS = ("id", "hypothesis", "variable", "control", "variant", "start", "end", "success_metric")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class StrategyError(ValueError):
    """Strategy failed validation; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("invalid strategy: " + "; ".join(errors))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class MajorChange:
    """A change that needs phone approval before it may be saved."""

    kind: str     # new_niche | new_source_type | budget_increase  (== db.DECISION_KINDS)
    summary: str  # speakable sentence
    detail: dict[str, Any]


class Strategy(dict):
    """A validated strategy: a plain ``dict`` (JSON-shaped) with typed accessors.

    Treat as read-only; build a modified copy with :meth:`updated`.
    """

    @property
    def version(self) -> int:
        return int(self["version"])

    @property
    def topics(self) -> list[dict[str, Any]]:
        return self["topics"]

    @property
    def source_types(self) -> list[str]:
        return self["source_types"]

    @property
    def clip_length(self) -> dict[str, float]:
        return self["clip_length_sec"]

    @property
    def experiment(self) -> dict[str, Any] | None:
        return self.get("experiment")

    @property
    def max_windows_per_source(self) -> int:
        return int(self.get("max_windows_per_source", DEFAULT_MAX_WINDOWS_PER_SOURCE))

    def allowlist_for(self, source_type: str) -> dict[str, Any]:
        return self["allowlist"].get(source_type, {"any_channel": False, "channels": [], "domains": []})

    def updated(self, **changes: Any) -> "Strategy":
        """Deep-copied strategy with top-level ``changes`` applied (not validated)."""
        data = json.loads(json.dumps(self))
        data.update(changes)
        return Strategy(data)

    def to_json(self) -> str:
        return json.dumps(self, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _valid_date(v: Any) -> bool:
    try:
        date.fromisoformat(v)
        return True
    except (TypeError, ValueError):
        return False


def validate_strategy(data: Mapping[str, Any]) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    errs: list[str] = []
    if not isinstance(data, Mapping):
        return ["strategy must be a JSON object"]
    keys = {k for k in data if not k.startswith("_")}
    for k in REQUIRED_KEYS:
        if k not in data:
            errs.append(f"missing key {k!r}")
    unknown = keys - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS)
    if unknown:
        errs.append(f"unknown keys {sorted(unknown)}")
    if errs:
        return errs

    if not isinstance(data["version"], int) or isinstance(data["version"], bool) or data["version"] < 1:
        errs.append("version must be an int >= 1")
    if not isinstance(data["updated_at"], str):
        errs.append("updated_at must be a string")
    for k in ("niche", "hook_style", "title_style"):
        if not isinstance(data[k], str) or not data[k].strip():
            errs.append(f"{k} must be a non-empty string")

    areas = data["topic_areas"]
    if not _is_str_list(areas) or not areas:
        errs.append("topic_areas must be a non-empty list of strings")
        areas = []
    stypes = data["source_types"]
    if not _is_str_list(stypes) or not stypes:
        errs.append("source_types must be a non-empty list of strings")
        stypes = []
    for st in stypes:
        if st not in KNOWN_SOURCE_TYPES:
            errs.append(f"unknown source type {st!r} (known: {sorted(KNOWN_SOURCE_TYPES)})")

    topics = data["topics"]
    if not isinstance(topics, list) or not topics:
        errs.append("topics must be a non-empty list")
    else:
        for i, t in enumerate(topics):
            if not isinstance(t, Mapping) or not isinstance(t.get("query"), str) or not t.get("query", "").strip():
                errs.append(f"topics[{i}] needs a non-empty 'query'")
                continue
            if t.get("area") not in areas:
                errs.append(f"topics[{i}].area {t.get('area')!r} not in topic_areas")
            ts = t.get("source_types")
            if not _is_str_list(ts) or not ts:
                errs.append(f"topics[{i}].source_types must be a non-empty list")
            else:
                bad = [x for x in ts if x not in stypes]
                if bad:
                    errs.append(f"topics[{i}].source_types {bad} not in source_types")

    allow = data["allowlist"]
    if not isinstance(allow, Mapping):
        errs.append("allowlist must be an object")
    else:
        for st in stypes:
            if st not in allow:
                errs.append(f"allowlist missing entry for source type {st!r}")
        for st, entry in allow.items():
            if st.startswith("_"):
                continue
            if not isinstance(entry, Mapping):
                errs.append(f"allowlist[{st}] must be an object")
                continue
            if not isinstance(entry.get("any_channel", False), bool):
                errs.append(f"allowlist[{st}].any_channel must be a bool")
            elif entry.get("any_channel") and st not in ANY_CHANNEL_SOURCE_TYPES:
                errs.append(f"allowlist[{st}].any_channel=true is not allowed: a government source type "
                            "must list its official publishers")
            chans = entry.get("channels", [])
            if not isinstance(chans, list):
                errs.append(f"allowlist[{st}].channels must be a list")
            else:
                for j, ch in enumerate(chans):
                    if not isinstance(ch, Mapping) or not isinstance(ch.get("name"), str):
                        errs.append(f"allowlist[{st}].channels[{j}] needs a 'name'")
            if not _is_str_list(entry.get("domains", [])):
                errs.append(f"allowlist[{st}].domains must be a list of strings")
            if not entry.get("any_channel") and not chans and not entry.get("domains"):
                errs.append(f"allowlist[{st}] allows nothing (no channels, no domains, any_channel false)")

    if not _is_str_list(data["blocklist_channels"]):
        errs.append("blocklist_channels must be a list of strings")

    cs = data["caption_style"]
    if not isinstance(cs, Mapping):
        errs.append("caption_style must be an object")
    else:
        for k, v in cs.items():
            if k.startswith("_"):
                continue
            typ = CAPTION_STYLE_KEYS.get(k)
            if typ is None:
                errs.append(f"caption_style has unknown key {k!r}")
            elif not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
                errs.append(f"caption_style.{k} must be {getattr(typ, '__name__', typ)}")
            elif k in CAPTION_STYLE_CHOICES and v not in CAPTION_STYLE_CHOICES[k]:
                errs.append(f"caption_style.{k} must be one of {list(CAPTION_STYLE_CHOICES[k])}")

    cl = data["clip_length_sec"]
    if not isinstance(cl, Mapping) or not all(_is_num(cl.get(k)) for k in ("min", "target", "max")):
        errs.append("clip_length_sec needs numeric min/target/max")
    elif not (0 < cl["min"] <= cl["target"] <= cl["max"] <= 180):
        errs.append("clip_length_sec must satisfy 0 < min <= target <= max <= 180")

    if not _is_num(data["min_haiku_score"]) or not 1 <= data["min_haiku_score"] <= 10:
        errs.append("min_haiku_score must be a number in 1..10")
    for k in ("windows_to_escalate", "max_clips_per_source"):
        if not isinstance(data[k], int) or isinstance(data[k], bool) or data[k] < 1:
            errs.append(f"{k} must be an int >= 1")
    mw = data.get("max_windows_per_source", DEFAULT_MAX_WINDOWS_PER_SOURCE)
    if not isinstance(mw, int) or isinstance(mw, bool) or mw < 1:
        errs.append("max_windows_per_source must be an int >= 1")
    upd = data["uploads_per_day"]
    if not isinstance(upd, int) or isinstance(upd, bool) or upd < 0:
        errs.append("uploads_per_day must be an int >= 0")

    pts = data["posting_times"]
    if not _is_str_list(pts) or not all(_HHMM.match(p) for p in pts):
        errs.append("posting_times must be a list of 'HH:MM' strings")
    elif len(set(pts)) != len(pts):
        errs.append("posting_times must be unique")
    elif isinstance(upd, int) and upd > len(pts):
        errs.append("uploads_per_day exceeds the number of posting_times")

    try:
        ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        errs.append(f"unknown timezone {data['timezone']!r}")

    if not _is_num(data["daily_llm_budget_usd"]) or data["daily_llm_budget_usd"] <= 0:
        errs.append("daily_llm_budget_usd must be a number > 0")

    if "default_hashtags" in data and not _is_str_list(data["default_hashtags"]):
        errs.append("default_hashtags must be a list of strings")
    if "notes" in data and not isinstance(data["notes"], str):
        errs.append("notes must be a string")

    exp = data["experiment"]
    if exp is not None:
        if not isinstance(exp, Mapping):
            errs.append("experiment must be an object or null")
        else:
            missing = [k for k in EXPERIMENT_KEYS if k not in exp]
            if missing:
                errs.append(f"experiment missing {missing}")
            else:
                if not isinstance(exp["id"], str) or not exp["id"]:
                    errs.append("experiment.id must be a non-empty string")
                if not (_valid_date(exp["start"]) and _valid_date(exp["end"])):
                    errs.append("experiment.start/end must be YYYY-MM-DD")
                elif exp["end"] < exp["start"]:
                    errs.append("experiment.end is before start")
    return errs


def parse_strategy(data: Mapping[str, Any]) -> Strategy:
    errs = validate_strategy(data)
    if errs:
        raise StrategyError(errs)
    return Strategy(json.loads(json.dumps(data)))


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------

def strategy_path() -> Path:
    return get_settings().strategy_path


def load_strategy(path: Path | str | None = None) -> Strategy:
    """Read and validate the live strategy file. Raises :class:`StrategyError` / ``OSError``."""
    p = Path(path) if path is not None else strategy_path()
    with open(p, encoding="utf-8") as fh:
        return parse_strategy(json.load(fh))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_strategy(
    new: Mapping[str, Any],
    *,
    report: str | None = None,
    decision_id: int | None = None,
    path: Path | str | None = None,
) -> Strategy:
    """Validate, bump ``version``, stamp ``updated_at``, write atomically, record in ``strategy_versions``.

    The new version is ``max(file version, newest recorded version) + 1``. The
    caller is responsible for getting approval for :func:`diff_major_changes`
    first and passing the approving ``decision_id``.
    """
    p = Path(path) if path is not None else strategy_path()
    current = 0
    if p.exists():
        try:
            with open(p, encoding="utf-8") as fh:
                current = int(json.load(fh).get("version", 0))
        except (ValueError, OSError, AttributeError):
            current = 0
    latest = db.latest_strategy_version()
    next_version = max(current, latest.version if latest else 0) + 1

    data = json.loads(json.dumps(dict(new)))
    data["version"] = next_version
    data["updated_at"] = db.now_iso()
    strat = parse_strategy(data)
    _atomic_write(p, strat.to_json())
    db.add_strategy_version(next_version, dict(strat), report=report, decision_id=decision_id)
    db.log_event("strategy", f"strategy saved as version {next_version}",
                 data={"version": next_version, "decision_id": decision_id})
    return strat


def record_initial_version(strategy: Strategy | None = None) -> None:
    """Record the shipped strategy in ``strategy_versions`` if that table is empty."""
    if db.latest_strategy_version() is None:
        s = strategy or load_strategy()
        db.add_strategy_version(s.version, dict(s), report="initial strategy")


# ---------------------------------------------------------------------------
# change detection & derived values
# ---------------------------------------------------------------------------

def _allowlist_entries(data: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {k: v for k, v in (data.get("allowlist") or {}).items() if not k.startswith("_")}


def _publishers(entry: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    return {((c.get("name") or "").casefold(), (c.get("handle") or "").casefold(), c.get("channel_id") or "")
            for c in entry.get("channels", [])}


def _spoken(source_type: str) -> str:
    return source_type.replace("_", " ")


def _allowlist_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    oa, na = _allowlist_entries(old), _allowlist_entries(new)
    changed: list[str] = []
    for st in sorted(set(oa) | set(na)):
        o, n = oa.get(st), na.get(st)
        if o is None:
            changed.append(f"new allowlist entry for {_spoken(st)}")
            continue
        if n is None:
            changed.append(f"remove the allowlist entry for {_spoken(st)}")
            continue
        added, removed = _publishers(n) - _publishers(o), _publishers(o) - _publishers(n)
        # same name, different id/handle: say so explicitly - this decides who counts as "official"
        renamed = {a[0] for a in added if a[0]} & {r[0] for r in removed if r[0]}
        for name in sorted(renamed):
            new_ids = next(a for a in added if a[0] == name)
            changed.append(f"change the official channel id or handle of {name} to "
                           f"{new_ids[2] or 'none'} {new_ids[1] or ''}".rstrip())
        added = {a for a in added if a[0] not in renamed}
        removed = {r for r in removed if r[0] not in renamed}
        if added:
            changed.append(f"allow {', '.join(a[0] or a[1] or a[2] for a in sorted(added))} for {_spoken(st)}")
        if removed:
            changed.append(f"stop using {', '.join(r[0] or r[1] or r[2] for r in sorted(removed))}")
        if bool(o.get("any_channel")) != bool(n.get("any_channel")):
            changed.append(f"set any channel to {bool(n.get('any_channel'))} for {_spoken(st)}")
        if sorted(o.get("domains", [])) != sorted(n.get("domains", [])):
            changed.append(f"change allowed websites for {_spoken(st)}")
    return changed


def diff_major_changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[MajorChange]:
    """Changes from ``old`` to ``new`` that require phone approval.

    * ``new_niche``: ``niche`` text changed, or a topic area was added.
    * ``new_source_type``: a source type was added.
    * ``budget_increase``: ``daily_llm_budget_usd`` went up.
    * ``strategy_change``: any edit to the publisher allowlist (channels, domains, any_channel,
      entries) or the blocklist, or a source type removed - these decide what footage is
      licence-eligible, so a human signs off on every one.

    Topic/search-term tweaks, styles, posting times, clip lengths and experiments are minor.
    """
    out: list[MajorChange] = []
    if old.get("niche", "").strip() != new.get("niche", "").strip():
        out.append(MajorChange("new_niche", f"Change the channel niche to: {new.get('niche', '')}",
                               {"old": old.get("niche"), "new": new.get("niche")}))
    added_areas = [a for a in new.get("topic_areas", []) if a not in old.get("topic_areas", [])]
    if added_areas:
        spoken = ", ".join(_spoken(a) for a in added_areas)
        out.append(MajorChange("new_niche", f"Add new topic area: {spoken}", {"added_topic_areas": added_areas}))
    added_types = [t for t in new.get("source_types", []) if t not in old.get("source_types", [])]
    if added_types:
        spoken = ", ".join(_spoken(t) for t in added_types)
        out.append(MajorChange("new_source_type", f"Start using a new source type: {spoken}",
                               {"added_source_types": added_types}))
    ob, nb = float(old.get("daily_llm_budget_usd", 0)), float(new.get("daily_llm_budget_usd", 0))
    if nb > ob + 1e-9:
        out.append(MajorChange("budget_increase",
                               f"Raise the daily AI budget from {ob:.2f} to {nb:.2f} dollars",
                               {"old": ob, "new": nb}))
    changed = _allowlist_changes(old, new)
    if changed:
        out.append(MajorChange("strategy_change", "Change the publisher allowlist: " + "; ".join(changed),
                               {"allowlist_changes": changed}))
    old_block = {b.casefold() for b in old.get("blocklist_channels", [])}
    new_block = {b.casefold() for b in new.get("blocklist_channels", [])}
    if new_block - old_block:
        out.append(MajorChange("strategy_change", f"Block {len(new_block - old_block)} more channel(s)",
                               {"blocklist_added": sorted(new_block - old_block)}))
    if old_block - new_block:
        out.append(MajorChange("strategy_change", f"Unblock {len(old_block - new_block)} channel(s)",
                               {"blocklist_removed": sorted(old_block - new_block)}))
    removed_types = [t for t in old.get("source_types", []) if t not in new.get("source_types", [])]
    if removed_types:
        out.append(MajorChange("strategy_change",
                               "Stop using source type " + ", ".join(_spoken(t) for t in removed_types),
                               {"removed_source_types": removed_types}))
    return out


def upload_cap(strategy: Mapping[str, Any] | None = None) -> int:
    """Effective uploads per local day: ``min(strategy.uploads_per_day, MAX_UPLOADS_PER_DAY)``."""
    limit = get_settings().max_uploads_per_day
    if strategy is None:
        try:
            strategy = load_strategy()
        except (OSError, ValueError):  # missing/unparsable/invalid -> config limit
            return limit
    return max(0, min(int(strategy["uploads_per_day"]), limit))


def active_experiment(strategy: Mapping[str, Any], day: date | None = None) -> dict[str, Any] | None:
    """The experiment if ``day`` (default local today) is within its start..end, else None."""
    exp = strategy.get("experiment")
    if not exp:
        return None
    d = (day or db.local_today()).isoformat()
    return exp if exp["start"] <= d <= exp["end"] else None


def assign_variant(strategy: Mapping[str, Any], key: int, day: date | None = None) -> tuple[str | None, str | None]:
    """Deterministic A/B arm for a new clip: ``(experiment_id, "control"|"variant")``.

    ``key`` is any stable integer (e.g. the number of clips already produced under
    the experiment); even -> control, odd -> variant. Returns ``(None, None)`` when
    no experiment is active.
    """
    exp = active_experiment(strategy, day)
    if exp is None:
        return None, None
    return exp["id"], ("control" if key % 2 == 0 else "variant")


# ---------------------------------------------------------------------------
# publisher allowlist (shared by scout and fetch)
# ---------------------------------------------------------------------------

_YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _host_matches(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def is_publisher_allowed(
    strategy: Mapping[str, Any],
    source_type: str,
    *,
    channel: str | None = None,
    channel_id: str | None = None,
    handle: str | None = None,
    url: str | None = None,
) -> tuple[bool, str]:
    """Is this publisher eligible for ``source_type`` under the strategy? Returns ``(ok, reason)``.

    Rules, in order:

    1. ``source_type`` must be in ``source_types``; blocklisted channel names never pass.
    2. ``any_channel`` entries pass when the URL host matches the entry's domains
       (or the entry lists no domains).
    3. A listed channel matches **only on a YouTube URL** (or when no URL is given, as a
       scout pre-filter), and only by an identifier YouTube guarantees to be unique:

       * an entry with a pinned ``channel_id`` matches that exact id and nothing else
         (a matching name or handle with another id is an impostor);
       * an entry without ``channel_id`` matches an exact (case-insensitive) ``handle``.

       A channel *name* is never sufficient: anyone can name a channel "NASA". Entries
       with neither ``channel_id`` nor ``handle`` therefore admit nothing until the owner
       fills one in (``main.py doctor`` warns about them). On other sites (Vimeo, ...)
       uploader names/ids are not checked at all - only rule 4 applies.
    4. A non-YouTube ``http(s)`` URL whose host is (a subdomain of) a listed domain passes.
       YouTube URLs always need rule 2 or 3 - a YouTube domain alone proves nothing.

    This checks the *publisher* only; the per-video licence check lives in
    ``fetch.check_licence``.
    """
    if source_type not in strategy.get("source_types", []):
        return False, f"source type {source_type!r} not enabled in strategy"
    blocked = {b.casefold() for b in strategy.get("blocklist_channels", [])}
    if channel and channel.strip().casefold() in blocked:
        return False, f"channel {channel!r} is blocklisted"
    entry = strategy.get("allowlist", {}).get(source_type) or {}
    domains = list(entry.get("domains", []))
    host, scheme = "", ""
    if url:
        try:
            parsed = urlparse(url)
            host, scheme = (parsed.hostname or ""), parsed.scheme.lower()
        except ValueError:
            return False, f"unparsable URL {url!r}"
        if scheme not in ("http", "https") or "\\" in url or parsed.username or parsed.password:
            return False, f"URL {url!r} is not a plain http(s) URL"
    on_youtube = bool(host) and _host_matches(host, list(_YOUTUBE_DOMAINS))
    if entry.get("any_channel") and (not domains or (host and _host_matches(host, domains))):
        return True, f"any channel allowed for {source_type}"
    if on_youtube or not url:
        for ch in entry.get("channels", []):
            pinned = ch.get("channel_id")
            if pinned:
                if channel_id and channel_id.strip() == pinned:
                    return True, f"allowlisted channel id {pinned}"
                continue  # the pinned id is authoritative: name/handle never stand in for it
            if handle and ch.get("handle") and handle.strip().casefold() == ch["handle"].strip().casefold():
                return True, f"allowlisted handle {ch['handle']}"
    if host and not on_youtube and _host_matches(host, domains):
        return True, f"allowlisted domain {host}"
    return False, f"publisher not on the {source_type} allowlist"
