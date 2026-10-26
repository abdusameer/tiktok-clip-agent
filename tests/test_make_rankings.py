import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import args as A
from clipagent import manifest as M
from clipagent import plan as P
from clipagent import qa as Q
from clipagent import render as R
from clipagent import runs as RU
from clipagent import style, titles
from clipagent import usage as U

ROOT = Path(__file__).resolve().parents[1]
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def moments(n, base=0.0, dur=11.0, names=None):
    names = names or ["Chat Roasts Streamer", "Mic Falls Over", "Surprise Jump Scare", "Wrong Button Panic", "Loud Slip Up"]
    return [{"rank": n - i, "title": names[i], "start": base + i * 20, "end": base + i * 20 + dur, "verified": True} for i in range(n)]


def video(cat, n=4, base=0.0, dur=11.0, names=None):
    return {"category": cat, "moments": moments(n, base, dur, names)}


# --- skill + style ---------------------------------------------------------
def test_skill_is_manual_only_and_discoverable():
    text = (ROOT / ".claude/skills/make-rankings/SKILL.md").read_text()
    front = text.split("---")[1]
    assert re.search(r"^name: make-rankings$", front, re.M)
    assert re.search(r"^disable-model-invocation: true$", front, re.M)


def test_claude_md_section():
    t = (ROOT / "CLAUDE.md").read_text()
    assert "/make-rankings" in t and "ranking_style.json" in t and "explicit approval" in t


def test_render_constants_come_from_style_file():
    s = style.load()
    assert (R.W, R.H, R.FPS) == (1080, 1920, 30)
    assert R.LABEL_Y == s["label"]["y"] == 1040 and R.CAP_Y == 1190 and R.KEEP_H == 630 and R.BAND_Y == 620
    assert s["video_encode"]["crf"] == 18 and s["audio"]["bitrate"] == "192k" and s["container"]["faststart"] is True
    assert s["audio"]["bleep_profanity"] is False


# --- argument handling -----------------------------------------------------
def test_args_positional_and_flags():
    assert A.parse("a.mp4").video == Path("a.mp4")
    a = A.parse("--video v.mp4 --audio a.m4a --transcript t.srt")
    assert (a.video, a.audio, a.transcript) == (Path("v.mp4"), Path("a.m4a"), Path("t.srt"))
    assert A.parse(["--video", "v.mp4", "--transcript", "t.json"]).audio is None
    assert A.parse('"my video.mp4"').video == Path("my video.mp4")


@pytest.mark.parametrize("bad", ["", "a.mp4 b.mp4", "a.mp4 --video b.mp4", "--video", "--video a.mp4 --video b.mp4", "--bogus x a.mp4",
                                 "--audio a.m4a"])
def test_args_rejected(bad):
    with pytest.raises(A.ArgError):
        A.parse(bad)


def test_validate_missing_file(tmp_path):
    with pytest.raises(A.ArgError, match="not found"):
        A.validate(A.MakeRankingsArgs(tmp_path / "nope.mp4"))


# --- runs, separate audio, supplied transcript, overwrite protection -----------
def test_mux_never_reencodes_video():
    cmd = RU.mux_command(Path("v.mp4"), Path("a.mp4"), Path("o.mp4"))
    assert cmd[cmd.index("-c") + 1] == "copy" and "libx264" not in cmd and "-n" in cmd


def test_run_layout_originals_hashed_and_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_ROOT", str(tmp_path))
    v, t = tmp_path / "v.mp4", tmp_path / "t.srt"
    v.write_bytes(b"video"); t.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    r1, r2 = RU.create_run(v, None, t), RU.create_run(v, None, t)
    assert r1 != r2
    assert {p.name for p in r1.iterdir()} == set(RU.SUBDIRS)
    rec = json.loads((r1 / "source" / "originals.json").read_text())
    assert set(rec["originals"]) == {"video", "transcript"} and len(rec["originals"]["transcript"]["sha256"]) == 64
    assert v.read_bytes() == b"video"                       # original untouched


def test_write_new_refuses_overwrite(tmp_path):
    p = RU.write_new(tmp_path / "x.json", "1")
    with pytest.raises(RU.RunError):
        RU.write_new(p, "2")
    assert p.read_text() == "1"


@needs_ffmpeg
def test_combine_separate_audio_copies_video_and_protects_output(tmp_path):
    v, a, out = tmp_path / "v.mp4", tmp_path / "a.m4a", tmp_path / "c.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=160x90:r=10", "-c:v", "libx264", str(v)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", str(a)], check=True)
    RU.combine(v, a, out)
    info = Q.probe(out)
    assert {s["codec_name"] for s in info["streams"]} == {"h264", "aac"}
    with pytest.raises(RU.RunError):
        RU.combine(v, a, out)


