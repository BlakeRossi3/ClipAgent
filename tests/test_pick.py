import json

import pytest

import budget
import db
import llm
import pick
import strategy as strategy_mod

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

WORD_SEC = 0.4  # each word: 0.35 s spoken + 0.05 s gap


def build_transcript(sentences, *, start=0.0, sentence_gap=0.4, words=True, source_id=1):
    """sentences: list of str, or (gap_before_sec, str) tuples for silences."""
    segs, t = [], start
    for i, item in enumerate(sentences):
        gap, text = item if isinstance(item, tuple) else (sentence_gap if i else 0.0, item)
        t += gap
        ws = []
        for tok in text.split():
            ws.append({"start": round(t, 3), "end": round(t + 0.35, 3), "word": tok, "prob": 0.9})
            t += WORD_SEC
        seg = {"id": i, "start": ws[0]["start"], "end": ws[-1]["end"], "text": text}
        if words:
            seg["words"] = ws
        segs.append(seg)
    return {"source_id": source_id, "media_path": "/abs/video.mp4", "backend": "test", "language": "en",
            "duration_sec": round(t + 1.0, 3), "created_at": "2026-11-13T15:00:00Z", "segments": segs}


def filler(n, tag="filler"):
    """n 10-word sentences (4 s each incl. gap)."""
    return [f"This is {tag} sentence number {i} with some words here." for i in range(n)]


def write_transcript(settings, source_id, transcript):
    path = settings.transcripts_dir / f"{source_id}.json"
    transcript = dict(transcript, source_id=source_id)
    path.write_text(json.dumps(transcript), encoding="utf-8")
    db.update_source(source_id, transcript_path=str(path))
    return path


def load_tr(source_id):
    from pathlib import Path
    return json.loads(Path(db.require_source(source_id).transcript_path).read_text(encoding="utf-8"))


@pytest.fixture
def source_with_transcript(make_source, settings):
    def _make(sentences, **kw):
        sid = make_source(status="transcribed", **kw)
        write_transcript(settings, sid, build_transcript(sentences))
        return sid
    return _make


def score_by_keyword(keyword="LAUNCH", high=9, low=3):
    def handler(req):
        return {"score": high if keyword in req.user else low, "reason": "because", "hook": "Liftoff now"}
    return handler


def sonnet_clip(start, end, **kw):
    d = {"start_sec": start, "end_sec": end, "title": "Rocket clears the tower",
         "description": "Launch commentary.", "hashtags": ["#Artemis", "#Shorts", "rocket"],
         "hook_text": "Watch this liftoff", "rationale": "Strong payoff."}
    d.update(kw)
    return d


def words_of(transcript):
    return pick.transcript_words(transcript)


CL = pick.ClipLength(20, 45, 59)

# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


def test_empty_transcript_has_no_windows():
    assert pick.make_windows({"segments": []}) == []
    assert pick.make_windows({"segments": [{"start": 0, "end": 1, "text": "", "words": []}]}) == []


def test_short_video_single_window():
    tr = build_transcript(["Hello there everyone.", "This is short."])
    ws = pick.make_windows(tr)
    assert len(ws) == 1
    assert ws[0].start_sec == 0.0 and ws[0].text == "Hello there everyone. This is short."
    assert ws[0].end_sec == tr["segments"][-1]["end"]


def test_windows_cover_transcript_on_boundaries():
    tr = build_transcript(filler(60))  # 240 s
    ws = pick.make_windows(tr, 60, 30)
    seg_starts = {s["start"] for s in tr["segments"]}
    seg_ends = {s["end"] for s in tr["segments"]}
    for w in ws:
        assert w.start_sec in seg_starts and w.end_sec in seg_ends
        assert w.text.endswith(".")
        assert 30 <= w.duration <= 60
    # starts roughly `stride` apart, and every segment is inside some window
    gaps = [b.start_sec - a.start_sec for a, b in zip(ws, ws[1:])]
    assert all(25 <= g <= 36 for g in gaps[:-1])
    for s in tr["segments"]:
        assert any(w.start_sec <= s["start"] and s["end"] <= w.end_sec for w in ws)
    assert ws[0].start_sec == 0.0 and ws[-1].end_sec == tr["segments"][-1]["end"]


