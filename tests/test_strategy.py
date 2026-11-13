import json
from datetime import date

import pytest

import config
import db
import strategy as st


def test_shipped_strategy_is_valid(strategy):
    assert strategy.version == 1
    assert set(strategy.source_types) == {"nasa", "congress", "cc_by_youtube"}
    assert "C-SPAN" in strategy["blocklist_channels"]
    assert strategy.allowlist_for("cc_by_youtube")["any_channel"] is True
    assert strategy.allowlist_for("unknown") == {"any_channel": False, "channels": [], "domains": []}
    assert strategy.clip_length["max"] <= 60
    assert st.validate_strategy(json.loads((config.REPO_ROOT / "strategy.json").read_text())) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("niche"), "missing key 'niche'"),
    (lambda d: d.update(bogus=1), "unknown keys"),
    (lambda d: d["source_types"].append("tiktok"), "unknown source type"),
    (lambda d: d["topics"].append({"query": "x", "area": "cooking", "source_types": ["nasa"]}), "not in topic_areas"),
    (lambda d: d["topics"].append({"query": "x", "area": "space", "source_types": ["dvids"]}), "not in source_types"),
    (lambda d: d.update(clip_length_sec={"min": 50, "target": 40, "max": 59}), "clip_length_sec"),
    (lambda d: d.update(posting_times=["9:30"]), "HH:MM"),
    (lambda d: d.update(uploads_per_day=9), "exceeds the number of posting_times"),
    (lambda d: d.update(timezone="Mars/Olympus"), "unknown timezone"),
    (lambda d: d.update(daily_llm_budget_usd=0), "daily_llm_budget_usd"),
    (lambda d: d.update(min_haiku_score=11), "min_haiku_score"),
    (lambda d: d["caption_style"].update(font_size="big"), "caption_style.font_size"),
    (lambda d: d["caption_style"].update(sparkle=True), "unknown key 'sparkle'"),
    (lambda d: d["experiment"].pop("hypothesis"), "experiment missing"),
    (lambda d: d["experiment"].update(start="2026-12-01", end="2026-11-01"), "before start"),
    (lambda d: d["allowlist"].pop("congress"), "allowlist missing entry"),
    (lambda d: d["allowlist"]["nasa"].update(channels=[], domains=[]), "allows nothing"),
])
def test_validation_errors(strategy, mutate, needle):
    data = json.loads(json.dumps(strategy))
    mutate(data)
    errs = st.validate_strategy(data)
    assert any(needle in e for e in errs), errs
    with pytest.raises(st.StrategyError):
        st.parse_strategy(data)


def test_experiment_may_be_null(strategy):
    assert st.validate_strategy(strategy.updated(experiment=None)) == []


def test_save_bumps_version_atomically_and_records(strategy, settings, clock):
    new = strategy.updated(notes="week 2", hook_style="Ask a question.")
    saved = st.save_strategy(new, report="Questions won.", decision_id=None)
    assert saved.version == 2 and saved["updated_at"] == "2026-11-13T15:00:00Z"
    on_disk = st.load_strategy()
    assert on_disk == saved and on_disk["notes"] == "week 2"
    row = db.latest_strategy_version()
    assert row.version == 2 and row.report == "Questions won." and row.json["hook_style"] == "Ask a question."
    assert not list(settings.strategy_path.parent.glob("strategy.json.*.tmp"))
    # DB ahead of file (e.g. file restored from git) -> still monotonic
    db.add_strategy_version(7, dict(saved))
    assert st.save_strategy(saved).version == 8


def test_save_rejects_invalid_and_leaves_file(strategy, settings):
    before = settings.strategy_path.read_text()
    with pytest.raises(st.StrategyError):
        st.save_strategy(strategy.updated(uploads_per_day=-1))
    assert settings.strategy_path.read_text() == before
    assert db.latest_strategy_version() is None


def test_record_initial_version(strategy):
    st.record_initial_version()
    st.record_initial_version()
    assert [v.version for v in db.list_strategy_versions()] == [1]


def test_diff_major_changes(strategy):
    assert st.diff_major_changes(strategy, strategy.updated(notes="x", posting_times=["10:00"])) == []
    new = strategy.updated(
        niche="Space, science and cooking",
        topic_areas=strategy["topic_areas"] + ["cooking"],
        source_types=strategy["source_types"] + ["dvids"],
        daily_llm_budget_usd=2.0,
    )
    kinds = [c.kind for c in st.diff_major_changes(strategy, new)]
    assert kinds == ["new_niche", "new_niche", "new_source_type", "budget_increase"]
    assert all(k in db.DECISION_KINDS for k in kinds)
    budget_change = st.diff_major_changes(strategy, strategy.updated(daily_llm_budget_usd=2.0))[0]
    assert "2.00 dollars" in budget_change.summary
    # topic-area removals and budget decreases are minor ...
    less = strategy.updated(daily_llm_budget_usd=0.5, topic_areas=["space"])
    assert st.diff_major_changes(strategy, less) == []
    # ... but removing a source type changes what footage is eligible
    fewer = st.diff_major_changes(strategy, strategy.updated(source_types=["nasa"]))
    assert [c.kind for c in fewer] == ["strategy_change"]
    assert fewer[0].detail == {"removed_source_types": ["congress", "cc_by_youtube"]}