# --- titles, ranks, plan rules ------------------------------------------------
def test_labels_combine_rank_and_title():
    e = {"rank": 4, "title": "KSI Bad Breath"}
    assert titles.label_text(e) == "#4 KSI Bad Breath"


@pytest.mark.parametrize("t", ["#4 KSI Bad Breath", "Wow", "One Two Three Four Five Six", "lowercase title here"])
def test_bad_titles(t):
    with pytest.raises(titles.TitleError):
        titles.validate_title(t)


def test_approved_video_labels_are_corrected_and_valid():
    p = ROOT / "clip_candidates/6a6f378350ec.tiktok1.labels.json"
    if not p.exists():
        pytest.skip("local project data not present")
    entries = json.loads(p.read_text())
    assert [titles.label_text(e) for e in titles.validate_entries(entries)] == [
        "#4 KSI Bad Breath", "#3 IRL Hate Drive-By", "#2 Lacy LOL", "#1 Grok Flaming Ron"]


def test_render_loads_labels_title_only_and_rank_must_match(tmp_path):
    f = tmp_path / "l.json"
    f.write_text(json.dumps([{"rank": r, "title": t} for r, t in ((2, "Mic Falls Over"), (1, "Loud Slip Up"), (3, "Chat Roasts Streamer"), (4, "Wrong Button Panic"))]))
    assert R.load_labels(f, ["4", "3", "2", "1"])["4"] == "Wrong Button Panic"
    with pytest.raises(R.RenderError):
        R.load_labels(f, ["5", "4", "3", "2"])               # rank/clip mismatch
    f.write_text(json.dumps({"4": "#1 Old Style"}))
    with pytest.raises(R.RenderError):
        R.load_labels(f, ["4"])                              # legacy format refused


def test_single_visible_rank_number_on_label_pill(tmp_path):
    spec = {"title": "T", "moments": {"1": {"start": 0, "end": 5, "words": [["a", 0.1, 0.3]], "lines": [{"text": "a", "first": 0, "last": 0}]}}}
    items = R.png_spec(spec, ["1"], tmp_path, {"1": "Mic Falls Over"})["items"]
    pill = next(i for i in items if i["out"].endswith("label1.png"))
    assert pill["blocks"][0]["text"] == "Mic Falls Over" and "#" not in pill["blocks"][0]["text"]
    badge = next(i for i in items if i["out"].endswith("badge1.png"))
    assert badge["blocks"][0]["text"] == "#1"


def test_four_and_five_rank_videos_pass():
    assert P.validate([video("Best Roasts", 4, 0, 12.5)]).ok
    assert P.validate([video("Best Roasts", 5, 0, 10.5)]).ok


def test_three_or_six_ranks_rejected():
    assert not P.validate([video("A", 3, 0, 15)]).ok
    six = [{"rank": 6 - i, "title": f"Moment Number {n}", "start": i * 20, "end": i * 20 + 9, "verified": True}
           for i, n in enumerate("abcdef")]
    assert not P.validate([{"category": "A", "moments": six}]).ok


def test_duration_rules_are_content_driven():
    five_long = P.validate([video("A", 5, 0, 17.0)])                      # 85 s: no 60 s ceiling
    assert five_long.ok and not five_long.warnings
    assert P.validate([video("A", 4, 0, 12.5)]).ok and not P.validate([video("A", 4, 0, 12.5)]).warnings
    over = P.validate([video("A", 4, 0, 40.0)])                           # a moment well above ~30 s is flagged, not rejected
    assert over.ok and any("above the ~30s guide" in w for w in over.warnings)
    short = P.validate([video("A", 4, 0, 4.0)])                           # <5 s needs to be completely understandable
    assert short.ok and any("under 5s" in w for w in short.warnings)
    v = video("A", 4, 0, 4.0)
    for m in v["moments"]:
        m["complete_when_short"] = True
    assert not P.validate([v]).warnings
    huge = video("A", 4, 0, 160.0)                                        # only TikTok's own 600 s upload limit is an error
    assert any("upload limit" in e for e in P.validate([huge]).errors)