def test_windows_do_not_span_long_silence():
    sents = filler(5, "a") + [(120.0, "After a long silence we continue talking here today.")] + filler(5, "b")
    tr = build_transcript(sents)
    gap_start = tr["segments"][4]["end"]
    gap_end = tr["segments"][5]["start"]
    ws = pick.make_windows(tr, 60, 30)
    assert ws
    for w in ws:
        assert not (w.start_sec < gap_start + 1 and w.end_sec > gap_end - 1)
    assert any(w.start_sec >= gap_end - 0.01 for w in ws)


def test_min_sec_drops_tiny_islands():
    sents = filler(10) + [(60.0, "Tiny island.")] + [(60.0, "Another.")] + filler(10, "late")
    tr = build_transcript(sents)
    ws = pick.make_windows(tr, 60, 30, min_sec=20)
    assert all(w.duration >= 20 for w in ws)
    assert not any("Tiny island" in w.text for w in ws)


def test_segments_without_word_timings():
    tr = build_transcript(filler(30), words=False)
    ws = pick.make_windows(tr, 60, 30)
    assert len(ws) >= 3 and all(w.duration <= 60 + 1e-6 for w in ws)


def test_very_long_segment_is_split():
    tr = {"segments": [{"id": 0, "start": 0.0, "end": 300.0, "text": " ".join(["word"] * 600)}]}
    ws = pick.make_windows(tr, 60, 30)
    assert len(ws) > 5 and all(w.duration <= 60 + 1e-6 for w in ws)


def test_long_video_capped_with_even_sample():
    tr = build_transcript(filler(2700))  # ~3 h
    full = pick.make_windows(tr, 60, 30)
    assert len(full) > 300
    capped = pick.make_windows(tr, 60, 30, max_windows=40)
    assert len(capped) == 40
    assert capped[0] == full[0] and capped[-1] == full[-1]
    starts = [w.start_sec for w in capped]
    steps = [b - a for a, b in zip(starts, starts[1:])]
    assert max(steps) < 1.3 * min(steps)  # evenly spread


def test_sample_windows_edge_cases():
    ws = [pick.Window(i, i + 1, str(i)) for i in range(10)]
    assert pick.sample_windows(ws, 0) == []
    assert pick.sample_windows(ws, 1) == [ws[5]]
    assert pick.sample_windows(ws, 20) == ws
    assert [w.text for w in pick.sample_windows(ws, 4)] == ["0", "3", "6", "9"]


# ---------------------------------------------------------------------------
# credit
# ---------------------------------------------------------------------------


def test_credit_line(make_source):
    gov = db.require_source(make_source())
    assert pick.credit_line(gov) == "Video: NASA (public domain)"
    cc = db.require_source(make_source(source_type="cc_by_youtube", channel="Physics Girl", licence="cc_by"))
    assert pick.credit_line(cc) == "Video: Physics Girl (CC BY)"
    anon = db.require_source(make_source(source_type="congress", channel=None, licence_evidence={}))
    assert pick.credit_line(anon) == "Video: U.S. Congress (public domain)"


# ---------------------------------------------------------------------------
# post-processing
# ---------------------------------------------------------------------------


def _pp(raw, tr, **kw):
    words = words_of(tr)
    kw.setdefault("allowed", [(0.0, tr["duration_sec"])])
    kw.setdefault("clip_length", CL)
    kw.setdefault("max_clips", 2)
    kw.setdefault("default_hashtags", ["#Shorts", "#Science"])
    kw.setdefault("duration", tr["duration_sec"])
    return pick.postprocess_clips(raw, words, **kw)


def test_snap_to_sentence_boundaries():
    tr = build_transcript(filler(30))  # sentence i starts at 4.0*i (incl. 0.4 gap -> 4.4 spacing)
    segs = tr["segments"]
    s_req = segs[3]["start"] + 0.5      # mid-sentence, inside a word
    e_req = segs[12]["end"] - 1.0       # just before the sentence end
    acc, rej = _pp([sonnet_clip(s_req, e_req)], tr)
    assert not rej
    c = acc[0]
    assert c.start_sec == pytest.approx(segs[3]["start"] - pick.LEAD_SEC)
    assert c.end_sec == pytest.approx(segs[12]["end"] + pick.TAIL_SEC)
    assert CL.min <= c.end_sec - c.start_sec <= CL.max


