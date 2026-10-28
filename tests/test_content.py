import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import contentflow as CF
from clipagent import cleanup, fullanalysis as FA, layout, qa, registry, render as R, segments as SG, source_library as SL
from clipagent.status import STAGES, RunStatus

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
SID = "abcdef123456"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
# (start, end, kind): A = one event with a CAMERA-ANGLE change at 8 s; B..F are separate clips (real boundaries at 12, 22, 32, 40, 48)
SCENES = {"A": (0, 12), "B": (12, 22), "C": (22, 32), "D": (32, 40), "E": (40, 48), "F": (48, 70)}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    p = tmp_path_factory.mktemp("content") / "master.mp4"
    v = ["testsrc2=s=1280x720:r=30:d=8", "testsrc2=s=1280x720:r=30:d=4,hflip", "mandelbrot=s=1280x720:r=30", "testsrc2=s=1280x720:r=30:d=10,hue=h=120",
         "gradients=s=1280x720:r=30:d=8:speed=0.05", "testsrc2=s=1280x720:r=30:d=8,hue=h=300", "mandelbrot=s=1280x720:r=30,hue=h=60"]
    a = ["sine=f=440:d=12", "sine=f=1000:d=10,volume=0.2", "sine=f=220:d=10", "anoisesrc=d=8:c=pink:a=0.1", "sine=f=800:d=8,volume=0.6", "anoisesrc=d=22:c=brown:a=0.05"]
    inputs = []
    for x in v:
        inputs += ["-f", "lavfi", "-i", x]
    for x in a:
        inputs += ["-f", "lavfi", "-i", x]
    fc = ("[0:v][1:v]concat=n=2:v=1:a=0[va];[2:v]trim=duration=10,setpts=PTS-STARTPTS[vb];[3:v]setpts=PTS-STARTPTS[vc];[4:v][5:v]concat=n=2:v=1:a=0[vde];"
          "[6:v]trim=duration=22,setpts=PTS-STARTPTS[vf];[va][vb][vc][vde][vf]concat=n=5:v=1:a=0[v];"
          "[7:a][8:a][9:a][10:a][11:a][12:a]concat=n=6:v=0:a=1[a]")
    # video: A(8+4) B(10) C(10) D+E(8+8->16 from gradients/life) F(22)
    subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", "70", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(p)], check=True)
    return p


@pytest.fixture
def proj(tmp_path, media, monkeypatch):
    root = tmp_path / "proj"
    d = root / "source_library" / SID
    for sub in ("transcripts", "analysis"):
        (d / sub).mkdir(parents=True)
    shutil.copy(media, d / "master.mp4")
    sha = hashlib.sha256((d / "master.mp4").read_bytes()).hexdigest()
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(d / "master.mp4")], capture_output=True, text=True).stdout)
    (d / "source.json").write_text(json.dumps({"id": SID, "status": "active", "sha256": sha, "master": {"duration_sec": dur, "size_bytes": (d / "master.mp4").stat().st_size, "width": 1280, "height": 720},
                                               "inputs": [{"filename": "Test Compilation.mp4", "sha256": sha}]}))
    segs = [{"start": 1.0, "end": 3.5, "text": "Wait what is happening here?"}, {"start": 13.0, "end": 16.0, "text": "Oh my god he really did that!"},
            {"start": 23.0, "end": 26.0, "text": "No way, that is crazy!"}, {"start": 33.0, "end": 36.0, "text": "Hold on, listen to this."},
            {"start": 41.0, "end": 45.0, "text": "That was the funniest thing ever!"}, {"start": 50.0, "end": 66.0, "text": "So we kept going and then it all got way out of hand somehow."}]
    (d / "transcripts" / f"{SID}.json").write_text(json.dumps({"segments": segs}))
    (d / "analysis" / f"{SID}.candidates.json").write_text(json.dumps({"candidates": []}))
    monkeypatch.setenv("CLIPAGENT_ROOT", str(root))
    monkeypatch.setattr(FA, "run_ocr", lambda *a, **k: {"available": False, "missing": "test", "hits": [], "burned_in_captions": {"present": "unknown", "fraction": None}})
    (root / "trash").mkdir()
    return root