def test_allowlist_and_blocklist_edits_are_major(strategy):
    allow = json.loads(json.dumps(strategy["allowlist"]))
    allow["congress"]["channels"].append({"name": "House Energy and Commerce", "handle": None, "channel_id": None})
    allow["nasa"]["domains"].append("nara.gov")
    changes = st.diff_major_changes(strategy, strategy.updated(allowlist=allow))
    assert [c.kind for c in changes] == ["strategy_change"]
    assert "house energy and commerce" in changes[0].summary and "allowed websites" in changes[0].summary
    blocked = st.diff_major_changes(strategy, strategy.updated(
        blocklist_channels=strategy["blocklist_channels"][1:] + ["Some News"]))
    assert [c.detail for c in blocked] == [{"blocklist_added": ["some news"]}, {"blocklist_removed": ["c-span"]}]


def test_any_channel_only_for_cc_by(strategy):
    allow = json.loads(json.dumps(strategy["allowlist"]))
    allow["congress"]["any_channel"] = True
    errs = st.validate_strategy(strategy.updated(allowlist=allow))
    assert any("allowlist[congress].any_channel" in e for e in errs)
    assert st.validate_strategy(strategy) == []      # cc_by_youtube keeps any_channel


def test_caption_framing_and_max_windows(strategy):
    style = {**strategy["caption_style"], "framing": "blur_fill"}
    assert st.validate_strategy(strategy.updated(caption_style=style)) == []
    bad = st.validate_strategy(strategy.updated(caption_style={**style, "framing": "stretch"}))
    assert bad == ["caption_style.framing must be one of ['auto', 'center_crop', 'blur_fill', 'fit']"]
    assert strategy.max_windows_per_source == 40
    no_key = st.parse_strategy({k: v for k, v in strategy.items() if k != "max_windows_per_source"})
    assert no_key.max_windows_per_source == st.DEFAULT_MAX_WINDOWS_PER_SOURCE
    assert st.validate_strategy(strategy.updated(max_windows_per_source=0)) == [
        "max_windows_per_source must be an int >= 1"]


def test_upload_cap(strategy, monkeypatch):
    assert st.upload_cap(strategy) == 4
    assert st.upload_cap(strategy.updated(uploads_per_day=4)) == 4
    config.set_settings(config.get_settings().replace(max_uploads_per_day=2))
    assert st.upload_cap(strategy) == 2
    assert st.upload_cap() == 2


def test_experiment_assignment(strategy):
    exp_id = strategy["experiment"]["id"]
    assert st.assign_variant(strategy, 0, day=date(2026, 11, 13)) == (exp_id, "control")
    assert st.assign_variant(strategy, 1, day=date(2026, 11, 27)) == (exp_id, "variant")
    assert st.assign_variant(strategy, 1, day=date(2026, 11, 28)) == (None, None)
    assert st.active_experiment(strategy.updated(experiment=None)) is None


@pytest.mark.parametrize("source_type, kw, ok", [
    ("nasa", dict(channel_id="UCLA_DiR1FfKNvjuUpBHmylQ", url="https://www.youtube.com/watch?v=x"), True),
    ("nasa", dict(channel="nasa goddard", url="https://youtu.be/x"), True),
    ("nasa", dict(handle="@nasajpl", url="https://youtu.be/x"), True),
    ("nasa", dict(channel="Space Fan 99", url="https://www.youtube.com/watch?v=x"), False),
    ("nasa", dict(url="https://images.nasa.gov/details/abc"), True),
    ("nasa", dict(url="https://evilnasa.gov.example.com/x"), False),
    ("congress", dict(url="https://science.house.gov/hearings?ID=1"), True),
    ("congress", dict(channel="C-SPAN", url="https://www.youtube.com/watch?v=x"), False),
    ("congress", dict(url="https://www.c-span.org/video/?123"), False),
    ("cc_by_youtube", dict(channel="Any Creator", url="https://www.youtube.com/watch?v=x"), True),
    ("cc_by_youtube", dict(channel="Forbes Breaking News", url="https://www.youtube.com/watch?v=x"), False),
    ("cc_by_youtube", dict(channel="Any", url="https://vimeo.com/1"), False),
    ("dvids", dict(url="https://www.dvidshub.net/video/1"), False),  # type not enabled
])
def test_is_publisher_allowed(strategy, source_type, kw, ok):
    allowed, reason = st.is_publisher_allowed(strategy, source_type, **kw)
    assert allowed is ok, reason
