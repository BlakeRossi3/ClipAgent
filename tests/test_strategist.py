from __future__ import annotations

import copy
import json
import sys
import types

import pytest

import budget
import db
import llm
import strategist
import strategy as strategy_mod


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def notify_stub(monkeypatch):
    """Stand-in for notify.py (written in parallel); records calls, creates real decisions."""
    mod = types.ModuleType("notify")
    mod.sent = []
    mod.approvals = []

    def notify(kind, text, *, urgent=False):
        mod.sent.append((kind, text, urgent))
        return len(mod.sent)

    def request_approval(kind, summary, payload, *, expires_hours=48):
        did = db.add_decision(kind, summary, payload)
        mod.approvals.append((did, kind, summary, payload))
        return did

    mod.notify = notify
    mod.request_approval = request_approval
    monkeypatch.setitem(sys.modules, "notify", mod)
    return mod


@pytest.fixture
def seeded(clock, make_source, make_clip, make_upload):
    """Eight uploaded clips with stats across topics/arms (enough to pass the cold-start gate)."""
    strategy_mod.record_initial_version()
    specs = [
        ("NASA Artemis", "Is this the Moon?", "control", 900, 72.0),
        ("NASA Artemis", "Watch the booster land", "variant", 1500, 81.0),
        ("Mars rover", "What did it find?", "control", 300, 55.0),
        ("Mars rover", "Perseverance found 3 rocks", "variant", 450, 60.0),
        ("UAP hearing", "He refused to answer", "control", 2500, 88.0),
        ("UAP hearing", "Why won't they say?", "variant", 1800, 79.0),
        ("rocket launch", "Liftoff in 10 seconds", "control", 120, 40.0),
        ("rocket launch", "", None, 60, 35.0),
    ]
    vids = []
    for i, (topic, hook, arm, views, pct) in enumerate(specs):
        stype = "congress" if "hearing" in topic else "nasa"
        sid = make_source(status="picked", topic=topic, source_type=stype,
                          channel="House Committee on Oversight and Accountability" if stype == "congress" else "NASA")
        cid = make_clip(sid, status="rendered", hook_text=hook, title=f"Clip {i} about {topic}",
                        start_sec=0.0, end_sec=30.0 + i * 5,
                        experiment_id="exp-001-hook-question-vs-claim" if arm else None, experiment_variant=arm)
        vid = f"vid{i:03d}"
        make_upload(cid, video_id=vid, publish_at=f"2026-11-1{i % 3}T17:45:00Z")
        db.upsert_stat(vid, "2026-11-12", views=views, avg_view_pct=pct, avg_view_duration_sec=pct / 3,
                       likes=views // 20, subscribers_gained=views // 100)
        vids.append(vid)
    db.upsert_channel_stat("2026-11-06", subscribers=100, total_views=1000, video_count=10)
    db.upsert_channel_stat("2026-11-13", subscribers=140, total_views=9000, video_count=18)
    return vids


def _current():
    return strategy_mod.load_strategy()


def reply(mutate=None, *, report="Good week. Views were up. I tweaked the hook style.", experiment=None):
    cur = _current()
    view = strategist.editable_view(cur)
    if mutate:
        mutate(view)
    return {"report": report, "strategy": view,
            "experiment": copy.deepcopy(experiment if experiment is not None else cur.experiment)}


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------

def test_digest_contains_compact_aggregates(seeded):
    text = strategist.build_digest(days=7)
    assert "PERIOD 2026-11-06..2026-11-13" in text
    for section in ("BY TOPIC", "BY SOURCE TYPE", "BY POSTING SLOT", "BY LENGTH", "BY HOOK STYLE", "CLIPS",
                    "LLM SPEND", "UPLOADS", "CHANNEL"):
        assert section in text
    assert "UAP hearing | n=2 | views=4300" in text
    assert "subscribers 100->140" in text
    # experiment comparison with sample sizes and the small-sample verdict
    assert "control n=4" in text and "variant n=3" in text
    assert "too thin to conclude" in text
    # hook styles classified in code; slot shown in local time (17:45Z -> 12:45 New York)
    assert "question |" in text and "number |" in text
    assert "12:45" in text
    assert "{" not in text  # no raw JSON/tables
    assert len(text) <= strategist.MAX_DIGEST_CHARS


def test_digest_is_capped(seeded, make_source, make_clip, make_upload):
    for i in range(60):
        sid = make_source(status="picked", topic=f"topic {i}")
        cid = make_clip(sid, status="rendered", title="x" * 80)
        make_upload(cid, video_id=f"many{i}")
        db.upsert_stat(f"many{i}", "2026-11-12", views=i, avg_view_pct=50.0)
    text = strategist.build_digest(days=7, max_chars=4000)
    assert len(text) <= 4000
    assert "omitted" in text or "truncated" in text
    full = strategist.build_digest(days=7)
    assert len(full) <= strategist.MAX_DIGEST_CHARS
    assert "middle-ranked clips omitted" in full


def test_hook_style_and_length_bucket():
    assert strategist.hook_style_of("Why is the sky blue") == "question"
    assert strategist.hook_style_of("3 things NASA hid") == "number"
    assert strategist.hook_style_of("This changes everything") == "claim"
    assert strategist.hook_style_of("") == "none"
    assert strategist.length_bucket(29) == "<30s" and strategist.length_bucket(59) == "45-59s"


# ---------------------------------------------------------------------------
# weekly_review
# ---------------------------------------------------------------------------

def test_cold_start_skips_llm(clock, fake_llm, notify_stub):
    review = strategist.weekly_review()
    assert review.status == "cold_start"
    assert fake_llm.calls == []
    assert review.applied_version is None and review.decision_id is None
    assert notify_stub.sent and notify_stub.sent[0][0] == "weekly_report"
    assert "not enough data" in notify_stub.sent[0][1]
    assert _current().version == 1


def test_minor_change_is_applied(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["hook_style"] = "Open with a short question the clip answers in the first 3 seconds."
        v["posting_times"] = ["08:45", "12:45", "17:10", "20:40"]
        v["topics"] = [t for t in v["topics"] if t["query"] != "rocket launch"]

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "applied", review.problems
    assert review.applied_version == 2
    cur = _current()
    assert cur.version == 2 and "12:45" in cur["posting_times"]
    assert cur["allowlist"]["_comment"]  # comments preserved
    assert db.latest_strategy_version().report.startswith("Good week")
    assert db.list_decisions() == []
    assert [k for k, *_ in notify_stub.sent] == ["weekly_report"]

    # the call: one Sonnet review; strategy in the cached system block; digest in the user turn
    (call,) = fake_llm.calls
    assert call.purpose == "strategist.review" and call.tier == "sonnet" and call.cache_system
    assert "CURRENT STRATEGY (version 1)" in call.system[-1]
    assert "2026-11-13" not in "".join(call.system[:1])
    assert "<digest>" in call.user and "BY TOPIC" in call.user and "Today is 2026-11-13" in call.user
    assert call.json_schema["required"] == ["report", "strategy", "experiment"]


def test_no_change_saves_nothing(seeded, fake_llm, notify_stub):
    fake_llm.queue(reply())
    review = strategist.weekly_review()
    assert review.status == "unchanged"
    assert _current().version == 1


def test_major_change_requests_approval_and_saves_nothing(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["topic_areas"].append("earth_science")
        v["topics"].append({"query": "hurricane from space", "area": "earth_science", "source_types": ["nasa"]})

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "approval_requested"
    assert review.decision_id is not None and review.applied_version is None
    assert [m.kind for m in review.major_changes] == ["new_niche"]
    assert _current().version == 1
    d = db.get_decision(review.decision_id)
    assert d.kind == "new_niche" and d.status == "pending"
    assert d.payload["action"] == "apply_strategy" and d.payload["base_version"] == 1
    assert "earth_science" in d.payload["strategy"]["topic_areas"]
    assert "approve" in d.summary.lower()
    assert notify_stub.sent[-1][0] == "weekly_report"


def test_several_major_changes_use_strategy_change_kind(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["daily_llm_budget_usd"] = 2.0
        v["niche"] = "Space only"

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "approval_requested"
    assert db.get_decision(review.decision_id).kind == "strategy_change"


def test_invalid_model_output_keeps_strategy(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["topics"][0]["source_types"] = ["tiktok"]

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "invalid" and review.problems
    assert _current().version == 1
    errs = db.list_events(level="error", module="strategist")
    assert errs and "rejected by guardrails" in errs[0].message
    assert notify_stub.sent and "nothing was changed" in notify_stub.sent[-1][1]


def test_unparsable_model_output(seeded, fake_llm, notify_stub):
    fake_llm.queue("not json", "still not json")
    review = strategist.weekly_review()
    assert review.status == "llm_error"
    assert _current().version == 1
    assert db.list_events(level="error", module="strategist")


@pytest.mark.parametrize("mutate", [
    lambda v: next(e for e in v["allowlist"] if e["source_type"] == "congress").update(any_channel=True),
    lambda v: next(e for e in v["allowlist"] if e["source_type"] == "congress")["channels"].append(
        {"name": "Forbes Breaking News", "handle": None, "channel_id": None, "note": ""}),
    lambda v: next(e for e in v["allowlist"] if e["source_type"] == "nasa")["domains"].append("youtube.com"),
    lambda v: v.update(blocklist_channels=[b for b in v["blocklist_channels"] if b != "C-SPAN"]),
], ids=["any_channel_gov", "add_news_outlet", "youtube_domain", "unblock_cspan"])
def test_allowlist_tampering_is_refused(seeded, fake_llm, notify_stub, mutate):
    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "invalid", review.problems
    assert _current().version == 1
    assert db.list_decisions() == []


def test_allowlist_addition_needs_approval(seeded, fake_llm, notify_stub):
    def mutate(v):
        next(e for e in v["allowlist"] if e["source_type"] == "congress")["channels"].append(
            {"name": "House Energy and Commerce Committee", "handle": None, "channel_id": None, "note": ""})

    # diff_major_changes alone does not see this (foundation gap) ...
    cur = _current()
    new = strategist._from_view(reply(mutate)["strategy"], cur)
    assert strategy_mod.diff_major_changes(cur, new) == []
    # ... but the strategist treats it as major
    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "approval_requested"
    assert review.major_changes[0].kind == "strategy_change"
    assert "allowlist" in review.major_changes[0].summary
    assert _current().version == 1


def test_source_type_removal_needs_approval(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["source_types"] = ["nasa", "congress"]
        v["allowlist"] = [e for e in v["allowlist"] if e["source_type"] != "cc_by_youtube"]
        for t in v["topics"]:
            t["source_types"] = [s for s in t["source_types"] if s != "cc_by_youtube"] or ["nasa"]

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "approval_requested"


def test_numeric_fields_are_clamped(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["uploads_per_day"] = 50
        v["posting_times"] = ["08:45", "12:15", "25:00", "17:10"]
        v["clip_length_sec"] = {"min": 2, "target": 400, "max": 50}
        v["windows_to_escalate"] = 99

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.status == "applied", review.problems
    cur = _current()
    assert cur["posting_times"] == ["08:45", "12:15", "17:10"]
    assert cur["uploads_per_day"] == 3
    assert cur["clip_length_sec"] == {"min": 5, "target": 50, "max": 180}
    assert cur["windows_to_escalate"] == strategist.MAX_WINDOWS_TO_ESCALATE
    assert review.notes


def test_running_experiment_cannot_be_silently_edited(seeded, fake_llm, notify_stub):
    exp = dict(_current().experiment, variant="Something else entirely", end="2026-11-30")
    fake_llm.queue(reply(lambda v: v.update(notes="tweak"), experiment=exp))
    review = strategist.weekly_review()
    assert review.status == "applied"
    cur = _current().experiment
    assert cur["variant"] == "Open with a question the clip answers." and cur["end"] == "2026-11-30"


@pytest.mark.parametrize("exp_patch,ok", [
    ({"id": "exp-002-short", "variable": "clip_length", "start": "2026-11-14", "end": "2026-11-28"}, True),
    ({"id": "exp-002-short", "variable": "clip_length", "start": "2026-11-01", "end": "2026-11-28"}, False),
    ({"id": "exp-002-short", "variable": "clip_length", "start": "2026-11-14", "end": "2027-02-01"}, False),
    ({"id": "exp-002-short", "variable": "everything", "start": "2026-11-14", "end": "2026-11-28"}, False),
])
def test_new_experiment_rules(seeded, fake_llm, notify_stub, exp_patch, ok):
    exp = dict(_current().experiment, control="45 second clips", variant="30 second clips", **exp_patch)
    fake_llm.queue(reply(experiment=exp))
    review = strategist.weekly_review()
    assert (review.status == "applied") is ok, review.problems


def test_budget_exceeded(seeded, fake_llm, notify_stub):
    budget.set_paused(True, "test")
    review = strategist.weekly_review()
    assert review.status == "budget"
    assert fake_llm.calls == []
    assert _current().version == 1
    assert "budget" in notify_stub.sent[-1][1]


# ---------------------------------------------------------------------------
# apply_decision
# ---------------------------------------------------------------------------

@pytest.fixture
def pending(seeded, fake_llm, notify_stub):
    def mutate(v):
        v["topic_areas"].append("earth_science")
        v["topics"].append({"query": "hurricane from space", "area": "earth_science", "source_types": ["nasa"]})

    fake_llm.queue(reply(mutate))
    review = strategist.weekly_review()
    assert review.decision_id
    return review.decision_id


def test_apply_decision_happy_path_and_idempotent(pending):
    assert db.resolve_decision(pending, "approved", via="sms")
    assert strategist.apply_decision(pending) is True
    cur = _current()
    assert cur.version == 2 and "earth_science" in cur["topic_areas"]
    assert db.latest_strategy_version().decision_id == pending
    # second call is a no-op
    assert strategist.apply_decision(pending) is False
    assert _current().version == 2
    assert len(db.list_strategy_versions()) == 2


@pytest.mark.parametrize("status", ["rejected", "expired", None])
def test_apply_decision_refuses_unapproved(pending, status):
    if status:
        db.resolve_decision(pending, status, via="timeout" if status == "expired" else "call")
    assert strategist.apply_decision(pending) is False
    assert _current().version == 1


def test_apply_decision_unknown_or_foreign(seeded):
    assert strategist.apply_decision(9999) is False
    did = db.add_decision("other", "something else", {"action": "something"})
    db.resolve_decision(did, "approved", via="cli")
    assert strategist.apply_decision(did) is False


def test_apply_decision_conflict(pending):
    # strategy moved on and touched the same field the proposal changes
    cur = _current()
    strategy_mod.save_strategy(cur.updated(topic_areas=cur["topic_areas"] + ["robotics"],
                                           topics=cur["topics"] + [{"query": "robot", "area": "robotics",
                                                                    "source_types": ["nasa"]}]))
    db.resolve_decision(pending, "approved", via="call")
    assert strategist.apply_decision(pending) is False
    assert _current().version == 2
    assert any("moved on" in e.message for e in db.list_events(level="error", module="strategist"))


def test_apply_decision_rebases_non_conflicting(pending):
    cur = _current()
    strategy_mod.save_strategy(cur.updated(hook_style="A new hook style."))
    db.resolve_decision(pending, "approved", via="call")
    assert strategist.apply_decision(pending) is True
    new = _current()
    assert new.version == 3
    assert new["hook_style"] == "A new hook style." and "earth_science" in new["topic_areas"]


def test_apply_decision_revalidates_payload(seeded):
    bad = dict(_current())
    bad["allowlist"] = copy.deepcopy(bad["allowlist"])
    bad["allowlist"]["congress"]["any_channel"] = True
    did = db.add_decision("strategy_change", "x", {"action": "apply_strategy", "strategy": bad, "base_version": 1})
    db.resolve_decision(did, "approved", via="cli")
    assert strategist.apply_decision(did) is False
    assert _current().version == 1


def test_editable_view_roundtrip(strategy):
    view = strategist.editable_view(strategy)
    assert isinstance(view["allowlist"], list)
    assert llm.validate_json(view, strategist.STRATEGY_VIEW_SCHEMA) == []
    merged = strategist._from_view(view, strategy)
    assert json.dumps(merged, sort_keys=True) == json.dumps(dict(strategy), sort_keys=True)