def analyzed(proj):
    FA.analyze_source(SID, root=proj, progress=lambda *_: None)
    return SG.build(SID, root=proj, progress=lambda *_: None)


# ---- 1-4: timeline, boundaries, camera angles, classification -------------------------------------------------
def test_complete_timeline_is_accounted_and_catalogued(proj):
    r = analyzed(proj)
    d = proj / "source_library" / SID / "analysis"
    tl = json.loads((d / "timeline.json").read_text())
    assert SG.verify_timeline(tl) == [] and abs(tl["accounted_sec"] - tl["duration_sec"]) < 0.1        # every second is in exactly one entry
    for f in ("timeline.json", "segments.csv", "candidates.json", "rejected.json"):
        assert (d / f).is_file(), f
    rows = list(csv.DictReader((d / "segments.csv").open()))
    assert set(SG.CSV_FIELDS) == set(rows[0]) and len(rows) == r["segments"]
    assert any((d / "thumbnails" / f"S001_{k}.jpg").is_file() for k in ("begin", "mid", "end")) and (d / "thumbnails" / "S001.jpg").is_file()   # 3-frame strip
    assert list((d / "contact-sheets").glob("segments_*.jpg"))
    assert not list((proj / "source_library" / SID).glob("*.mp4.copy")) and len(list((proj / "source_library" / SID).glob("*.mp4"))) == 1    # one master only


def test_real_clip_boundaries_are_found_and_camera_angle_changes_do_not_split(proj):
    r = analyzed(proj)
    tl = json.loads((proj / "source_library" / SID / "analysis" / "timeline.json").read_text())
    real = [round(b["t"]) for b in tl["boundaries"] if b["boundary"]]
    angle = [round(b["t"]) for b in tl["boundaries"] if not b["boundary"]]
    for t in (12, 22, 32, 40, 48):
        assert any(abs(x - t) <= 1 for x in real), (t, real)                   # real clip boundaries detected
    assert any(abs(x - 8) <= 1 for x in angle) and not any(abs(x - 8) <= 1 for x in real)      # the angle change at 8 s stays INSIDE one segment
    segs = [e for e in tl["timeline"] if e["kind"] == "segment"]
    assert any(e["start"] < 1 and e["end"] > 11 for e in segs)                 # segment A spans the angle change (0-12 s)
    assert r["boundaries_camera_angle"] >= 1 and r["boundaries_real"] >= 5


def test_every_candidate_gets_exactly_one_classification(proj):
    analyzed(proj)
    c = FA.load_candidates(SID, proj)["candidates"]
    assert c and all(x["suggested_format"] in ("ranking", "funny", "continuous", "rejected") for x in c)
    assert len({x["id"] for x in c}) == len(c)
    assert all(isinstance(x["humor_score"], float) and 0 <= x["context_score"] <= 1 and x["used_clip_match"] in ("none", "overlap", "near", "text/reserved") for x in c)


def test_too_few_segments_trigger_a_second_coverage_pass(proj):
    FA.analyze_source(SID, root=proj, progress=lambda *_: None)
    r = SG.build(SID, root=proj, progress=lambda *_: None)                    # 70 s source -> fewer than 8 usable segments -> second pass runs
    assert r["second_coverage_pass"] is True


# ---- 5-8: plan, formats, no badge, readable names ------------------------------------------------------------
def review(proj, spec):
    """spec: {segment_id: (format, title, category)}"""
    data = FA.load_candidates(SID, proj)["candidates"]
    out = []
    for c in data:
        fmt, title, cat = spec.get(c["id"], ("rejected", None, None))
        r = {"id": c["id"], "suggested_format": fmt, "viewed_frames": [(v["file"] if isinstance(v, dict) else v) for v in c["frames"].values()], "audio_confidence": "medium", "visual_confidence": "high",
             "works_without_context": True}
        if fmt == "rejected":
            r["rejection_reason"] = "test: not used"
        else:
            r.update(visible_action="something happens", hook="hook", setup="setup", payoff="payoff", title=title, category=cat,
                     treatment="standalone_punchline", duration_reason="complete joke", added_value="headline framing")
        out.append(r)
    FA.apply_reviews(SID, out, proj)