def test_duplicate_moments_titles_and_categories_prevented():
    a = video("A", 4, 0, 12.0)
    overlap = video("B", 4, 5, 12.0, names=["Chat Roasts Other", "Mic Drops Again", "Jump Scare Two", "Big Slip Up"])
    assert any("already used" in e for e in P.validate([a, overlap]).errors)
    same_titles = video("B", 4, 500, 12.0)
    assert any("repeated" in e for e in P.validate([a, same_titles]).errors)
    other = video("A", 4, 500, 12.0, names=["Chat Roasts Other", "Mic Drops Again", "Jump Scare Two", "Big Slip Up"])
    assert any("distinct" in e for e in P.validate([a, other]).errors)


def test_unverified_moment_rejected_and_no_fixed_video_cap():
    v = video("A", 4, 0, 12.0)
    v["moments"][0]["verified"] = False
    assert any("not verified" in e for e in P.validate([v]).errors)
    many = [video(f"C{i}", 4, i * 1000, 12.0, names=[f"Clip {i} {c}" for c in "abcd"]) for i in range(6)]
    assert not any("at most" in e for e in P.validate(many).errors)   # production goal: as many strong videos as the source supports; no arbitrary cap


# --- approval gates -------------------------------------------------------------
def _manifest():
    return M.build("a.mp4", [{"rank": r, "title": t} for r, t in ((4, "One Two"), (3, "Three Four"), (2, "Five Six"), (1, "Seven Eight"))],
                   "Best", "cap", "Cover", ["#a", "b", "c", "d"], "manual_review")


def test_manifest_starts_unapproved_and_not_public():
    m = _manifest()
    assert m["approved_by_user"] is False and m["public_publish"] is False and m["transfer"]["status"] == "not_started"
    assert m["ranks"][0]["label"] == "#4 One Two" and m["hashtags"][1] == "#b"
    with pytest.raises(ValueError):
        M.build("a.mp4", [{"rank": 1, "title": "Ab Cd"}], "x", "c", "t", ["#a"], "pass")


def test_transfer_gates():
    m = _manifest()
    with pytest.raises(M.ApprovalError, match="not been approved"):
        M.authorize_transfer(m, "inbox", True)
    ap = M.approve(m)
    with pytest.raises(M.ApprovalError, match="explicit user authorization"):
        M.authorize_transfer(ap, "inbox", False)
    with pytest.raises(M.ApprovalError, match="direct posting"):
        M.authorize_transfer(ap, "direct", True)
    assert M.authorize_transfer(ap, "draft", True)["transfer"] == {"status": "ready", "mode": "draft"}
    with pytest.raises(M.ApprovalError, match="never automatic"):
        M.authorize_transfer({**ap, "public_publish": True}, "inbox", True)


def test_manifest_write_refuses_overwrite(tmp_path):
    M.write(tmp_path, _manifest())
    with pytest.raises(RU.RunError):
        M.write(tmp_path, _manifest())


# --- usage logging --------------------------------------------------------------
def test_usage_log_and_summary(tmp_path):
    run = tmp_path / "runs" / "r1"
    run.mkdir(parents=True)
    out = run / "o.mp4"; out.write_bytes(b"x" * 10)
    log = U.UsageLog(run)
    with log.stage("render"):
        pass
    with pytest.raises(RuntimeError):
        with log.stage("qa"):
            raise RuntimeError("boom")
    log.ffmpeg(); log.ffmpeg(retry=True); log.warn("short"); log.output(out, 48.0)
    log.d["higgsfield_credits"] = {"before": 365.5, "after": 365.5}
    log.save()
    d = json.loads((run / "usage.json").read_text())
    assert d["claude_usage"] is None                      # never fabricated
    assert d["ffmpeg"] == {"executions": 2, "retries": 1} and d["completed_videos"] == 1
    assert "render" in d["stages_sec"] and d["errors"] == ["qa: boom"] and d["tiktok_transfer"] == "not_started"
    s = U.summarize(U.load_all(tmp_path / "runs"))
    assert s["runs"] == 1 and s["credits_spent"] == 0 and s["claude_usage"] == "not available"


# --- QA parsing ------------------------------------------------------------------
def test_qa_parsers_and_manual_flags():
    assert Q.parse_max_volume("max_volume: -3.2 dB") == -3.2
    assert Q.parse_intervals("black_start:1.5 black_end:2 black_duration:0.5", "black_start", "black_duration") == [(1.5, 0.5)]
    assert all(c["status"] == "pass" for c in Q.layout_checks())
    flags = {c["check"]: c["status"] for c in Q.manual_checks([{"rank": 1, "title": "Ab Cd"}])}
    assert flags["faces_and_action_visible"] == Q.MANUAL                      # never claimed as passed
