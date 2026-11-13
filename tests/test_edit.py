"""Tests for edit.py: pure ASS / ffmpeg-command builders, render_clip with injected
subprocess fakes, and one small real-ffmpeg integration test (skipped without ffmpeg)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import db
import edit
from edit import MediaInfo, RenderError

STYLE = {
    "font": "Inter Black", "font_size": 78, "primary_color": "#FFFFFF", "highlight_color": "#FFD400",
    "outline_color": "#000000", "outline_px": 5, "max_words_per_line": 3, "position": "lower_third",
    "uppercase": True, "animation": "word_pop",
}


def W(start: float, end: float, word: str) -> dict:
    return {"start": start, "end": end, "word": word, "prob": 0.9}


SENTENCE = [W(10.0, 10.3, "We"), W(10.35, 10.6, "have"), W(10.65, 11.1, "liftoff!"),
            W(11.6, 11.9, "Four"), W(11.95, 12.5, "astronauts"), W(12.55, 12.8, "are"),
            W(12.85, 13.2, "headed"), W(13.25, 13.4, "to"), W(13.45, 13.9, "the"), W(13.95, 14.4, "Moon.")]


def dialogues(ass: str, style: str | None = None) -> list[tuple[str, str, str, str]]:
    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            parts = line[len("Dialogue: "):].split(",", 9)
            if style is None or parts[3] == style:
                out.append((parts[1], parts[2], parts[3], parts[9]))
    return out


def visible_text(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------

def test_ass_color_and_time():
    assert edit.ass_color("#FFD400") == "&H0000D4FF"
    assert edit.ass_color("12ab34") == "&H0034AB12"
    assert edit.ass_color("not a colour", "#000000") == "&H00000000"
    assert edit.ass_color("#FF000080") == "&H7F0000FF"  # CSS alpha 0x80 -> ASS alpha 0x7F
    assert edit.ass_time(0) == "0:00:00.00"
    assert edit.ass_time(61.234) == "0:01:01.23"
    assert edit.ass_time(3725.5) == "1:02:05.50"


def test_ass_escape_neutralises_override_syntax():
    s = edit.ass_escape("a {\\b1} b\\Nc\nd\te")
    assert "{" not in s and "}" not in s and "\\" not in s
    assert s == "a (/b1) b/Nc d e"


def test_group_words_respects_budget_pauses_and_sentence_ends():
    ws = edit._clean_words(SENTENCE, 10.0, 10.0, upper=False)
    groups = [[w.text for w in g] for g in edit.group_words(ws, max_words=3, max_chars=20)]
    assert groups[0] == ["We", "have", "liftoff!"]       # sentence end + pause
    assert all(len(g) <= 3 for g in groups)
    assert sum(len(g) for g in groups) == len(SENTENCE)
    tight = edit.group_words(ws, max_words=5, max_chars=8)
    assert all(sum(len(w.text) for w in g) + len(g) - 1 <= 10 or len(g) == 1 for g in tight)


# ---------------------------------------------------------------------------
# build_ass
# ---------------------------------------------------------------------------

def test_build_ass_structure_hook_and_credit():
    ass = edit.build_ass(SENTENCE, 10.0, STYLE, hook_text="Wait for the roar", credit_text="Video: NASA (public domain)",
                         duration=5.0)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Caption,Inter Black,78,&H00FFFFFF" in ass
    credit = dialogues(ass, "Credit")
    assert len(credit) == 1 and credit[0][:2] == ("0:00:00.00", "0:00:05.00")
    assert visible_text(credit[0][3]) == "Video: NASA (public domain)"
    hook = dialogues(ass, "Hook")
    assert len(hook) == 1 and hook[0][:2] == ("0:00:00.00", "0:00:03.00")
    assert visible_text(hook[0][3]) == "Wait for the roar"
    # hook sits in the top third, credit above it
    assert re.search(r"\\pos\(540,(\d+)\)", hook[0][3]).group(1) == str(edit.HOOK_Y)
    assert edit.CREDIT_Y < edit.HOOK_Y < 1920 / 3


def test_build_ass_word_pop_highlights_each_word_in_turn():
    ass = edit.build_ass(SENTENCE, 10.0, STYLE, duration=5.0)
    caps = dialogues(ass, "Caption")
    assert len(caps) == len(SENTENCE)                       # one event per spoken word
    assert caps[0][0] == "0:00:00.00"                       # shifted by clip_start
    for (_, _, _, text), w in zip(caps, SENTENCE):
        hl = re.search(r"\{\\c&H00D4FF&[^}]*\}([^{]+)\{\\r\}", text)
        assert hl and hl.group(1) == w["word"].upper()
    # events of one line are contiguous (no flicker between words)
    assert caps[0][1] == caps[1][0] and caps[1][1] == caps[2][0]
    # caption anchored above the bottom 20% of the frame
    y = int(re.search(r"\\pos\(540,(\d+)\)", caps[0][3]).group(1))
    assert y <= 1920 * 0.8


def test_build_ass_clips_words_to_range_and_lowercase_option():
    words = [W(5.0, 5.5, "before"), *SENTENCE, W(20.0, 20.5, "after")]
    style = {**STYLE, "uppercase": False, "animation": "none", "position": "center"}
    ass = edit.build_ass(words, 10.0, style, duration=3.0)
    text = " ".join(visible_text(d[3]) for d in dialogues(ass, "Caption"))
    assert "before" not in text and "after" not in text
    assert "liftoff!" in text and "LIFTOFF" not in text
    assert all(d[1] <= "0:00:03.00" for d in dialogues(ass, "Caption"))
    assert "\\pos(540,960)" in ass


def test_build_ass_karaoke_uses_kf_tags():
    ass = edit.build_ass(SENTENCE[:3], 10.0, {**STYLE, "animation": "karaoke", "uppercase": False}, duration=2.0)
    caps = dialogues(ass, "CaptionK")
    assert len(caps) == 1
    assert caps[0][3].count("\\kf") == 3
    assert "Style: CaptionK,Inter Black,78,&H0000D4FF,&H00FFFFFF" in ass


def test_build_ass_no_words_and_hostile_text():
    ass = edit.build_ass([], 0.0, {}, hook_text="{\\fs200}Big} claim\\N", credit_text="Video: A, B {x}",
                         duration=1.5)
    assert dialogues(ass, "Caption") == []
    hook = dialogues(ass, "Hook")[0]
    assert hook[1] == "0:00:01.50"                          # hook never outlives the clip
    body = visible_text(hook[3])
    assert "\\fs200" not in body and "{" not in body
    assert "Video: A, B (x)" in ass
    # a word containing braces cannot inject override tags
    ass2 = edit.build_ass([W(0.1, 0.5, "{\\an8}hi")], 0.0, STYLE, duration=1.0)
    assert "\\an8" not in ass2 and "(/AN8)HI" in ass2


def test_build_ass_long_hook_wraps_and_style_defaults():
    hook = "This is a very long hook sentence that keeps going and going well past any sane length for a short"
    ass = edit.build_ass([], 0.0, {"font_size": "bogus", "position": "nowhere"}, hook_text=hook, duration=10)
    text = dialogues(ass, "Hook")[0][3]
    lines = visible_text(text).split("\\N")
    assert 2 <= len(lines) <= 4
    assert lines[-1].endswith("\u2026")
    assert "Style: Caption,Inter Black,78," in ass          # defaults applied


def test_merged_caption_style_tolerates_unknown_keys():
    st = edit.merged_caption_style({"framing": "blur_fill", "max_words_per_line": 99, "_comment": "x"})
    assert st["framing"] == "blur_fill" and st["max_words_per_line"] == 8 and "_comment" not in st


# ---------------------------------------------------------------------------
# framing + ffmpeg command
# ---------------------------------------------------------------------------

WIDE = MediaInfo(1920, 1080, 600.0, True)


@pytest.mark.parametrize("info,mode,expected", [
    (WIDE, "auto", "center_crop"),
    (WIDE, None, "center_crop"),
    (WIDE, "blur_fill", "blur_fill"),
    (MediaInfo(1440, 1080, 10, True), "auto", "blur_fill"),
    (MediaInfo(1080, 1080, 10, True), "auto", "blur_fill"),
    (MediaInfo(1080, 1920, 10, True), "blur_fill", "fit"),
    (MediaInfo(720, 1280, 10, True), "auto", "fit"),
    (WIDE, "weird", "center_crop"),
])
def test_resolve_framing(info, mode, expected):
    assert edit.resolve_framing(mode, info) == expected


def test_build_ffmpeg_cmd_center_crop(settings, tmp_path):
    cmd = edit.build_ffmpeg_cmd(Path("/m/src.mp4"), tmp_path / "1.ass", tmp_path / "1.mp4", 12.5, 57.25, settings,
                                info=WIDE, framing="center_crop")
    assert cmd[0] == settings.ffmpeg_bin
    i_ss, i_in = cmd.index("-ss"), cmd.index("-i")
    assert i_ss < i_in and cmd[i_ss + 1] == "12.500" and cmd[i_in + 1] == "/m/src.mp4"   # fast input seek
    assert cmd[cmd.index("-t") + 1] == "44.750"
    assert cmd.index("-t") > i_in                                       # accurate output-side trim
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "fps=30" in graph and "scale=3412:1920" in graph and "crop=1080:1920:1166:0" in graph
    assert "ass=filename=" in graph and "[aout]" in graph
    for flag, val in (("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-c:a", "aac"), ("-movflags", "+faststart"),
                      ("-r", "30")):
        assert cmd[cmd.index(flag) + 1] == val
    assert cmd[-1] == str(tmp_path / "1.mp4")
    assert "anullsrc" not in " ".join(cmd)


def test_build_ffmpeg_cmd_blur_fill_and_silent_audio(settings, tmp_path):
    info = MediaInfo(1440, 1080, 30.0, has_audio=False)
    cmd = edit.build_ffmpeg_cmd(Path("/m/src.mp4"), tmp_path / "1.ass", tmp_path / "o.mp4", 0, 10, settings,
                                info=info, framing="auto")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "gblur" in graph and "overlay" in graph and "split=2" in graph
    assert "anullsrc" in " ".join(cmd)
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["[vout]", "1:a:0"]


def test_build_ffmpeg_cmd_crop_offset_clamped_and_rejects_bad_range(settings, tmp_path):
    cmd = edit.build_ffmpeg_cmd(Path("s.mp4"), tmp_path / "a.ass", tmp_path / "o.mp4", 0, 5, settings,
                                info=WIDE, framing="center_crop", crop_x=99999)
    assert "crop=1080:1920:2332:0" in cmd[cmd.index("-filter_complex") + 1]
    with pytest.raises(ValueError):
        edit.build_ffmpeg_cmd(Path("s.mp4"), tmp_path / "a.ass", tmp_path / "o.mp4", 5, 5, settings)


def test_filter_path_escaping(settings):
    ass = Path("/data/it's here/a:b,c[1].ass")
    cmd = edit.build_ffmpeg_cmd(Path("s.mp4"), ass, Path("o.mp4"), 0, 1, settings, info=WIDE)
    graph = cmd[cmd.index("-filter_complex") + 1]
    seg = graph.split("ass=filename=", 1)[1].split(",format=yuv420p[vout]")[0]
    # no unescaped graph separators survive inside the path
    assert re.search(r"(?<!\\)[,\[\];]", seg) is None
    assert "a\\\\:b" in seg and "it\\\\\\'s" in seg   # escaped at both levels (as in the ffmpeg docs)


def test_crop_locator_seam():
    src = Path("x.mp4")
    assert edit.crop_x_offset(src, 0, 10, WIDE) == (3412 - 1080) // 2
    try:
        edit.set_crop_locator(lambda s, a, b, w, h: 0.0)
        assert edit.crop_x_offset(src, 0, 10, WIDE) == 0
        edit.set_crop_locator(lambda s, a, b, w, h: 1.0)
        assert edit.crop_x_offset(src, 0, 10, WIDE) == 3412 - 1080
        edit.set_crop_locator(lambda s, a, b, w, h: 1 / 0)
        assert edit.crop_x_offset(src, 0, 10, WIDE) == round(0.5 * 3412 - 540)
    finally:
        edit.set_crop_locator(None)
    assert edit.crop_locator is edit.center_locator


def test_parse_probe_rotation_sar_and_audio():
    data = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
                         "side_data_list": [{"rotation": -90}]}],
            "format": {"duration": "12.5"}}
    info = edit.parse_probe(data)
    assert (info.width, info.height, info.has_audio, info.duration) == (1080, 1920, False, 12.5)
    assert abs(info.fps - 29.97) < 0.01
    anam = edit.parse_probe({"streams": [{"codec_type": "video", "width": 1440, "height": 1080,
                                          "sample_aspect_ratio": "4:3"}, {"codec_type": "audio"}], "format": {}})
    assert (anam.width, anam.height, anam.has_audio) == (1920, 1080, True)
    with pytest.raises(RenderError):
        edit.parse_probe({"streams": [{"codec_type": "audio"}]})


# ---------------------------------------------------------------------------
# render_clip with injected ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def _write_transcript(settings, source_id: int, words: list[dict]) -> Path:
    p = settings.transcripts_dir / f"{source_id}.json"
    p.write_text(json.dumps({"source_id": source_id, "language": "en", "segments": [
        {"id": 0, "start": 0.0, "end": 60.0, "text": " ".join(w["word"] for w in words), "words": words}]}))
    return p


@pytest.fixture
def clip_setup(settings, make_source, make_clip, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not really a video")
    sid = make_source(status="picked")
    tp = _write_transcript(settings, sid, SENTENCE)
    db.update_source(sid, video_path=str(video), transcript_path=str(tp))
    cid = make_clip(source_id=sid, start_sec=10.0, end_sec=40.0, hook_text="Liftoff!",
                    credit_text="Video: NASA (public domain)")
    return cid


class FakeFF:
    def __init__(self, *, fail: str | None = None, out_info: MediaInfo | None = None,
                 src_info: MediaInfo = WIDE):
        self.cmds: list[list[str]] = []
        self.fail = fail
        self.out_info = out_info
        self.src_info = src_info

    def run(self, cmd, *, timeout=None):
        self.cmds.append(cmd)
        if self.fail:
            raise RenderError(self.fail)
        Path(cmd[-1]).write_bytes(b"\x00" * 100)

    def probe(self, path, settings=None):
        if str(path).endswith("video.mp4"):
            return self.src_info
        return self.out_info or MediaInfo(1080, 1920, 30.0, True)


@pytest.fixture
def fakeff(monkeypatch):
    ff = FakeFF()
    monkeypatch.setattr(edit, "run_ffmpeg", ff.run)
    monkeypatch.setattr(edit, "probe_media", ff.probe)
    return ff


def test_render_clip_success_and_idempotent(clip_setup, fakeff, settings):
    out = edit.render_clip(clip_setup)
    assert out == settings.renders_dir / f"{clip_setup}.mp4" and out.exists()
    assert not (settings.renders_dir / f"{clip_setup}.tmp.mp4").exists()
    clip = db.require_clip(clip_setup)
    assert clip.status == "rendered" and clip.render_path == str(out.resolve())
    ass = (settings.renders_dir / f"{clip_setup}.ass").read_text()
    assert "Video: NASA (public domain)" in ass and "LIFTOFF!" in ass
    assert len(dialogues(ass, "Caption")) == len(SENTENCE)
    cmd = fakeff.cmds[0]
    assert cmd[cmd.index("-t") + 1] == "30.000" and "crop=1080:1920" in cmd[cmd.index("-filter_complex") + 1]
    ev = db.list_events(module="edit", limit=5)
    assert ev[0].level == "info" and "rendered clip" in ev[0].message
    # re-render is allowed and leaves the clip rendered
    edit.render_clip(clip_setup, framing="blur_fill")
    assert db.require_clip(clip_setup).status == "rendered"
    assert "gblur" in fakeff.cmds[1][fakeff.cmds[1].index("-filter_complex") + 1]


def test_render_clip_ffmpeg_failure_marks_failed(clip_setup, fakeff, settings):
    fakeff.fail = "ffmpeg exited 1: Invalid data found when processing input"
    with pytest.raises(RenderError):
        edit.render_clip(clip_setup)
    clip = db.require_clip(clip_setup)
    assert clip.status == "failed" and "Invalid data" in clip.status_reason
    assert db.list_events(level="error", limit=1)[0].module == "edit"
    # retry from failed works
    fakeff.fail = None
    edit.render_clip(clip_setup)
    assert db.require_clip(clip_setup).status == "rendered"


def test_render_clip_bad_output_fails_verification(clip_setup, fakeff):
    fakeff.out_info = MediaInfo(1920, 1080, 12.0, False)
    with pytest.raises(RenderError, match="output check failed"):
        edit.render_clip(clip_setup)
    reason = db.require_clip(clip_setup).status_reason
    assert "1920x1080" in reason and "no audio" in reason and "duration" in reason


def test_render_clip_refuses_non_renderable_status(make_clip, fakeff):
    cid = make_clip(status="uploaded")
    with pytest.raises(RenderError, match="uploaded"):
        edit.render_clip(cid)
    assert db.require_clip(cid).status == "uploaded"
    assert fakeff.cmds == []


def test_render_clip_missing_video_fails(make_clip, fakeff):
    cid = make_clip()
    with pytest.raises(RenderError, match="source video missing"):
        edit.render_clip(cid)
    assert db.require_clip(cid).status == "failed"


def test_render_clip_without_transcript_or_credit_text(make_source, make_clip, fakeff, settings, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x")
    sid = make_source(status="picked", channel="Some Creator", licence="cc_by")
    db.update_source(sid, video_path=str(video))
    cid = make_clip(source_id=sid, start_sec=0, end_sec=20, credit_text="")
    fakeff.out_info = MediaInfo(1080, 1920, 20.0, True)
    edit.render_clip(cid)
    ass = (settings.renders_dir / f"{cid}.ass").read_text()
    assert dialogues(ass, "Caption") == []
    credit = visible_text(dialogues(ass, "Credit")[0][3])
    assert "Some Creator" in credit and "CC BY" in credit       # credit always rendered
    assert any(e.level == "warning" and "transcript" in e.message for e in db.list_events(module="edit"))


def test_render_clip_long_clip_warns_and_too_long_fails(clip_setup, fakeff, make_clip, make_source, tmp_path):
    db.update_clip(clip_setup, end_sec=10.0 + 90.0)
    fakeff.out_info = MediaInfo(1080, 1920, 90.0, True)
    edit.render_clip(clip_setup)
    assert any(e.level == "warning" and "longer than strategy max" in e.message
               for e in db.list_events(module="edit"))
    db.update_clip(clip_setup, end_sec=10.0 + 200.0)
    with pytest.raises(RenderError, match="Shorts"):
        edit.render_clip(clip_setup)


def test_words_for_clip_local_reader_handles_segments_without_words():
    tr = {"segments": [{"start": 0.0, "end": 2.0, "text": "one two"},
                       {"start": 2.0, "end": 3.0, "text": "x", "words": [W(2.1, 2.4, "three")]}]}
    words = edit._local_words_between(tr, 0.0, 2.5)
    assert [w["word"] for w in words] == ["one", "two", "three"]


# ---------------------------------------------------------------------------
# real ffmpeg (small, fast); skipped automatically when ffmpeg is unavailable
# ---------------------------------------------------------------------------

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")
@pytest.mark.parametrize("size,audio,framing,expect", [
    ("640x360", True, None, "center_crop"),
    ("480x360", False, "blur_fill", "blur_fill"),
])
def test_render_clip_real_ffmpeg(size, audio, framing, expect, settings, make_source, make_clip, tmp_path):
    src = tmp_path / "video.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", f"testsrc2=size={size}:rate=25:duration=4"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(src)]
    subprocess.run(cmd, check=True, timeout=60)
    sid = make_source(status="picked")
    tp = _write_transcript(settings, sid, [W(1.0, 1.4, "Hello"), W(1.5, 1.9, "{world}!"), W(2.2, 2.6, "Bye.")])
    db.update_source(sid, video_path=str(src), transcript_path=str(tp))
    cid = make_clip(source_id=sid, start_sec=0.8, end_sec=2.8, hook_text="Real render", credit_text="Video: NASA")
    out = edit.render_clip(cid, framing=framing)
    info = edit.probe_media(out)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
    assert abs(info.duration - 2.0) < edit.DURATION_TOLERANCE_SEC
    assert db.require_clip(cid).status == "rendered"
    assert db.list_events(module="edit", limit=1)[0].data["framing"] == expect


def test_resolve_font_falls_back_and_reads_bundled_fonts(monkeypatch, tmp_path):
    monkeypatch.setattr(edit, "_installed_families", lambda: frozenset({"dejavu sans", "inter black"}))
    monkeypatch.setattr(edit, "ASSETS_FONTS_DIR", tmp_path)
    assert edit.resolve_font("Montserrat ExtraBold") == "Inter Black"
    (tmp_path / "Montserrat[wght].ttf").write_bytes(b"")
    assert edit.resolve_font("Montserrat ExtraBold") == "Montserrat"
    (tmp_path / "Montserrat-ExtraBold.ttf").write_bytes(b"")
    assert edit.resolve_font("Montserrat ExtraBold") == "Montserrat ExtraBold"
    monkeypatch.setattr(edit, "_installed_families", lambda: frozenset({"dejavu sans"}))
    (tmp_path / "Montserrat-ExtraBold.ttf").unlink()
    (tmp_path / "Montserrat[wght].ttf").unlink()
    assert edit.resolve_font("Nope Font") == "DejaVu Sans"