def plan_ready(proj):
    analyzed(proj)
    segs = FA.load_candidates(SID, proj)["candidates"]
    big = [c for c in segs if c["duration"] >= 5]
    assert len(big) >= 6, [(c["id"], c["start"], c["end"]) for c in segs]
    spec = {}
    names = ["Wait What Happened", "He Really Did It", "No Way Moment", "Listen to This"]
    for c, n in zip(big[:4], names):
        spec[c["id"]] = ("ranking", n, "Test Moments")
    spec[big[4]["id"]] = ("funny", "Quick Laugh Moment", None)
    spec[big[5]["id"]] = ("continuous", "Long Scene Plays Out", None)
    review(proj, spec)
    rid = CF.start(SID, proj).name
    return rid, spec


def test_every_segment_goes_to_one_format_and_rankings_are_numbered(proj):
    rid, spec = plan_ready(proj)
    pl = CF.build_plan(rid, proj)
    vids = pl["videos"]
    rk = [v for v in vids if v["format"] == "ranking"]
    assert len(rk) == 1 and sorted(m["rank"] for m in rk[0]["moments"]) == [1, 2, 3, 4], pl["rejected"]               # four moments, countdown to #1
    assert [m["rank"] for m in rk[0]["moments"]] == [4, 3, 2, 1]                                      # plays #4 first, #1 last
    assert len({m["title"] for m in rk[0]["moments"]}) == 4 and all(2 <= len(m["title"].split()) <= 5 for m in rk[0]["moments"])
    assert sum(1 for v in vids if v["format"] == "funny") == 1 and sum(1 for v in vids if v["format"] == "continuous") == 1
    segs = [m["segment"] for v in vids for m in v["moments"]]
    assert len(segs) == len(set(segs))                                                                # a segment appears in only one format/video
    rng = sorted((m["start"], m["end"]) for v in vids for m in v["moments"])
    assert all(b[0] >= a[1] for a, b in zip(rng, rng[1:]))                                            # no overlap between any moments
    for f in ("rankings.json", "funny-moments.json", "continuous-moments.json", "rejected.json"):
        assert (proj / "runs" / rid / "plan" / f).is_file()


def test_unusable_or_short_ranking_groups_never_become_rankings(proj):
    analyzed(proj)
    segs = [c for c in FA.load_candidates(SID, proj)["candidates"] if c["duration"] >= 5]
    spec = {c["id"]: ("ranking", n, "Tiny Group") for c, n in zip(segs[:2], ["One Two Three", "Four Five Six"])}
    review(proj, spec)
    rid = CF.start(SID, proj).name
    pl = CF.build_plan(rid, proj)
    assert not [v for v in pl["videos"] if v["format"] == "ranking"]                                  # 2 moments cannot make a ranking (min 3, never padded)
    assert pl["videos"] and all(v["format"] == "funny" for v in pl["videos"])                         # they become standalones instead


