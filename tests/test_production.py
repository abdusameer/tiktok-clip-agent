import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import cleanup, plan as P, production as PR, qa as Q, registry, render as R

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
RID = "20261012-120000-abcdef12"
SID = "abcdef123456"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_mp4(path, w, h, dur, fps=30):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={dur}", "-f", "lavfi",
                    "-i", f"sine=f=440:d={dur},volume=0.4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", str(path)], check=True)


@pytest.fixture(scope="session")
def good(tmp_path_factory):
    p = tmp_path_factory.mktemp("m") / "good.mp4"
    make_mp4(p, 1080, 1920, 16)
    return p


@pytest.fixture(scope="session")
def master(tmp_path_factory):
    p = tmp_path_factory.mktemp("m") / "master.mp4"
    make_mp4(p, 320, 180, 3)
    return p


@pytest.fixture
def proj(tmp_path, good, master):
    root = tmp_path / "proj"
    run = root / "runs" / RID
    for d in ("source", "transcripts", "candidates", "renders", "qa", "upload-manifests", "tmp"):
        (run / d).mkdir(parents=True)
    (run / "source" / "originals.json").write_text("{}")
    (run / "usage.json").write_text("{}")
    (run / "candidates" / "analyze.log").write_text("log")
    (run / "candidates" / "cand.json").write_text("{}")
    (run / "candidates" / "clip.mp4").write_bytes(b"c" * 200)
    (run / "tmp" / "frame.png").write_bytes(b"p" * 20)
    shutil.copy(good, run / "renders" / "a.mp4")
    lib = root / "source_library" / SID
    (lib / "transcripts").mkdir(parents=True)
    (lib / "analysis").mkdir()
    shutil.copy(master, lib / "master.mp4")
    (lib / "transcripts" / f"{SID}.json").write_text("{}")
    (root / "source_videos").mkdir()
    (root / "source_videos" / "in_video.mp4").write_bytes(b"v" * 300)
    (root / "source_videos" / "in_audio.mp4").write_bytes(b"a" * 100)
    info = {"id": SID, "status": "active", "sha256": sha(lib / "master.mp4"),
            "master": {"duration_sec": 3.0, "size_bytes": (lib / "master.mp4").stat().st_size, "width": 1280, "height": 720},
            "inputs": [{"filename": "in_video.mp4", "sha256": sha(root / "source_videos" / "in_video.mp4")},
                       {"filename": "in_audio.mp4", "sha256": sha(root / "source_videos" / "in_audio.mp4")}]}
    (lib / "source.json").write_text(json.dumps(info))
    (lib / "analysis" / f"{SID}.candidates.json").write_text(json.dumps({"candidates": [
        {"start": 100.0, "end": 112.0, "score": 0.8, "text": "left over", "policy_risk": "low"},
        {"start": 10.0, "end": 26.0, "score": 0.9, "text": "used one", "policy_risk": "low"}]}))
    (root / "trash").mkdir()
    return root


def meta(fmt="standalone", start=10.0, title="Cat Falls Off Desk"):
    ms = [{"rank": 1, "title": title, "start": start, "end": start + 16.0, "text_sha256": "t" + str(start), "sketch": [1, 2]}]
    if fmt == "ranking":
        ms = [{"rank": r, "title": t, "start": 200.0 + r * 30, "end": 216.0 + r * 30, "text_sha256": f"r{r}", "sketch": [r]}
              for r, t in ((4, "One Two"), (3, "Three Four"), (2, "Five Six"), (1, "Seven Eight"))]
    return {"source_id": SID, "video_title": "Test Video", "cover_title": "Cover", "caption": "A caption #a #b #c #d",
            "hashtags": ["#a", "#b", "#c", "#d"], "moments": ms}


# ---- plan -------------------------------------------------------------------
def standalone(title="Cat Falls Off Desk", start=0.0, dur=20.0):
    return {"format": "standalone", "moments": [{"rank": 1, "title": title, "start": start, "end": start + dur, "verified": True}]}


def test_plan_accepts_standalone_and_has_no_video_cap():
    vids = [standalone(f"Clip {c} Moment", i * 100.0) for i, c in enumerate(["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"])]
    assert P.validate(vids).ok


def test_plan_rejects_thin_or_malformed_standalone():
    assert P.validate([standalone(dur=5.0)]).ok and P.validate([standalone(dur=90.0)]).ok     # one complete scene: short or long
    assert any("under 5s" in w for w in P.validate([standalone(dur=3.0)]).warnings)           # <5 s only if completely understandable
    two = standalone()
    two["moments"].append({"rank": 2, "title": "Other Moment Here", "start": 50, "end": 70, "verified": True})
    assert any("exactly one" in e for e in P.validate([two]).errors)
    assert any("unknown format" in e for e in P.validate([{"format": "carousel", "moments": []}]).errors)
    bad = standalone()
    bad["moments"][0]["verified"] = False
    assert any("not verified" in e for e in P.validate([bad]).errors)
    assert any("overlap" in e or "already used" in e for e in P.validate([standalone(), standalone("Other Clip Title", 5.0)]).errors)