def test_start_inside_word_includes_that_word():
    tr = build_transcript([" ".join(f"w{i}" for i in range(200)) + "."])  # one long sentence
    words = words_of(tr)
    acc, rej = _pp([sonnet_clip(words[10].start + 0.2, words[80].start + 0.1)], tr)
    assert not rej
    assert acc[0].start_sec == pytest.approx(words[9].end)  # just before word 10, not cutting word 9
    assert acc[0].start_sec <= words[10].start


def test_too_long_is_clamped_to_sentence_end():
    tr = build_transcript(filler(40))
    segs = tr["segments"]
    acc, rej = _pp([sonnet_clip(segs[0]["start"], segs[30]["end"])], tr)
    assert not rej
    c = acc[0]
    assert c.end_sec - c.start_sec <= CL.max
    assert any(c.end_sec == pytest.approx(s["end"] + pick.TAIL_SEC) for s in segs)


def test_too_short_is_extended_or_rejected():
    tr = build_transcript(filler(40))
    segs = tr["segments"]
    acc, rej = _pp([sonnet_clip(segs[5]["start"], segs[5]["end"])], tr)
    assert not rej and acc[0].end_sec - acc[0].start_sec >= CL.min
    tiny = build_transcript(["Only a few words here."])
    acc, rej = _pp([sonnet_clip(0.0, 1.5)], tiny)
    assert not acc and rej[0].reason == "cannot reach minimum length"


def test_clip_at_end_extends_backwards():
    tr = build_transcript(filler(20))
    segs = tr["segments"]
    acc, rej = _pp([sonnet_clip(segs[-2]["start"], segs[-1]["end"])], tr)
    assert not rej
    assert acc[0].end_sec - acc[0].start_sec >= CL.min
    assert acc[0].start_sec < segs[-2]["start"]


@pytest.mark.parametrize("bad, reason", [
    (sonnet_clip("ten", 40), "non-numeric"),
    (sonnet_clip(None, 40), "non-numeric"),
    (sonnet_clip(float("nan"), 40), "non-numeric"),
    (sonnet_clip(40, 10), "invalid range"),
    (sonnet_clip(-5, 30), "invalid range"),
    (sonnet_clip(5000, 5040), "invalid range"),
    (sonnet_clip(100, 140), "outside the moments"),
    ("not a dict", "not an object"),
])
def test_bad_model_times_rejected(bad, reason):
    tr = build_transcript(filler(60))
    acc, rej = _pp([bad], tr, allowed=[(0.0, 80.0)])
    assert not acc and reason in rej[0].reason


def test_non_list_clips_is_empty():
    tr = build_transcript(filler(20))
    assert _pp(None, tr) == ([], [])
    assert _pp({"start_sec": 1}, tr) == ([], [])


def test_overlap_keeps_better_ranked_and_respects_existing():
    tr = build_transcript(filler(60))
    segs = tr["segments"]
    a = sonnet_clip(segs[0]["start"], segs[10]["end"], title="First")
    b = sonnet_clip(segs[5]["start"], segs[15]["end"], title="Second")
    acc, rej = _pp([a, b], tr)
    assert [c.title for c in acc] == ["First"] and "overlaps a better clip" in rej[0].reason
    acc, rej = _pp([a], tr, existing=[(segs[2]["start"], segs[8]["end"])])
    assert not acc and "existing" in rej[0].reason
    # duplicates are overlaps too
    acc, rej = _pp([a, dict(a)], tr)
    assert len(acc) == 1 and len(rej) == 1


def test_max_clips_enforced():
    tr = build_transcript(filler(100))
    segs = tr["segments"]
    raw = [sonnet_clip(segs[i]["start"], segs[i + 8]["end"], title=f"T{i}") for i in (0, 20, 40)]
    acc, rej = _pp(raw, tr, max_clips=2)
    assert [c.title for c in acc] == ["T0", "T20"] and rej[0].rank == 2