def test_funny_and_continuous_have_no_ranking_badge_and_ranking_does(tmp_path):
    spec = {"video_id": SID, "title": "x", "headline": "Quick Laugh Moment", "wording_verified_by": "supplied_transcript",
            "moments": {"1": {"start": 0.0, "end": 8.0, "words": [["hi", 0.5, 1.0]], "lines": [{"text": "hi", "first": 0, "last": 0}]}}}
    items = R.standalone_png_spec(spec, tmp_path, "Quick Laugh Moment", "#58D9FF")["items"]
    outs = " ".join(i["out"] for i in items)
    assert "label1.png" in outs and "badge" not in outs and "header" not in outs and "hook" not in outs
    cmd = " ".join(R.standalone_clip_command(spec, tmp_path, tmp_path / "m.mp4", tmp_path / "o.mp4", None, True))
    assert "label1.png" in cmd and "badge" not in cmd and "header" not in cmd
    rk = R.png_spec({"title": "T", "moments": {"1": spec["moments"]["1"]}}, ["1"], tmp_path, {"1": "Title"}, {"1": "#58D9FF"})["items"]
    assert any("badge1.png" in i["out"] for i in rk)                                                  # the ranking template still has its badge
    assert not any(c["check"] in ("header_clear_of_top_ui", "label_matches_moment") for c in qa.layout_checks("headline") + qa.manual_checks(None, "headline"))


# ---- 9-13: status, outputs, dashboard, needs-review, cleanup -----------------------------------------------------
def test_status_files_update_through_every_stage(tmp_path):
    d = tmp_path / "run1"
    d.mkdir()
    st = RunStatus(d, SID, "Test Compilation.mp4", 70.0)
    for stage in STAGES:
        st.stage(stage, segments_detected=5)
        data = json.loads((d / "status.json").read_text())
        assert data["stage"] == stage and f"**Current stage: {stage}**" in (d / "STATUS.md").read_text()
    st.warn("careful")
    st.error("boom")
    md = (d / "STATUS.md").read_text()
    for needle in ("Source ID", "Source duration", "Segments detected", "Segments analyzed", "Ranking candidates", "Funny-moment candidates", "Continuous-moment candidates",
                   "Rejected candidates", "Videos planned", "Videos rendering", "Videos in QA", "Videos passed", "Videos needing review", "Videos ready for drafts",
                   "Cleanup", "careful", "boom"):
        assert needle in md, needle
    with pytest.raises(ValueError):
        st.stage("NOT_A_STAGE")


@pytest.fixture
def produced(proj, monkeypatch):
    rid, spec = plan_ready(proj)
    CF.build_plan(rid, proj)
    monkeypatch.setattr(CF, "_clip_evidence", lambda d, vid, m, media, base, root, chat_step=1.5: {
        "decodes": [[], []], "chat": {"ocr_available": True, "samples": 3, "text_boxes": 0, "too_small_in_output": 0, "chat_unverified": False, "verified_clean": True,
                                      "findings": [], "blocked_spans": [], "clean_spans": [], "note": "no severe on-screen text"}})
    CF.build_specs(rid, proj, progress=lambda *_: None)
    return rid


def run_all(proj, rid, words_fn=lambda media, t: []):
    renders = CF.render_all(rid, proj, progress=lambda *_: None)
    assert all(r["error"] is None for r in renders), renders
    return CF.finalize_all(rid, proj, progress=lambda *_: None, words_fn=words_fn)


def test_passed_videos_go_to_the_matching_format_folder_with_readable_names(proj, produced):
    recs = run_all(proj, produced)
    base = proj / "approved_clips"
    assert all(r["status"] == "ready_for_draft_transfer" for r in recs), [(r["stem"], r["needs_review_reasons"]) for r in recs]
    names = {r["format"]: r["stem"] for r in recs}
    assert names["ranking"].startswith("R01-test-moments") and names["funny"] == "F01-quick-laugh-moment" and names["continuous"] == "C01-long-scene-plays-out"
    for fmt, folder in (("ranking", "rankings"), ("funny", "standalone/funny"), ("continuous", "standalone/continuous")):
        stem = names[fmt]
        d = base / folder / produced / stem
        for f in (f"{stem}.mp4", "cover.jpg", "caption.txt", "manifest.json", "qa.json"):
            assert (d / f).is_file(), (d, f)
        m = json.loads((d / "manifest.json").read_text())
        assert m["status"] == "ready_for_draft_transfer" and m["transfer"]["authorized"] is False and m["public_publish"] is False and m["source_sha256"]
        assert (m["rank_count"] == 4) == (fmt == "ranking") and m["accent_color"] and m["manually_reviewed"] is False
        for k in ("output_filename", "source_timecodes", "hook_used", "payoff", "editing_decisions", "final_runtime_sec", "candidate_ids", "intentional_reuse", "qa_result"):
            assert k in m, k                                                                          # the manifest the rules require
    assert not any(c.isdigit() and len(c) > 8 for c in names.values())                                  # readable names, no hash-only filenames
    assert all(len(registry.load(proj)["clips"]) >= 6 for _ in [0])                                     # moments registered as used
    pr = json.loads((base / "rankings" / produced / names["ranking"] / "manifest.json").read_text())
    assert len({c for c in pr["text_colors"].values()}) == 1                                            # one accent color per video