# ---- qa gate ----------------------------------------------------------------
def test_auto_decision_only_tolerates_design_manual_items():
    ok = {"checks": [{"check": "resolution_1080x1920", "status": "pass", "detail": ""}, {"check": "faces_and_action_visible", "status": "manual_review", "detail": ""}]}
    assert Q.auto_decision(ok) == (True, [])
    assert not Q.auto_decision({"checks": [{"check": "no_black_frames", "status": "fail", "detail": "x"}]})[0]
    assert not Q.auto_decision({"checks": [{"check": "something_unclear", "status": "manual_review", "detail": "?"}]})[0]


def test_standalone_qa_has_no_ranking_checks(good, tmp_path):
    (tmp_path / "q").mkdir()
    rep = Q.run_qa(good, tmp_path / "q", None, "standalone")
    names = {c["check"] for c in rep["checks"]}
    assert "label_clear_of_captions" not in names and "header_clear_of_top_ui" not in names and "hook_and_payoff_clear" in names
    assert rep["verdict"] != "fail", rep
    assert Q.auto_decision(rep)[0]


# ---- render (standalone graphics) ------------------------------------------------
def test_standalone_command_has_no_ranking_graphics(tmp_path):
    spec = {"moments": {"1": {"start": 1.0, "end": 9.0, "words": [["hi", 1.1, 1.8]], "lines": [{"text": "hi", "first": 0, "last": 0}]}}}
    cmd = " ".join(R.standalone_clip_command(spec, tmp_path, tmp_path / "m.mp4", tmp_path / "o.mp4"))
    assert "header" not in cmd and "badge" not in cmd and "hook" not in cmd and "label" not in cmd
    assert "crop=iw:2*trunc(ih*0.875" in cmd and "gblur" in cmd and f"overlay=0:{R.CAP_Y}" in cmd


# ---- finalize / registry / cleanup -------------------------------------------------
def test_pass_goes_to_approved_registered_and_not_marked_reviewed(proj):
    rec = PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    assert rec["status"] == "approved", rec
    final = proj / rec["final"]
    assert final.parent == proj / "approved" / RID and final.is_file()
    assert not (proj / "runs" / RID / "renders" / "a.mp4").exists()
    m = json.loads(final.with_suffix(".manifest.json").read_text())
    assert m["approval"] == "auto_qa_approved" and m["manually_reviewed"] is False and m["approved_by_user"] is False
    assert m["public_publish"] is False and m["caption"] and m["cover_title"] and len(m["hashtags"]) == 4 and m["format"] == "standalone"
    assert (final.parent / m["qa_report"]).is_file()
    reg = registry.load(proj)
    c = [x for x in reg["clips"] if x["run_id"] == RID]
    assert len(c) == 1 and c[0]["approval"] == "auto_qa_approved" and c[0]["start"] == 10.0


def test_over_a_minute_passes_but_a_technical_failure_goes_to_needs_review(proj, tmp_path):
    long_ = tmp_path / "long.mp4"
    make_mp4(long_, 1080, 1920, 75)                       # 75 s: no longer an automatic failure
    shutil.copy(long_, proj / "runs" / RID / "renders" / "long.mp4")
    from clipagent import colors
    (proj / "runs" / RID / "renders" / "long.mp4.render.json").write_text(json.dumps({"label_colors": colors.assign("s", ["4", "3", "2", "1"])}))
    ok = PR.finalize_video(RID, "renders/long.mp4", "ranking", meta("ranking"), proj)
    assert ok["status"] == "approved", ok
    m = json.loads((proj / ok["final"]).with_suffix(".manifest.json").read_text())
    assert m["duration_sec"] > 60 and all("duration_sec" in x for x in m["moments"])
    assert m["status"] == "ready_for_draft_transfer" and m["transfer"]["status"] == "ready_for_draft_transfer" and m["transfer"]["authorized"] is False
    assert set(m["text_colors"]) == {"4", "3", "2", "1"} and m["category"]
    bad = tmp_path / "wrong.mp4"
    make_mp4(bad, 640, 360, 20)                           # wrong resolution = real failure
    shutil.copy(bad, proj / "runs" / RID / "renders" / "a.mp4")
    rec = PR.finalize_video(RID, "renders/a.mp4", "ranking", meta("ranking", ), proj)
    assert rec["status"] == "needs_review" and (proj / "needs-review" / RID / "a.mp4").is_file()
    assert any("resolution" in r for r in rec["needs_review_reasons"])
    assert len(registry.load(proj)["clips"]) == 4         # only the approved long video is registered