def test_title_hashtag_and_quote_cleaning():
    tr = build_transcript(["We have liftoff of the rocket today.", *filler(20)])
    segs = tr["segments"]
    long_title = '"' + "Astronaut <b>explains</b> " + "very " * 40 + 'long title"'
    raw = [sonnet_clip(segs[0]["start"], segs[8]["end"], title=long_title,
                       hashtags=["#Shorts", "#artemis", "Artemis", "#!!!", "#2026", "#a b"] + [f"#t{i}" for i in range(9)],
                       hook_text='"We have liftoff" in seconds')]
    acc, rej = _pp(raw, tr)
    assert not rej
    c = acc[0]
    assert len(c.title) <= 100 and "<" not in c.title and ">" not in c.title and not c.title.startswith('"')
    assert c.hashtags[:2] == ["#Shorts", "#Science"]
    assert len(c.hashtags) == pick.MAX_HASHTAGS
    assert [h.lower() for h in c.hashtags].count("#artemis") == 1 and "#2026" not in c.hashtags
    # fabricated quote -> rejected
    raw[0]["hook_text"] = '"We found aliens" says NASA'
    acc, rej = _pp(raw, tr)
    assert not acc and "quote not in clip" in rej[0].reason


def test_haiku_score_attached_from_overlapping_window():
    tr = build_transcript(filler(60))
    segs = tr["segments"]
    acc, _ = _pp([sonnet_clip(segs[0]["start"], segs[10]["end"])], tr,
                 window_scores=[(0.0, 60.0, 8), (200.0, 260.0, 9)])
    assert acc[0].haiku_score == 8.0


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def test_prompts_are_stable_and_contain_strategy(strategy, clock):
    sp = pick.score_system_prompt(strategy)
    assert sp == pick.score_system_prompt(strategy)
    for needle in (strategy["niche"], strategy["hook_style"], "NASA Artemis", strategy["experiment"]["id"]):
        assert needle in sp
    fp = pick.finalize_system_prompt(strategy)
    assert strategy["title_style"] in fp and "#Shorts #Science" in fp
    assert "2026" not in sp.replace(strategy["experiment"]["id"], "")  # no dates/timestamps
    # experiment over -> note disappears but prompt still valid
    clock.set(clock.now.replace(month=12))
    assert strategy["experiment"]["id"] not in pick.score_system_prompt(strategy)


# ---------------------------------------------------------------------------
# pick_clips end to end (fake LLM)
# ---------------------------------------------------------------------------


def launch_sentences():
    return (filler(30, "intro")
            + [f"LAUNCH sequence step {i} and the rocket climbs higher now." for i in range(15)]
            + filler(30, "outro"))


def test_pick_clips_happy_path(fake_llm, clock, source_with_transcript, strategy):
    sid = source_with_transcript(launch_sentences())
    tr = load_tr(sid)
    launch_segs = [s for s in tr["segments"] if "LAUNCH" in s["text"]]
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword()
    fake_llm.by_purpose["pick.finalize"] = lambda req: {"clips": [
        sonnet_clip(launch_segs[0]["start"] + 0.2, launch_segs[9]["end"] - 0.5)]}

    ids = pick.pick_clips(sid)

    purposes = fake_llm.purposes()
    n_windows = purposes.count("pick.score_window")
    assert n_windows >= 5 and purposes.count("pick.finalize") == 1 and purposes[-1] == "pick.finalize"
    # cache-friendly: identical system prompt for every window, window text only in user turn
    haiku = [c for c in fake_llm.calls if c.purpose == "pick.score_window"]
    assert len({c.system for c in haiku}) == 1 and all(c.cache_system for c in haiku)
    assert all("Transcript:" in c.user for c in haiku) and "Transcript:" not in haiku[0].system[0]
    assert all(c.tier == "haiku" and c.source_id == sid and c.json_schema == pick.SCORE_SCHEMA for c in haiku)
    son = fake_llm.calls[-1]
    assert son.tier == "sonnet" and "## Moment A" in son.user and "[" in son.user
    assert son.user.count("## Moment") <= strategy["windows_to_escalate"]
    assert "LAUNCH" in son.user

    assert len(ids) == 1
    clip = db.require_clip(ids[0])
    assert clip.start_sec == pytest.approx(launch_segs[0]["start"] - pick.LEAD_SEC)
    assert clip.end_sec == pytest.approx(launch_segs[9]["end"] + pick.TAIL_SEC)
    assert clip.haiku_score == 9.0 and clip.sonnet_rationale == "Strong payoff."
    assert clip.strategy_version == strategy.version
    assert clip.credit_text == "Video: NASA (public domain)"
    assert clip.hashtags[:2] == ["#Shorts", "#Science"] and "#Artemis" in clip.hashtags
    assert clip.experiment_id == strategy["experiment"]["id"] and clip.experiment_variant == "control"
    assert clip.licence == "us_gov_public_domain"
    src = db.require_source(sid)
    assert src.status == "picked"
    assert db.kv_get(f"pick:scores:{sid}") is None
    ev = db.list_events(module="pick")[0]
    assert ev.data["windows_scored"] == n_windows and ev.data["escalated"] >= 1
    assert ev.data["clips_created"] == 1 and ev.data["cost_usd"] > 0