def test_dashboard_links_every_finished_video(proj, produced):
    recs = run_all(proj, produced)
    RunStatus(proj / "runs" / produced).update(segments_detected=6)
    idx = CF.build_dashboard(produced, proj)
    html = idx.read_text()
    assert idx.parent == proj / "approved_clips" / "_reports" / SID / produced and "http://" not in html and "https://" not in html and "src=\"data:image/jpeg;base64," in html
    for r in recs:
        link = f'href="{__import__("os").path.relpath(proj / r["final"], idx.parent)}"'
        assert link in html and (idx.parent / __import__("os").path.relpath(proj / r["final"], idx.parent)).resolve().is_file()
    summary = json.loads((idx.parent / "summary.json").read_text())
    assert summary["counts"]["ready_for_drafts"] == 3 and summary["counts"]["by_format"] == {"ranking": 1, "funny": 1, "continuous": 1}
    for needle in ("Segments detected", "Needs review", "cover title", "accent", "QA "):
        assert needle in html, needle


def test_sloppy_cuts_fail_qa_and_go_to_needs_review(proj, produced):
    mid_word = lambda media, t: [(t - 0.3, t + 0.3, "inter-rupted")]                                     # every cut lands inside a word
    recs = run_all(proj, produced, words_fn=mid_word)
    assert recs and all(r["status"] == "needs_review" for r in recs)
    assert all(any("falls inside the word" in x for x in r["needs_review_reasons"]) for r in recs)
    for r in recs:
        assert (proj / "needs-review" / produced / r["stem"] / f"{r['stem']}.mp4").is_file()
        assert "falls inside the word" in (proj / "needs-review" / produced / r["stem"] / "FAILURE.txt").read_text()   # written failure reason
    assert not (proj / "approved_clips" / "rankings" / produced).exists()
    assert not [c for c in registry.load(proj)["clips"] if c["run_id"] == produced]                    # nothing was registered as used


def test_one_failed_render_does_not_stop_the_other_outputs(proj, produced, monkeypatch):
    real = R.render_standalone
    monkeypatch.setattr(R, "render_standalone", lambda *a, **k: (_ for _ in ()).throw(R.RenderError("boom")) if "F01" in str(a[2]) else real(*a, **k))
    recs = run_all.__wrapped__(proj, produced) if hasattr(run_all, "__wrapped__") else None
    renders = CF.render_all(produced, proj, progress=lambda *_: None)
    assert any(r["error"] and "boom" in r["error"] for r in renders) and any(r["error"] is None for r in renders)
    done = CF.finalize_all(produced, proj, progress=lambda *_: None, words_fn=lambda m, t: [])
    assert {r["status"] for r in done} == {"ready_for_draft_transfer", "render_failed"} and recs is None      # the failed one is listed for review


