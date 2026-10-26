import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from clipagent import captions as C
from clipagent import fullanalysis as FA
from clipagent import render as R
from test_fullanalysis import SID, lib, video  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
A = [["Wait,", 10.0, 10.3], ["what", 10.4, 10.6], ["is", 10.7, 10.8], ["happening", 10.9, 11.4], ["here?", 11.5, 11.9], ["Maybe", 12.5, 12.9], ["Marlin", 13.0, 13.5]]
B = [["Wait,", 10.0, 10.3], ["what", 10.4, 10.6], ["is", 10.7, 10.8], ["happening", 10.9, 11.4], ["here?", 11.5, 11.9], ["Maybe", 12.5, 12.9], ["Marlon", 13.0, 13.5]]


def spec(lines, **extra):
    words = [[t, a, b] for t, a, b in lines]
    return {"title": "t", **extra, "moments": {"1": {"start": 10.0, "end": 14.0, "decodes": [A, B], "words": words,
                                                        "lines": [{"text": t, "first": i, "last": i} for i, (t, _, _) in enumerate(lines)]}}}


# ---- two-decode caption agreement ---------------------------------------------------------------------------
def test_agreed_words_exclude_what_the_decodes_disagree_on():
    ws = [w["w"] for w in C.agreed_words(A, B)]
    assert ws == ["wait", "what", "is", "happening", "here", "maybe"] and "marlin" not in ws and "marlon" not in ws


def test_confirmed_wording_passes_and_unconfirmed_wording_is_refused():
    ok = spec([("Wait, what is happening here?", 10.0, 11.9), ("Maybe", 12.5, 12.9)])
    assert C.problems(ok) == []
    bad = spec([("Wait, what is happening here?", 10.0, 11.9), ("Maybe Marlin", 12.5, 13.5)])
    p = C.problems(bad)
    assert len(p) == 1 and "'marlin'" in p[0] and "omit it" in p[0]
    with pytest.raises(C.CaptionError):
        C.assert_agreed(bad)


def test_words_must_be_confirmed_near_their_own_time_and_each_only_once():
    stray = spec([("Maybe wait", 12.5, 12.9)])                    # 'wait' was heard at 10 s, not near this line
    assert any("'wait'" in x for x in C.problems(stray))
    twice = spec([("what what", 10.4, 10.6)])
    assert any("'what'" in x for x in C.problems(twice))


def test_missing_evidence_is_refused_unless_the_wording_comes_from_a_supplied_transcript():
    s = spec([("Maybe", 12.5, 12.9)])
    del s["moments"]["1"]["decodes"]
    assert any("no two-decode evidence" in x for x in C.problems(s))
    one = spec([("Maybe", 12.5, 12.9)])
    one["moments"]["1"]["decodes"] = [A, []]
    assert C.problems(one)
    assert C.problems({**s, "wording_verified_by": "supplied_transcript"}) == []


def test_suggested_lines_only_contain_agreed_words():
    lines = C.suggest_lines(A, B)
    text = " ".join(t for t, _, _ in lines).lower()
    assert "marlon" not in text and "marlin" not in text and "happening" in text
    assert C.problems(spec([(t, a, b) for t, a, b in lines])) == []


def test_renders_refuse_unconfirmed_captions(tmp_path, monkeypatch):
    s = spec([("Maybe Marlin", 12.5, 13.5)])
    p = tmp_path / "s.json"
    p.write_text(json.dumps(s))
    with pytest.raises(R.RenderError, match="not confirmed by both decodes"):
        R.render_standalone("abcdef123456", p, tmp_path / "o.mp4")


# ---- camera-angle change vs unrelated scene change -------------------------------------------------------------
@pytest.fixture(scope="module")
def continuity_video(tmp_path_factory):
    """0-6 s scene A, 6-12 s scene B (camera angle change: SAME continuing tone), 12-18 s scene C (unrelated: different ambience and level)."""
    p = tmp_path_factory.mktemp("cont") / "m.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=6", "-f", "lavfi", "-i", "smptebars=s=640x360:r=30:d=6",
                    "-f", "lavfi", "-i", "mandelbrot=s=640x360:r=30", "-f", "lavfi", "-i",
                    "aevalsrc='0.3*sin(2*PI*440*t)+0.1*sin(2*PI*880*t)':d=12", "-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:a=0.02",
                    "-filter_complex", "[2:v]trim=duration=6,setpts=PTS-STARTPTS[m];[0:v][1:v][m]concat=n=3:v=1:a=0[v];[3:a][4:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


def test_camera_angle_change_is_allowed_but_an_unrelated_scene_is_flagged(continuity_video):
    cuts = FA.scene_cuts_full(continuity_video, 0.3, 0.6)
    assert [round(c["t"]) for c in cuts] == [6, 12]
    res = FA.cut_continuity(continuity_video, cuts, [])
    kinds = {round(c["t"]): c["continuity"]["kind"] for c in res}
    assert kinds == {6: "continuing", 12: "unrelated"}, res
    assert all("why" in c["continuity"] for c in res)


def test_a_line_of_speech_across_a_cut_marks_it_continuing(continuity_video):
    cuts = FA.scene_cuts_full(continuity_video, 0.3, 0.6)
    res = FA.cut_continuity(continuity_video, cuts, [{"start": 11.0, "end": 13.5, "text": "and he kept talking"}])
    assert {round(c["t"]): c["continuity"]["kind"] for c in res}[12] == "continuing"


def test_plan_gate_blocks_only_unrelated_cuts(lib):                      # noqa: F811
    FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    d = lib / "source_library" / SID
    a = json.loads((d / "analysis" / "signals.json").read_text())
    for c in a["scenes"]["cuts"]:
        c["continuity"]["kind"] = "continuing" if round(c["t"]) == 5 else "unrelated"          # cut at 5 s = angle change, cut at 9 s = unrelated
    (d / "analysis" / "signals.json").write_text(json.dumps(a))
    assert FA.unrelated_cuts_in(SID, 1.0, 8.0, lib) == []                                         # only the allowed angle change inside
    bad = FA.unrelated_cuts_in(SID, 6.0, 14.0, lib)
    assert [round(c["t"]) for c in bad] == [9]
    q = json.loads((d / "analysis" / "candidates.json").read_text())["candidates"]
    assert all("unrelated_internal" in c["scene_cuts"] for c in q)


def test_older_analyses_gain_continuity_without_redoing_the_analysis(lib):                       # noqa: F811
    FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    d = lib / "source_library" / SID
    a = json.loads((d / "analysis" / "signals.json").read_text())
    for c in a["scenes"]["cuts"]:
        c.pop("continuity")
    (d / "analysis" / "signals.json").write_text(json.dumps(a))
    frames_before = sorted(p.name for p in (d / "analysis" / "thumbnails" / "_sampled").glob("*.jpg"))
    again = FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    assert again["reused"] and all("continuity" in c for c in again["analysis"]["scenes"]["cuts"])
    assert sorted(p.name for p in (d / "analysis" / "thumbnails" / "_sampled").glob("*.jpg")) == frames_before