def test_system_prompt_identical_across_sources(fake_llm, clock, source_with_transcript):
    fake_llm.by_purpose["pick.score_window"] = {"score": 2, "reason": "meh", "hook": ""}
    a = source_with_transcript(filler(40, "alpha"), title="Alpha")
    b = source_with_transcript(filler(40, "beta"), title="Beta", channel="NASA Goddard")
    pick.pick_clips(a)
    pick.pick_clips(b)
    systems = {c.system for c in fake_llm.calls}
    assert len(systems) == 1
    assert any("Alpha" in c.user for c in fake_llm.calls) and any("Beta" in c.user for c in fake_llm.calls)


def test_nothing_qualifies_marks_done(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(filler(40))
    fake_llm.by_purpose["pick.score_window"] = {"score": 6, "reason": "ok", "hook": ""}  # min is 7
    assert pick.pick_clips(sid) == []
    assert "pick.finalize" not in fake_llm.purposes()
    src = db.require_source(sid)
    assert src.status == "done" and "min_haiku_score" in src.status_reason


def test_escalation_picks_top_distinct_windows(fake_llm, clock, source_with_transcript, strategy):
    sents = (filler(20, "a") + [f"HOT one {i} moment sentence here we go." for i in range(15)]
             + filler(20, "b") + [f"HOT two {i} moment sentence here we go." for i in range(15)]
             + filler(20, "c"))
    sid = source_with_transcript(sents)
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword("HOT", high=8)
    fake_llm.by_purpose["pick.finalize"] = {"clips": []}
    pick.pick_clips(sid)
    son = fake_llm.calls[-1]
    assert son.purpose == "pick.finalize"
    assert "HOT one" in son.user and "HOT two" in son.user
    n = son.user.count("## Moment")
    assert 2 <= n <= strategy["windows_to_escalate"]
    assert db.require_source(sid).status == "done"  # finalize returned nothing


def test_window_cap_limits_haiku_calls(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(filler(400))  # ~27 min -> ~50 windows
    fake_llm.by_purpose["pick.score_window"] = {"score": 1, "reason": "", "hook": ""}
    pick.pick_clips(sid, max_windows=7)
    assert fake_llm.purposes().count("pick.score_window") == 7
    sid2 = source_with_transcript(filler(400))
    db.kv_set("pick:max_windows", 4)
    pick.pick_clips(sid2)
    assert fake_llm.purposes().count("pick.score_window") == 11


def test_malformed_window_score_skipped(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(launch_sentences())
    calls = {"n": 0}

    def scorer(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return "this is not json"
        if "LAUNCH" in req.user:
            return {"score": 9, "reason": "r", "hook": ""}
        return {"score": 11, "reason": "out of range", "hook": ""}  # fails client-side validation

    fake_llm.by_purpose["pick.score_window"] = scorer
    fake_llm.by_purpose["pick.finalize"] = {"clips": []}
    pick.pick_clips(sid)
    ev = db.list_events(module="pick")[0]
    assert ev.data["score_errors"] >= 1 and ev.data["escalated"] >= 1


def test_all_windows_unscorable_fails(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(filler(30))
    fake_llm.by_purpose["pick.score_window"] = llm.FakeReply(text="", stop_reason="refusal")
    with pytest.raises(pick.PickError):
        pick.pick_clips(sid)
    assert db.require_source(sid).status == "failed"


def test_finalize_malformed_output_fails_source(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(launch_sentences())
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword()
    fake_llm.by_purpose["pick.finalize"] = "definitely not json"
    with pytest.raises(pick.PickError, match="finalize failed"):
        pick.pick_clips(sid)
    src = db.require_source(sid)
    assert src.status == "failed" and "LLMOutputError" in src.status_reason
    assert fake_llm.purposes().count("pick.finalize") == 2  # llm retried once
    assert not db.list_clips(source_id=sid)
    assert db.list_events(module="pick", level="error")
    # stage-1 scores survive for a cheap retry
    assert db.kv_get(f"pick:scores:{sid}")["scores"]


def test_finalize_out_of_range_times_rejected(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(launch_sentences())
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword()
    fake_llm.by_purpose["pick.finalize"] = {"clips": [sonnet_clip(1.0, 40.0), sonnet_clip(9000, 9040)]}
    assert pick.pick_clips(sid) == []
    src = db.require_source(sid)
    assert src.status == "done"
    ev = db.list_events(module="pick")[0]
    assert len(ev.data["rejected"]) == 2


def test_wrong_status_and_missing_transcript(make_source, fake_llm):
    sid = make_source(status="fetched")
    with pytest.raises(pick.PickError):
        pick.pick_clips(sid)
    assert db.require_source(sid).status == "fetched"  # untouched
    sid2 = make_source(status="transcribed")
    with pytest.raises(pick.PickError, match="transcript unreadable"):
        pick.pick_clips(sid2)
    assert db.require_source(sid2).status == "failed"
    assert not fake_llm.calls


# ---------------------------------------------------------------------------
# budget exhaustion and resumption
# ---------------------------------------------------------------------------


def _pause_after(n_calls, fake_llm, base):
    count = {"n": 0}

    def scorer(req):
        count["n"] += 1
        if count["n"] == n_calls:
            budget.set_paused(True, "test")
        return base(req)
    return scorer


def test_budget_exhausted_mid_scoring_resumes_without_rescoring(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(launch_sentences())
    tr = load_tr(sid)
    launch_segs = [s for s in tr["segments"] if "LAUNCH" in s["text"]]
    fake_llm.by_purpose["pick.score_window"] = _pause_after(3, fake_llm, score_by_keyword())
    fake_llm.by_purpose["pick.finalize"] = {"clips": [
        sonnet_clip(launch_segs[0]["start"], launch_segs[9]["end"])]}

    with pytest.raises(budget.BudgetExceeded):
        pick.pick_clips(sid)
    assert db.require_source(sid).status == "transcribed"
    assert not db.list_clips(source_id=sid)
    saved = db.kv_get(f"pick:scores:{sid}")
    assert len(saved["scores"]) == 3 and saved["cost_usd"] > 0
    assert any("will resume" in e.message for e in db.list_events(module="pick", level="warning"))

    # next day: budget available again
    budget.set_paused(False)
    first_run = fake_llm.purposes().count("pick.score_window")
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword()
    ids = pick.pick_clips(sid)
    second_run = fake_llm.purposes().count("pick.score_window") - first_run
    total_windows = db.list_events(module="pick")[0].data["windows_considered"]
    assert second_run == total_windows - 3
    assert len(ids) == 1 and db.require_source(sid).status == "picked"
    assert db.list_events(module="pick")[0].data["windows_reused"] == 3

    # re-running a picked source never duplicates clips
    with pytest.raises(pick.PickError):
        pick.pick_clips(sid)
    assert len(db.list_clips(source_id=sid)) == 1


def test_budget_exhausted_before_finalize(fake_llm, clock, source_with_transcript):
    sid = source_with_transcript(launch_sentences())
    fake_llm.by_purpose["pick.finalize"] = {"clips": []}
    # pause exactly before the Sonnet call: pause from inside the last Haiku reply
    total = len(pick.make_windows(load_tr(sid), 60, 30, min_sec=20, max_windows=40))
    fake_llm.by_purpose["pick.score_window"] = _pause_after(total, fake_llm, score_by_keyword())
    with pytest.raises(budget.BudgetExceeded):
        pick.pick_clips(sid)
    assert "pick.finalize" not in fake_llm.purposes()
    assert db.require_source(sid).status == "transcribed"
    budget.set_paused(False)
    before = len(fake_llm.calls)
    pick.pick_clips(sid)
    assert [c.purpose for c in fake_llm.calls[before:]] == ["pick.finalize"]


def test_strategy_version_change_invalidates_scores(fake_llm, clock, source_with_transcript, strategy):
    sid = source_with_transcript(filler(40))
    fake_llm.by_purpose["pick.score_window"] = _pause_after(2, fake_llm, lambda r: {"score": 1, "reason": "", "hook": ""})
    with pytest.raises(budget.BudgetExceeded):
        pick.pick_clips(sid)
    budget.set_paused(False)
    fake_llm.by_purpose["pick.score_window"] = {"score": 1, "reason": "", "hook": ""}
    newer = strategy.updated(version=strategy.version + 1)
    pick.pick_clips(sid, newer)
    ev = db.list_events(module="pick")[0]
    assert ev.data["windows_reused"] == 0 and ev.data["windows_scored"] == ev.data["windows_considered"]


# ---------------------------------------------------------------------------
# experiments
# ---------------------------------------------------------------------------


def _two_clip_setup(fake_llm, source_with_transcript):
    sents = (filler(15, "a") + [f"LAUNCH one {i} the rocket climbs higher now ok." for i in range(15)]
             + filler(20, "b") + [f"LAUNCH two {i} the rocket climbs higher now ok." for i in range(15)]
             + filler(15, "c"))
    sid = source_with_transcript(sents)
    tr = load_tr(sid)
    one = [s for s in tr["segments"] if "LAUNCH one" in s["text"]]
    two = [s for s in tr["segments"] if "LAUNCH two" in s["text"]]
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword()
    fake_llm.by_purpose["pick.finalize"] = {"clips": [
        sonnet_clip(one[0]["start"], one[9]["end"], title="One"),
        sonnet_clip(two[0]["start"], two[9]["end"], title="Two")]}
    return sid


def test_experiment_arms_recorded_and_instructed(fake_llm, clock, source_with_transcript, strategy, make_clip):
    exp_id = strategy["experiment"]["id"]
    sid = _two_clip_setup(fake_llm, source_with_transcript)
    ids = pick.pick_clips(sid)
    clips = [db.require_clip(i) for i in ids]
    assert [(c.title, c.experiment_id, c.experiment_variant) for c in clips] == [
        ("One", exp_id, "control"), ("Two", exp_id, "variant")]
    son = fake_llm.calls[-1]
    assert "Clip 1 uses experiment arm CONTROL" in son.user and "Clip 2 uses experiment arm VARIANT" in son.user
    assert strategy["experiment"]["variant"] in son.user
    # alternation continues across sources: one more existing experiment clip -> start on variant
    make_clip(experiment_id=exp_id, experiment_variant="control")
    sid2 = _two_clip_setup(fake_llm, source_with_transcript)
    ids2 = pick.pick_clips(sid2)
    assert [db.require_clip(i).experiment_variant for i in ids2] == ["variant", "control"]


def test_no_experiment_when_inactive(fake_llm, clock, source_with_transcript):
    clock.set(clock.now.replace(month=12, day=20))
    sid = _two_clip_setup(fake_llm, source_with_transcript)
    ids = pick.pick_clips(sid)
    assert ids and all(db.require_clip(i).experiment_id is None for i in ids)
    assert "experiment arm" not in fake_llm.calls[-1].user


def test_cost_estimate_inputs_are_small(strategy, clock, source_with_transcript, fake_llm):
    """Guard prompt sizes that the cost estimate in the report relies on."""
    sp = pick.score_system_prompt(strategy)
    fp = pick.finalize_system_prompt(strategy)
    assert llm.approx_tokens(sp) < 800 and llm.approx_tokens(fp) < 900
    sid = source_with_transcript(launch_sentences())
    fake_llm.by_purpose["pick.score_window"] = score_by_keyword()
    fake_llm.by_purpose["pick.finalize"] = {"clips": []}
    pick.pick_clips(sid)
    haiku_user = max(llm.approx_tokens(c.user) for c in fake_llm.calls if c.purpose == "pick.score_window")
    sonnet_user = llm.approx_tokens(fake_llm.calls[-1].user)
    assert haiku_user < 400 and sonnet_user < 2500