def test_cleanup_preserves_sources_catalog_thumbnails_and_finals(proj, produced):
    recs = run_all(proj, produced)
    CF.build_dashboard(produced, proj)
    lib = proj / "source_library" / SID
    keep = [lib / "master.mp4", lib / "source.json", lib / "transcript" / "transcript.json", lib / "analysis" / "segments.csv", lib / "analysis" / "timeline.json",
            lib / "analysis" / "candidates.json", lib / "analysis" / "thumbnails" / "S001.jpg", proj / "registry" / "used_clips.json",
            *[proj / r["final"] for r in recs], proj / "approved_clips" / "_reports" / SID / produced / "index.html", proj / "runs" / produced / "status.json"]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in keep}
    scratch = proj / "runs" / produced / "working" / "rankings" / "scratch.tmp"
    scratch.write_bytes(b"x" * 1000)
    (proj / "runs" / produced / "segments" / "shortlisted-previews" / "p.mp4").write_bytes(b"p" * 500)
    res = CF.clean_run(produced, proj, trash_dir=proj / "trash")
    assert res["result"] == "completed" and res["moved"] >= 2 and res["bytes"] >= 1500
    assert not scratch.exists() and len(list((proj / "trash").iterdir())) == res["moved"]                # moved to the Trash, not deleted
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in keep} == before                       # everything protected is byte-identical
    assert CF.clean_run(produced, proj, trash_dir=proj / "no-trash")["result"] == "failed" and (proj / "runs" / produced / "logs" / "cleanup.json").exists()


# ---- 14: registry protections ---------------------------------------------------------------------------------------
def test_used_moments_are_never_planned_again_until_the_user_rejects_the_run(proj, produced):
    run_all(proj, produced)
    registry.release(produced, proj)
    d2 = CF.start(SID, proj).name
    pl = CF.build_plan(d2, proj)
    assert pl["videos"] == [] and any("already used" in r["reason"] for r in pl["rejected"])             # registry blocks reuse across runs
    n = registry.reject_run(produced, "rejected by user", proj)
    assert n >= 6 and all(c["status"] == "rejected_by_user" and c["rejection_reason"] for c in registry.load(proj)["clips"] if c["run_id"] == produced)
    pl2 = CF.build_plan(d2, proj)                                                                         # released for reuse; audit history kept
    assert pl2["videos"]


def test_legacy_root_files_are_moved_into_the_clean_layout(tmp_path):
    lib = tmp_path / "lib"
    (lib / "frames").mkdir(parents=True)
    (lib / "frames" / "a.jpg").write_bytes(b"1")
    for n in ("transcript.json", "transcript.srt", "analysis.json", "candidates.json"):
        (lib / n).write_text("{}")
    moved = layout.migrate(lib)
    assert len(moved) == 5 and (lib / "transcript" / "transcript.json").is_file() and (lib / "analysis" / "signals.json").is_file()
    assert (lib / "analysis" / "thumbnails" / "_sampled" / "a.jpg").is_file() and not (lib / "transcript.json").exists()
    assert layout.migrate(lib) == []                                                                       # idempotent, never overwrites


# ---- new policy: honest counts, ledger, mandatory passes, reuse, duplicate exports, intake -----------------------------
def test_partition_keeps_every_honest_entry_and_never_pads():
    assert CF._partition(2) == [-2] and CF._partition(0) == []
    assert CF._partition(3) == [3] and CF._partition(4) == [4] and CF._partition(7) == [7] and CF._partition(10) == [10]
    p = CF._partition(11)                                           # only the header's technical limit splits a theme, into balanced parts >= 3
    assert sum(p) == 11 and all(3 <= x <= 10 for x in p) and max(p) - min(p) <= 1


def test_three_honest_moments_make_a_ranking_and_the_ledger_records_both_passes(proj):
    analyzed(proj)
    segs = [c for c in FA.load_candidates(SID, proj)["candidates"] if c["duration"] >= 5]
    spec = {c["id"]: ("ranking", n, "Tiny Group") for c, n in zip(segs[:3], ["One Two Three", "Four Five Six", "Seven Eight Nine"])}
    spec[segs[3]["id"]] = ("funny", "Quick Laugh Moment", None)
    review(proj, spec)
    rid = CF.start(SID, proj).name
    pl = CF.build_plan(rid, proj)
    assert [len(v["moments"]) for v in pl["videos"] if v["format"] == "ranking"] == [3]                # a Top 3 is a valid ranking
    led = json.loads((proj / "runs" / rid / "candidates" / "ledger.json").read_text())
    assert led["passes"]["ranking_pass"]["videos"] == ["R01"] and led["passes"]["standalone_pass"]["videos"] == ["F01"]
    assert len(led["candidates"]) == len(FA.load_candidates(SID, proj)["candidates"])                   # every candidate is accounted for
    assert all(r["used_in"] or r["not_used_because"] for r in led["candidates"])
    assert (proj / "runs" / rid / "projects" / "rankings").is_dir() and any((proj / "runs" / rid / "projects" / "standalone").iterdir())