def test_one_failure_is_recorded_and_the_run_continues(proj, good):
    bad = PR.finalize_video(RID, "renders/missing.mp4", "standalone", meta(), proj)
    assert bad["status"] == "render_failed" and "missing" in bad["error"]
    shutil.copy(good, proj / "runs" / RID / "renders" / "b.mp4")
    ok = PR.finalize_video(RID, "renders/b.mp4", "standalone", meta(title="Second Fine Clip", start=50.0), proj)
    assert ok["status"] == "approved"
    log = json.loads((proj / "runs" / RID / "production.json").read_text())
    assert len(log["videos"]) == 2 and len(log["errors"]) == 1


def test_cleanup_moves_only_run_temp_files_and_protects_everything_else(proj):
    PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    registry.reserve(RID, json.loads((proj / "source_library" / SID / "source.json").read_text())["sha256"], [(900.0, 910.0)], proj)
    before = {p: sha(p) for p in [proj / "source_library" / SID / "master.mp4", proj / "registry" / "used_clips.json",
                                   proj / "source_library" / SID / "transcripts" / f"{SID}.json", *(proj / "approved" / RID).iterdir()]}
    res = PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash")
    assert res["result"] == "completed" and res["bytes"] > 0
    moved = {m["rel"] for m in res["moved"]}
    assert {"candidates/analyze.log", "candidates/clip.mp4", "tmp/frame.png"} <= moved
    assert not any(r.endswith(".qa.json") or r == "candidates/cand.json" for r in moved)
    assert {p: sha(p) for p in before} == before                       # finals, master, registry, transcript untouched
    run = proj / "runs" / RID
    assert (run / "usage.json").exists() and (run / "production.json").exists() and (run / "source" / "originals.json").exists()
    assert (run / "candidates" / "cand.json").exists() and list((run / "qa").glob("*.qa.json"))
    assert (proj / "source_videos" / "in_video.mp4").exists()          # input copies are NOT touched unless asked
    assert len(list((proj / "trash").iterdir())) == len(moved)          # moved, not deleted
    assert all(r["status"] != "active" for r in registry.load_reservations(proj)["reservations"])
    assert PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash")["moved"] == []   # idempotent


def test_input_copies_move_only_after_master_verified(proj):
    PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    lib = proj / "source_library" / SID
    (lib / "master.mp4").write_bytes((lib / "master.mp4").read_bytes()[:-50])         # corrupt master -> inputs must stay
    res = PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash", source_id=SID, include_inputs=True)
    assert (proj / "source_videos" / "in_video.mp4").exists() and any("not verified" in p for p in res["problems"])


def test_input_copies_move_when_master_verified(proj):
    PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    res = PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash", source_id=SID, include_inputs=True)
    assert res["result"] == "completed", res
    assert not (proj / "source_videos" / "in_video.mp4").exists() and not (proj / "source_videos" / "in_audio.mp4").exists()
    assert (proj / "source_library" / SID / "master.mp4").exists()


def test_cleanup_failure_leaves_files_and_records_it(proj):
    PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    res = PR.auto_cleanup(RID, root=proj, trash_dir=proj / "no-such-trash")
    assert res["result"] == "failed" and (proj / "runs" / RID / "tmp" / "frame.png").exists()
    assert json.loads((proj / "runs" / RID / "production.json").read_text())["cleanup"]["result"] == "failed"


def test_cleanup_skipped_when_a_video_was_not_stored_safely(proj):
    run = proj / "runs" / RID
    (run / "production.json").write_text(json.dumps({"run_id": RID, "errors": [], "cleanup": None,
                                                     "videos": [{"render": "renders/x.mp4", "status": "store_failed", "format": "standalone"}]}))
    res = PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash")
    assert res["result"] == "skipped" and (run / "tmp" / "frame.png").exists()


def test_report_lists_everything_needed(proj):
    PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash")
    r = PR.report(RID, proj)
    text = PR.format_report(r)
    assert r["source_id"] == SID and len(r["approved"]) == 1
    assert [c["clip_id"] for c in r["remaining"]] == [f"{SID}:100.00-112.00"]       # 10-26 s is used; 100-112 s is free
    for needle in ("Videos created: 1", "standalone", "approved/" + RID, f"{SID}:10.00-26.00", "Remaining unused candidates: 1",
                   "moved to Trash", f"source_library/{SID}/master.mp4", "auto_qa_approved"):
        assert needle in text, needle


def test_input_copy_in_library_root_is_found_but_not_the_master(proj):
    f = proj / "source_library" / "dl copy.mp4"
    f.write_bytes((proj / "source_library" / SID / "master.mp4").read_bytes())
    info = json.loads((proj / "source_library" / SID / "source.json").read_text())
    info["inputs"] = [{"filename": "dl copy.mp4", "sha256": sha(f)}]
    (proj / "source_library" / SID / "source.json").write_text(json.dumps(info))
    assert PR._input_copies(SID, proj) == [f]
    PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash", source_id=SID, include_inputs=True)
    assert not f.exists() and (proj / "source_library" / SID / "master.mp4").exists()
