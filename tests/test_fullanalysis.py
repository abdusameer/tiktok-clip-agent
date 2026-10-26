import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import args as A
from clipagent import fullanalysis as FA
from clipagent import source_library as SL

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
SID = "abcdef123456"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


@pytest.fixture(scope="session")
def video(tmp_path_factory):
    d = tmp_path_factory.mktemp("fa")
    p = d / "master.mp4"
    cap = d / "cap.png"          # this ffmpeg build has no drawtext: burn the caption in from a Pillow-rendered PNG
    subprocess.run(["/usr/bin/python3", "-c", (
        "from PIL import Image, ImageDraw, ImageFont\n"
        f"f = ImageFont.truetype('{FONT}', 44)\n"
        "im = Image.new('RGBA', (1280, 110), (0, 0, 0, 200)); d = ImageDraw.Draw(im)\n"
        "d.text((40, 28), 'THIS IS A BURNED IN CAPTION', font=f, fill=(255, 255, 255, 255))\n"
        f"im.save('{cap}')")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=5", "-f", "lavfi", "-i", "smptebars=s=1280x720:r=30:d=4",
                    "-f", "lavfi", "-i", "mandelbrot=s=1280x720:r=30", "-f", "lavfi", "-i", "sine=f=440:d=15,volume='if(between(t,6.0,7.6),1.0,0.03)':eval=frame",
                    "-i", str(cap),
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[a];[2:v]trim=duration=6,setpts=PTS-STARTPTS[m];[a][m]concat=n=2:v=1:a=0[c];"
                    "[c][4:v]overlay=0:610[v]",
                    "-map", "[v]", "-map", "3:a", "-t", "15", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", str(p)], check=True)
    return p


@pytest.fixture
def lib(tmp_path, video):
    root = tmp_path / "proj"
    d = root / "source_library" / SID
    for sub in ("transcripts", "analysis"):
        (d / sub).mkdir(parents=True)
    shutil.copy(video, d / "master.mp4")
    sha = hashlib.sha256((d / "master.mp4").read_bytes()).hexdigest()
    (d / "source.json").write_text(json.dumps({"id": SID, "status": "active", "sha256": sha, "master": {"duration_sec": 15.0, "size_bytes": 1}}))
    segs = [{"start": 0.5, "end": 3.2, "text": "Wait what is happening here?"}, {"start": 5.2, "end": 6.0, "text": "Oh my god."},
            {"start": 13.5, "end": 14.8, "text": "And then he just left."}]
    (d / "transcripts" / f"{SID}.json").write_text(json.dumps({"segments": segs}))
    cands = [{"start": 0.5, "end": 3.2, "text": "Wait what is happening here?", "score": 0.9, "verified": True, "policy_risk": "low"},
             {"start": 13.5, "end": 14.8, "text": "And then he just left.", "score": 0.5, "verified": False, "policy_risk": "low"}]
    (d / "analysis" / f"{SID}.candidates.json").write_text(json.dumps({"candidates": cands}))
    return root


# ---- dependencies are reported, never installed --------------------------------------------------
def test_missing_dependency_is_reported_not_installed(monkeypatch):
    calls = []
    monkeypatch.setattr(FA, "_find", lambda name: None)
    monkeypatch.setattr(FA.subprocess, "run", lambda *a, **k: calls.append(a[0]) or subprocess.CompletedProcess(a[0], 1, b"", b""))
    with pytest.raises(FA.AnalysisError, match="NOT installed automatically"):
        FA.require("yt-dlp", "ffmpeg")
    assert not any(any(x in str(c) for x in ("pip", "brew", "install")) for c in calls)


def test_only_youtube_urls_and_downloads_need_authorization(monkeypatch):
    assert FA.check_url("https://youtu.be/caR2N2OvHJ8?si=abc")
    assert FA.check_url("https://www.youtube.com/watch?v=caR2N2OvHJ8")
    for bad in ("https://example.com/watch?v=1", "file:///etc/passwd", "https://youtube.com.evil.com/watch?v=1"):
        with pytest.raises(FA.AnalysisError):
            FA.check_url(bad)
    with pytest.raises(FA.AnalysisError, match="authorization note"):
        FA.youtube_download("https://youtu.be/caR2N2OvHJ8", "  ")


def test_youtube_info_records_id_title_duration_chapters_and_captions(monkeypatch):
    j = {"id": "abc", "title": "T", "duration": 99, "webpage_url": "https://www.youtube.com/watch?v=abc", "uploader": "U", "upload_date": "20260101",
         "chapters": [{"title": "Intro", "start_time": 0, "end_time": 10}], "subtitles": {"en": []}, "automatic_captions": {"en": [], "fr": []}}
    monkeypatch.setattr(FA, "_find", lambda n: f"/x/{n}")
    monkeypatch.setattr(FA.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, json.dumps(j), ""))
    i = FA.youtube_info("https://youtu.be/abc")
    assert (i["video_id"], i["title"], i["duration_sec"]) == ("abc", "T", 99)
    assert i["chapters"][0]["title"] == "Intro" and i["captions"] == {"manual": ["en"], "automatic": ["en"]}


def test_vtt_parser_dedupes_rolling_captions():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello there\n\n00:00:02.000 --> 00:00:03.500\nhello there\n\n00:00:03.500 --> 00:00:05.000\n<c>new line</c>\n"
    s = FA.parse_vtt(vtt)
    assert [x["text"] for x in s] == ["hello there", "new line"] and s[1]["start"] == 3.5


# ---- /make-rankings arguments -------------------------------------------------------------------------
def test_make_rankings_accepts_url_video_or_source_id_exclusively():
    a = A.parse('--url https://youtu.be/x --rights "mine"')
    assert a.url and a.rights == "mine" and A.validate(a).url
    with pytest.raises(A.ArgError, match="--rights"):
        A.validate(A.parse("--url https://youtu.be/x"))
    assert A.parse("--source-id abcdef123456").source_id == "abcdef123456"
    with pytest.raises(A.ArgError):
        A.parse("--url https://youtu.be/x --source-id abcdef123456")
    with pytest.raises(A.ArgError):
        A.parse("")


# ---- full analysis on a synthetic video -------------------------------------------------------------
def test_full_analysis_outputs_and_detections(lib):
    res = FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    d = lib / "source_library" / SID
    for n in ("analysis/signals.json", "transcript/transcript.json", "transcript/transcript.srt", "analysis/candidates.json"):
        assert (d / n).is_file(), n
    assert list((d / "analysis" / "thumbnails" / "_sampled").glob("f_*.jpg")) and list((d / "analysis" / "contact-sheets").glob("sheet_*.jpg"))
    a = res["analysis"]
    assert a["probe"]["duration_sec"] > 14 and a["probe"]["video"]["width"] == 1280
    assert len(a["scenes"]["cuts"]) >= 2                                              # testsrc2 -> bars -> mandelbrot
    assert a["transcript"]["source"] == "local_whisper_small.en"
    assert all(k in a["signals"] for k in ("audio_db", "motion", "speech_wps", "peaks"))
    if a["ocr"]["available"]:                                                         # the burned-in caption is found on screen
        assert a["ocr"]["burned_in_captions"]["present"] is True
        assert any("CAPTION" in h["text"].upper() for h in a["ocr"]["hits"])
    c = json.loads((d / "analysis" / "candidates.json").read_text())
    assert c["evidence_required"] and c["counts"]["spoken"] == 2
    for cand in c["candidates"]:
        assert set(cand["frames"]) == set(FA.PHASES) and all((d / v["file"]).is_file() for v in cand["frames"].values())
        assert cand["review"]["status"] == "needs_visual_review"
        assert {"quality", "internal_count"} <= set(cand["scene_cuts"]) and "needs_outside_context" in cand["context"]
        assert cand["bounds"]["start"] < cand["bounds"]["end"] and cand["transcript_excerpt"]
    kinds = {x["kind"] for x in c["candidates"]}
    assert "spoken" in kinds and a["detections"]["scene_changes"] >= 2
    assert a["detections"]["reaction_moments"] >= 1                                   # the loud burst right after "Oh my god."
    assert a["detections"]["visual_moments_not_in_dialogue"] >= 1                     # moving mandelbrot scene with no dialogue


def test_analysis_is_reused_not_redone(lib):
    FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    d = lib / "source_library" / SID
    before = {p.name: p.stat().st_mtime_ns for p in (d / "analysis" / "signals.json", d / "analysis" / "candidates.json")}
    again = FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    assert again["reused"] is True and before == {p.name: p.stat().st_mtime_ns for p in (d / "analysis" / "signals.json", d / "analysis" / "candidates.json")}
    assert FA.status(SID, lib)["analysis/candidates.json"] is True


def test_a_moment_cannot_be_planned_from_transcript_alone(lib):
    FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    c0 = FA.load_candidates(SID, lib)["candidates"][0]
    ok, why = FA.evidence_for(SID, c0["start"], c0["end"], lib)
    assert not ok and "not visually reviewed" in why
    assert not FA.evidence_for(SID, 500.0, 510.0, lib)[0]                             # outside every analyzed candidate
    frames = [v["file"] for v in c0["frames"].values()]
    good = {"id": c0["id"], "suggested_format": "standalone", "visible_action": "two scenes of test pattern", "works_without_context": True,
            "hook": "question", "setup": "pattern", "payoff": "answer", "audio_confidence": "high", "visual_confidence": "medium", "viewed_frames": frames}
    with pytest.raises(FA.AnalysisError, match="at least 3"):
        FA.apply_reviews(SID, [{**good, "viewed_frames": frames[:1]}], lib)
    with pytest.raises(FA.AnalysisError, match="uncertain"):
        FA.apply_reviews(SID, [{**good, "works_without_context": "uncertain"}], lib)
    with pytest.raises(FA.AnalysisError, match="rejection_reason"):
        FA.apply_reviews(SID, [{**good, "suggested_format": "rejected"}], lib)
    FA.apply_reviews(SID, [good], lib)
    assert FA.evidence_for(SID, c0["start"], c0["end"], lib) == (True, "")
    c1 = FA.load_candidates(SID, lib)["candidates"][1]
    FA.apply_reviews(SID, [{"id": c1["id"], "suggested_format": "rejected", "rejection_reason": "needs too much outside context",
                            "works_without_context": False, "audio_confidence": "low", "visual_confidence": "low",
                            "viewed_frames": [v["file"] for v in c1["frames"].values()]}], lib)
    ok, why = FA.evidence_for(SID, c1["start"], c1["end"], lib)
    assert not ok and "rejected" in why


def test_sample_report_shows_timestamps_transcript_and_frames(lib):
    FA.analyze_source(SID, root=lib, progress=lambda *_: None)
    text = FA.sample_report(SID, 3, seed=1, root=lib)
    assert "transcript:" in text and "frame main_action:" in text and "scene cuts:" in text
    for line in text.splitlines():
        if "frame " in line and ": /" in line:
            assert Path(line.split(": ", 1)[1]).is_file()


def test_legacy_sources_without_analysis_are_not_blocked(lib):
    assert FA.evidence_for(SID, 1.0, 5.0, lib)[0] is True                              # no candidates.json yet