def test_a_missing_format_is_explained_in_the_ledger(proj):
    analyzed(proj)
    segs = [c for c in FA.load_candidates(SID, proj)["candidates"] if c["duration"] >= 5]
    review(proj, {segs[0]["id"]: ("funny", "Quick Laugh Moment", None)})
    rid = CF.start(SID, proj).name
    CF.build_plan(rid, proj)
    led = json.loads((proj / "runs" / rid / "candidates" / "ledger.json").read_text())
    assert led["passes"]["ranking_pass"]["none_because"] and led["passes"]["standalone_pass"]["videos"] == ["F01"]


def test_recorded_reuse_gives_a_ranking_entry_a_materially_different_standalone(proj):
    analyzed(proj)
    segs = [c for c in FA.load_candidates(SID, proj)["candidates"] if c["duration"] >= 5]
    spec = {c["id"]: ("ranking", n, "Test Moments") for c, n in zip(segs[:3], ["Wait What Happened", "He Really Did It", "No Way Moment"])}
    review(proj, spec)
    data = FA.load_candidates(SID, proj)
    c0 = data["candidates"][[c["id"] for c in data["candidates"]].index(segs[0]["id"])]
    c0["review"]["standalone_reuse"] = {"title": "Whole Scene Plays Out", "reason": "full scene with context", "use_start": c0["start"], "use_end": min(c0["end"] + 0.6 * (c0["end"] - c0["start"]), 69.0)}
    FA._write(proj / "source_library" / SID / "analysis" / "candidates.json", data)
    rid = CF.start(SID, proj).name
    pl = CF.build_plan(rid, proj)
    reused = [v for v in pl["videos"] if v.get("reuse")]
    assert len(reused) == 1 and reused[0]["reuse"]["of"] == "R01" and reused[0]["format"] == "funny", pl["rejected"]
    led = json.loads((proj / "runs" / rid / "candidates" / "ledger.json").read_text())
    assert next(r for r in led["candidates"] if r["id"] == segs[0]["id"])["intentional_reuse"] is True


def test_duplicate_exports_are_detected_by_content_hash(tmp_path):
    CF.exports_register(tmp_path, "abc", "R01", "run1", "approved_clips/rankings/run1/R01-x/R01-x.mp4", [[0, 5]])
    assert CF.exports_find(tmp_path, "abc", "R02", "run2") is None                    # the file is gone: nothing to collide with
    f = tmp_path / "approved_clips/rankings/run1/R01-x/R01-x.mp4"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    assert CF.exports_find(tmp_path, "abc", "R02", "run2") == "approved_clips/rankings/run1/R01-x/R01-x.mp4"
    assert CF.exports_find(tmp_path, "abc", "R01", "run1") is None                    # re-finalizing the same output is not a duplicate
    assert CF.exports_find(tmp_path, "zzz", "R02", "run2") is None


def test_intake_pairs_video_and_audio_from_sourcevideo(tmp_path):
    from clipagent import intake
    inbox = tmp_path / "sourcevideo"
    inbox.mkdir()
    for n in ("show.mp4", "show.m4a", ".DS_Store", "notes.txt"):
        (inbox / n).write_bytes(b"x")
    found = intake.scan(tmp_path)
    assert len(found) == 1 and found[0]["video"].name == "show.mp4" and found[0]["audio"].name == "show.m4a" and found[0]["where"] == "sourcevideo"
