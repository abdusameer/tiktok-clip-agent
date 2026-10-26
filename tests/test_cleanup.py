import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import cleanup as C

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
RID = "20261012-004041-6a6f3783"
PHRASE = f"DELETE RUN {RID}"


@pytest.fixture(scope="session")
def mp4(tmp_path_factory):
    p = tmp_path_factory.mktemp("media") / "final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=160x96:r=10", "-f", "lavfi", "-i", "sine=d=1",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


@pytest.fixture
def proj(tmp_path, mp4):
    root = tmp_path / "proj"
    run = root / "runs" / RID
    for d in ("source", "transcripts", "candidates", "renders", "qa", "upload-manifests", "tmp"):
        (run / d).mkdir(parents=True)
    shutil.copy(mp4, run / "renders" / "final.mp4")
    (run / "renders" / "draft1.mp4").write_bytes(b"d" * 100)
    (run / "source" / "combined.mp4").write_bytes(b"s" * 1000)
    (run / "source" / "originals.json").write_text("{}")
    (run / "transcripts" / "raw.json").write_text("{}")
    (run / "candidates" / "c1.mp4").write_bytes(b"c" * 50)
    (run / "candidates" / "analysis.json").write_text("{}")
    (run / "tmp" / "frame.png").write_bytes(b"p" * 10)
    (run / "qa" / "final.qa.json").write_text('{"verdict": "pass"}')
    (run / "qa" / "final.contact.png").write_bytes(b"q" * 20)
    (run / "upload-manifests" / "final.upload.json").write_text("{}")
    (run / "usage.json").write_text("{}")
    for keep in ("src/clipagent/x.py", "tests/t.py", "config/ranking_style.json", ".claude/skills/a/SKILL.md", "CLAUDE.md", "fonts/f.ttf"):
        f = root / keep
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("keep")
    other = root / "runs" / "20250101-000000-deadbeef"
    other.mkdir()
    (other / "x.mp4").write_bytes(b"o")
    (root / "trash").mkdir()
    return root


def record(root, **kw):
    C.record_completion(RID, "renders/final.mp4", kw.pop("qa", "passed"), kw.pop("asset", "hf_123"), kw.pop("status", "succeeded"),
                        root=root, user_reviewed=True, **kw)


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size if p.is_file() else -1) for p in root.rglob("*"))


def run_cleanup(root):
    return C.execute(RID, PHRASE, root=root, trash_dir=root / "trash")


def test_preview_changes_nothing_and_lists_everything(proj):
    record(proj)
    before = snapshot(proj)
    text = C.preview(RID, proj)
    assert snapshot(proj) == before
    for needle in (RID, "renders/final.mp4", "hf_123", "Total recoverable", PHRASE, "finals/" + RID):
        assert needle in text
    assert "renders/draft1.mp4" in text and "candidates/c1.mp4" in text
    assert "source/combined.mp4" not in text and "transcripts/raw.json" not in text      # canonical media/transcripts never listed


def test_requires_exact_confirmation_phrase(proj):
    record(proj)
    before = snapshot(proj)
    for bad in ("yes", "okay", "", "delete run " + RID, PHRASE + " ", "DELETE RUN other", "DELETE " + RID):
        with pytest.raises(C.CleanupError, match="exactly"):
            C.execute(RID, bad, root=proj, trash_dir=proj / "trash")
    assert snapshot(proj) == before


def test_blocked_when_qa_not_passed(proj):
    record(proj, qa="manual_review")
    before = snapshot(proj)
    with pytest.raises(C.CleanupError, match="QA"):
        run_cleanup(proj)
    assert snapshot(proj) == before


@pytest.mark.parametrize("status,asset", [("failed", "hf_1"), ("succeeded", ""), ("pending", "hf_1")])
def test_blocked_without_confirmed_upload(proj, status, asset):
    record(proj, status=status, asset=asset)
    before = snapshot(proj)
    with pytest.raises(C.CleanupError, match="Higgsfield"):
        run_cleanup(proj)
    assert snapshot(proj) == before


def test_blocked_when_final_missing_empty_or_invalid(proj):
    record(proj)
    f = proj / "runs" / RID / "renders" / "final.mp4"
    for mutate, msg in ((lambda: f.write_bytes(b""), "empty"), (lambda: f.write_bytes(b"not a video"), "ffprobe"),
                        (lambda: f.unlink(), "missing|not a regular|not readable|ffprobe")):
        mutate()
        before = snapshot(proj)
        with pytest.raises(C.CleanupError, match=msg):
            run_cleanup(proj)
        assert snapshot(proj) == before


def test_final_without_audio_is_blocked(proj, tmp_path):
    record(proj)
    silent = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=160x96:r=10", "-c:v", "libx264", str(silent)], check=True)
    shutil.copy(silent, proj / "runs" / RID / "renders" / "final.mp4")
    with pytest.raises(C.CleanupError, match="audio"):
        run_cleanup(proj)


def test_cleanup_keeps_final_and_protected_and_moves_only_allowlisted(proj, mp4):
    record(proj)
    keep_before = {k: v for k, v in snapshot(proj) if not k.startswith(f"runs/{RID}/") and not k.startswith("trash")}
    res = run_cleanup(proj)
    assert res["result"] == "completed" and res["moved"] == 4
    final = proj / "finals" / RID / "final.mp4"
    assert final.read_bytes() == mp4.read_bytes()                       # final kept, byte-identical
    run = proj / "runs" / RID
    assert not (run / "renders" / "final.mp4").exists()                  # moved, not duplicated
    for gone in ("renders/draft1.mp4", "candidates/c1.mp4", "tmp/frame.png", "qa/final.contact.png"):
        assert not (run / gone).exists()
    for kept in ("usage.json", "completion.json", "source/originals.json", "qa/final.qa.json", "upload-manifests/final.upload.json",
                 "source/combined.mp4", "transcripts/raw.json", "candidates/analysis.json"):
        assert (run / kept).exists()
    assert not (run / "tmp").exists() and not (run / "renders").exists()             # emptied dirs removed
    after = {k: v for k, v in snapshot(proj) if not k.startswith(f"runs/{RID}/") and not k.startswith("trash") and not k.startswith("finals")}
    assert after == keep_before                                          # code, config, skills, assets, other runs untouched
    assert (proj / "runs" / "20250101-000000-deadbeef" / "x.mp4").exists()
    trashed = sorted(p.name for p in (proj / "trash").iterdir())
    assert len(trashed) == 4 and all(n.startswith(RID) for n in trashed)


def test_rejects_targets_outside_run_or_protected(proj):
    record(proj)
    recp = proj / "runs" / RID / "completion.json"
    base = json.loads(recp.read_text())
    cases = [("../20250101-000000-deadbeef/x.mp4", "unsafe"), ("/etc/hosts", "unsafe"), ("renders/../../../CLAUDE.md", "unsafe"),
             ("renders/*.mp4", "unsafe"), ("usage.json", "outside|protected"), ("upload-manifests/final.upload.json", "outside|protected"),
             ("qa/final.qa.json", "protected"), ("source/originals.json", "outside|protected"),
             ("source/combined.mp4", "outside|protected"), ("transcripts/raw.json", "outside|protected"), ("candidates/analysis.json", "protected"), ("src/clipagent/x.py", "outside")]
    for bad, msg in cases:
        rec = dict(base, cleanup_eligible=base["cleanup_eligible"] + [bad])
        recp.write_text(json.dumps(rec))
        before = snapshot(proj)
        with pytest.raises(C.CleanupError, match=msg):
            run_cleanup(proj)
        assert snapshot(proj) == before
    recp.write_text(json.dumps(dict(base, cleanup_eligible=["renders/final.mp4"])))
    with pytest.raises(C.CleanupError, match="protected|final MP4 is in the cleanup list"):
        run_cleanup(proj)
    assert (proj / "runs" / RID / "renders" / "final.mp4").exists()


def test_invalid_run_ids_rejected(proj):
    for bad in ("", "*", "../proj", "20261012-004041", RID + "/../x"):
        with pytest.raises(C.CleanupError):
            C.preview(bad, proj)


def test_symlinks_are_not_followed(proj, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.mp4").write_bytes(b"precious")
    run = proj / "runs" / RID
    (run / "tmp" / "link.mp4").symlink_to(outside / "precious.mp4")
    (run / "frames_link").symlink_to(outside)                              # symlinked dir at run level
    record(proj)
    rec = json.loads((run / "completion.json").read_text())
    assert "tmp/link.mp4" in rec["cleanup_eligible"] and not any("precious" in x for x in rec["cleanup_eligible"])
    res = run_cleanup(proj)
    assert (outside / "precious.mp4").read_bytes() == b"precious"        # target untouched
    assert not (run / "tmp" / "link.mp4").exists() and (run / "frames_link").is_symlink()
    assert res["moved"] == 5


def test_symlinked_parent_directory_is_rejected(proj, tmp_path):
    record(proj)
    run = proj / "runs" / RID
    outside = tmp_path / "out2"
    outside.mkdir()
    (outside / "x.png").write_bytes(b"x")
    shutil.rmtree(run / "tmp")
    (run / "tmp").symlink_to(outside)
    recp = run / "completion.json"
    rec = json.loads(recp.read_text())
    recp.write_text(json.dumps(dict(rec, cleanup_eligible=["tmp/x.png"])))
    with pytest.raises(C.CleanupError, match="symlink"):
        run_cleanup(proj)
    assert (outside / "x.png").exists()


def test_second_run_is_safe_noop(proj):
    record(proj)
    run_cleanup(proj)
    snap = snapshot(proj)
    manifest_text = (proj / "finals" / RID / "manifest.json").read_text()
    assert run_cleanup(proj)["result"] == "already_clean"
    assert snapshot(proj) == snap and (proj / "finals" / RID / "manifest.json").read_text() == manifest_text
    assert "already cleaned" in C.preview(RID, proj)


def test_never_overwrites_a_different_existing_final(proj):
    record(proj)
    dest = proj / "finals" / RID
    dest.mkdir(parents=True)
    (dest / "final.mp4").write_bytes(b"different")
    before = snapshot(proj)
    with pytest.raises(C.CleanupError, match="differs"):
        run_cleanup(proj)
    assert snapshot(proj) == before and (dest / "final.mp4").read_bytes() == b"different"


def test_identical_existing_final_is_reused(proj, mp4):
    record(proj)
    dest = proj / "finals" / RID
    dest.mkdir(parents=True)
    shutil.copy(mp4, dest / "final.mp4")
    assert run_cleanup(proj)["result"] == "completed"
    assert (dest / "final.mp4").read_bytes() == mp4.read_bytes()


def test_trash_unavailable_stops_without_changes(proj):
    record(proj)
    before = snapshot(proj)
    with pytest.raises(C.CleanupError, match="Trash is unavailable"):
        C.execute(RID, PHRASE, root=proj, trash_dir=proj / "no-such-trash")
    assert snapshot(proj) == before


def test_manifest_is_accurate(proj, mp4):
    record(proj)
    run = proj / "runs" / RID
    expected = sorted(str(run / x) for x in ("renders/draft1.mp4", "candidates/c1.mp4", "tmp/frame.png", "qa/final.contact.png"))
    sizes = 100 + 50 + 10 + 20
    run_cleanup(proj)
    m = json.loads((proj / "finals" / RID / "manifest.json").read_text())
    assert m["run_id"] == RID and m["final_filename"] == "final.mp4" and m["final_path"] == f"finals/{RID}/final.mp4"
    assert m["size_bytes"] == mp4.stat().st_size and m["resolution"] == "160x96" and abs(m["duration_sec"] - 1.0) < 0.2
    assert m["sha256"] == C.sha256(mp4)
    assert m["higgsfield_asset_id"] == "hf_123" and m["higgsfield_upload_status"] == "succeeded"
    assert m["tiktok_transfer_status"] == "not_started" and m["cleanup_result"] == "completed"
    assert m["total_bytes_trashed"] == sizes and sorted(m["cleaned_paths"]) == expected
    assert m["completed_at"] and m["cleanup_timestamp"]


def test_record_completion_refuses_unreviewed_qa(proj):
    (proj / "runs" / RID / "qa" / "final.qa.json").write_text('{"verdict": "manual_review"}')
    with pytest.raises(C.CleanupError, match="manual_review"):
        C.record_completion(RID, "renders/final.mp4", "passed", "a", "succeeded", root=proj)
    (proj / "runs" / RID / "qa" / "final.qa.json").write_text('{"verdict": "fail"}')
    with pytest.raises(C.CleanupError):
        C.record_completion(RID, "renders/final.mp4", "passed", "a", "succeeded", root=proj, user_reviewed=True)


def test_upload_checksum_mismatch_blocks(proj):
    record(proj, uploaded_sha256="0" * 64)
    with pytest.raises(C.CleanupError, match="differs from the file that was uploaded"):
        run_cleanup(proj)


def test_missing_record_blocks(proj):
    with pytest.raises(C.CleanupError, match="completion.json"):
        C.preview(RID, proj)


def test_cleanup_module_never_uses_rm_or_shell():
    src = Path(C.__file__).read_text()
    for banned in ("shutil.rmtree", "os.remove", "rm -", "shell=True", "glob("):
        assert banned not in src


# --- backfilled runs: 'uploaded' status, external scope, hard-linked alias, registry, metadata ------------------
from clipagent import plan as P
from clipagent import registry as REG

ASSET = "9ae7f5f3-6a4e-49b8-ad42-c9e6c98abd3a"
EVID = {"media_confirm_status": "uploaded", "confirmed_at": "2026-10-12T00:17:08Z"}
SRC_SHA = "a" * 64


@pytest.fixture
def legacy(proj, mp4):
    """A pre-run-system project: final in rendered_clips/ (hard-linked into the run), working files in project dirs."""
    for d in ("rendered_clips", "source_videos", "transcripts", "clip_candidates"):
        (proj / d).mkdir(exist_ok=True)
    shutil.copy(mp4, proj / "rendered_clips" / "vid.final.mp4")
    run = proj / "runs" / RID
    (run / "renders" / "final.mp4").unlink()
    (run / "renders" / "draft1.mp4").unlink()
    os.link(proj / "rendered_clips" / "vid.final.mp4", run / "renders" / "final.mp4")
    files = {"rendered_clips/vid.draft.mp4": b"d" * 70}
    canonical = {"source_videos/vid.mp4": b"s" * 900, "source_videos/Core1_video.mp4": b"v" * 500, "transcripts/vid.json": b"{}",
                 "clip_candidates/vid.rankings.json": b"{}"}
    for rel, data in {**files, **canonical}.items():
        (proj / rel).write_bytes(data)
    (proj / "source_videos" / "abcdef012345.json").write_text(json.dumps({"id": "abcdef012345", "sha256": SRC_SHA}))
    (proj / "source_videos" / ".gitkeep").write_text("")
    REG.register({"sha256": SRC_SHA, "id": "abcdef012345"},
                 [{"source_sha256": SRC_SHA, "run_id": RID, "rank": 1, "title": "Ab Cd", "start": 10.0, "end": 20.0}], proj)
    extra = {"retain_in_run": True, "external_cleanup": list(files) + ["rendered_clips/vid.final.mp4"],
             "external_keep": ["source_videos/abcdef012345.json"], "final_alias": "rendered_clips/vid.final.mp4",
             "registry_required": True, "registry_source_sha256": SRC_SHA, "registry_clip_count": 1,
             "upload_evidence": EVID}
    C.record_completion(RID, "renders/final.mp4", "passed", ASSET, "uploaded", root=proj, user_reviewed=True, extra=extra)
    return proj


def test_uploaded_status_accepted_only_with_asset_id_and_evidence(legacy):
    C.make_plan(RID, legacy)                                              # valid: uploaded + UUID + evidence
    recp = legacy / "runs" / RID / "completion.json"
    base = json.loads(recp.read_text())
    for bad in ({"asset_id": "not-a-uuid"}, {"asset_id": ""}, {"evidence": None}, {"evidence": {"media_confirm_status": "pending", "confirmed_at": "x"}},
                {"status": "failed"}):
        up = dict(base["higgsfield_upload"], **bad)
        recp.write_text(json.dumps(dict(base, higgsfield_upload=up)))
        with pytest.raises(C.CleanupError, match="Higgsfield|uploaded"):
            C.make_plan(RID, legacy)


def test_legacy_cleanup_scope_alias_and_space(legacy, mp4):
    text = C.preview(RID, legacy)
    assert "alias: hard link" in text and "source_videos/vid.mp4" not in text
    plan = C.make_plan(RID, legacy)
    assert plan["bytes"] == 70 + (50 + 10 + 20)                              # draft + in-run candidates/tmp/qa media; alias counts 0
    before = snapshot(legacy)
    assert snapshot(legacy) == before
    res = run_cleanup(legacy)
    assert res["moved"] == 5                                                # 3 in-run + draft + alias
    final = legacy / "runs" / RID / "renders" / "final.mp4"
    assert final.read_bytes() == mp4.read_bytes()                           # canonical final stays in the run
    assert not (legacy / "rendered_clips" / "vid.final.mp4").exists()       # old alias name moved to Trash
    assert (legacy / "runs" / RID / "renders" / "manifest.json").exists()   # manifest stays beside the final
    assert not (legacy / "finals").exists()
    assert not (legacy / "rendered_clips" / "vid.draft.mp4").exists()
    for kept in ("source_videos/vid.mp4", "source_videos/Core1_video.mp4", "source_videos/abcdef012345.json", "transcripts/vid.json",
                 "clip_candidates/vid.rankings.json", "source_videos/.gitkeep", "src/clipagent/x.py", "runs/%s/transcripts/raw.json" % RID,
                 "runs/%s/source/combined.mp4" % RID):
        assert (legacy / kept).exists(), kept                                # canonical sources/transcripts/analysis always survive
    meta = json.loads((legacy / "source_videos" / "abcdef012345.json").read_text())
    assert "local_media_cleaned" not in meta                                 # normal cleanup never touches source metadata
    reg = REG.load(legacy)
    assert reg["sources"][SRC_SHA]["local_media_cleaned"] is False and len(reg["clips"]) == 1
    assert run_cleanup(legacy)["result"] == "already_clean"


def test_alias_must_really_be_a_hard_link_of_the_final(legacy):
    alias = legacy / "rendered_clips" / "vid.final.mp4"
    alias.unlink()
    alias.write_bytes(b"a different file")
    with pytest.raises(C.CleanupError, match="not a hard link"):
        C.make_plan(RID, legacy)


def test_final_cannot_be_listed_as_external_target(legacy):
    recp = legacy / "runs" / RID / "completion.json"
    rec = json.loads(recp.read_text())
    recp.write_text(json.dumps(dict(rec, final_alias=None)))
    with pytest.raises(C.CleanupError, match="final MP4 is in the cleanup list"):
        C.make_plan(RID, legacy)


@pytest.mark.parametrize("bad", ["src/clipagent/x.py", "config/ranking_style.json", "CLAUDE.md", "source_videos/../CLAUDE.md",
                                 "source_videos/sub/x.mp4", "/etc/hosts", "source_videos/vid.mp4", "transcripts/vid.json", "clip_candidates/vid.rankings.json",
                                 "source_library/abcdef012345/master.mp4", ".claude/skills/a/SKILL.md", "registry/used_clips.json",
                                 "source_videos/.gitkeep"])
def test_external_scope_rejects_non_working_paths(legacy, bad):
    recp = legacy / "runs" / RID / "completion.json"
    rec = json.loads(recp.read_text())
    recp.write_text(json.dumps(dict(rec, external_cleanup=rec["external_cleanup"] + [bad])))
    before = snapshot(legacy)
    with pytest.raises(C.CleanupError):
        run_cleanup(legacy)
    assert snapshot(legacy) == before


def test_metadata_updates_are_not_part_of_normal_cleanup(legacy):
    recp = legacy / "runs" / RID / "completion.json"
    rec = json.loads(recp.read_text())
    recp.write_text(json.dumps(dict(rec, metadata_updates=[{"path": "source_videos/abcdef012345.json", "set": {"x": 1}}])))
    with pytest.raises(C.CleanupError, match="PURGE SOURCE"):
        C.make_plan(RID, legacy)


def test_registry_must_hold_the_moments_before_cleanup(legacy):
    (legacy / "registry" / "used_clips.json").write_text(json.dumps({"version": 1, "sources": {}, "clips": []}))
    before = snapshot(legacy)
    with pytest.raises(C.CleanupError, match="registry"):
        run_cleanup(legacy)
    assert snapshot(legacy) == before


def test_external_symlink_not_followed(legacy, tmp_path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"secret")
    (legacy / "rendered_clips" / "link.txt").symlink_to(secret)
    recp = legacy / "runs" / RID / "completion.json"
    rec = json.loads(recp.read_text())
    recp.write_text(json.dumps(dict(rec, external_cleanup=rec["external_cleanup"] + ["rendered_clips/link.txt"])))
    run_cleanup(legacy)
    assert secret.read_bytes() == b"secret" and not (legacy / "rendered_clips" / "link.txt").exists()


def test_manifest_file_is_never_cleanable(proj):
    recp_before = None
    record(proj)
    (proj / "runs" / RID / "renders" / "manifest.json").write_text("{}")
    recp = proj / "runs" / RID / "completion.json"
    rec = json.loads(recp.read_text())
    recp.write_text(json.dumps(dict(rec, cleanup_eligible=rec["cleanup_eligible"] + ["renders/manifest.json"])))
    with pytest.raises(C.CleanupError, match="protected"):
        run_cleanup(proj)


def test_registry_is_append_only_and_blocks_reuse(tmp_path):
    src = {"sha256": SRC_SHA, "id": "x"}
    clip = {"source_sha256": SRC_SHA, "run_id": "r1", "rank": 1, "title": "Ab Cd", "start": 100.0, "end": 110.0}
    REG.register(src, [clip], tmp_path)
    REG.register(src, [clip], tmp_path)                                      # idempotent
    assert len(REG.load(tmp_path)["clips"]) == 1
    with pytest.raises(ValueError, match="overlaps"):
        REG.register(src, [dict(clip, run_id="r2", rank=2, start=105.0, end=120.0)], tmp_path)
    REG.register(src, [dict(clip, run_id="r2", rank=2, start=200.0, end=210.0)], tmp_path)
    assert len(REG.load(tmp_path)["clips"]) == 2
    assert REG.text_fingerprint(["Hello,", "World!"]) == REG.text_fingerprint(["hello", "world"])


def test_plan_rejects_moments_already_in_the_registry():
    from test_make_rankings import video
    used = [{"start": 0.0, "end": 12.0, "run_id": "old-run", "title": "Old Title"}]
    res = P.validate([video("A", 4, 5, 12.0)], used)
    assert any("already used" in e for e in res.errors)
    assert P.validate([video("A", 4, 500, 12.0)], used).ok


# --- canonical source purge: separate flow, separate confirmation -----------------------------------------------
SID = "abcdef012345"


@pytest.fixture
def lib(legacy):
    d = legacy / "source_library" / SID
    d.mkdir(parents=True)
    (d / "master.mp4").write_bytes(b"m" * 800)
    (d / "transcript.json").write_text("{}")
    (d / "source.json").write_text(json.dumps({"id": SID, "sha256": SRC_SHA, "status": "archived", "master": {"sha256": "b" * 64}}))
    (legacy / "source_videos" / f"{SID}.mp4").write_bytes(b"l" * 300)
    return legacy


def test_purge_preview_is_read_only_and_media_only(lib):
    before = snapshot(lib)
    text = C.purge_preview(SID, lib)
    assert snapshot(lib) == before
    assert "source_library/%s/master.mp4" % SID in text and "source_videos/%s.mp4" % SID in text
    assert "transcript.json" not in text and f"PURGE SOURCE {SID}" in text and "1,100" in text


@pytest.mark.parametrize("bad", ["yes", "", "DELETE RUN " + RID, f"PURGE SOURCE {SID} ", f"purge source {SID}", "PURGE SOURCE ffffffffffff"])
def test_purge_needs_exact_phrase(lib, bad):
    before = snapshot(lib)
    with pytest.raises(C.CleanupError, match="exactly"):
        C.purge_execute(SID, bad, root=lib, trash_dir=lib / "trash")
    assert snapshot(lib) == before


def test_run_cleanup_phrase_cannot_purge_and_purge_phrase_cannot_clean_a_run(lib):
    with pytest.raises(C.CleanupError):
        C.execute(RID, f"PURGE SOURCE {SID}", root=lib, trash_dir=lib / "trash")
    with pytest.raises(C.CleanupError):
        C.purge_execute(SID, PHRASE, root=lib, trash_dir=lib / "trash")
    assert (lib / "source_library" / SID / "master.mp4").exists()


def test_purge_moves_media_only_and_keeps_history(lib):
    res = C.purge_execute(SID, f"PURGE SOURCE {SID}", root=lib, trash_dir=lib / "trash")
    assert res["result"] == "purged" and res["moved"] == 2 and res["bytes"] == 1100
    d = lib / "source_library" / SID
    assert not (d / "master.mp4").exists() and not (lib / "source_videos" / f"{SID}.mp4").exists()
    assert (d / "transcript.json").exists() and (d / "source.json").exists()
    meta = json.loads((d / "source.json").read_text())
    assert meta["status"] == "purged" and meta["local_media_cleaned"] and meta["sha256"] == SRC_SHA and meta["master"]["sha256"] == "b" * 64
    reg = REG.load(lib)
    assert reg["sources"][SRC_SHA]["local_media_cleaned"] is True and len(reg["clips"]) == 1
    assert C.purge_execute(SID, f"PURGE SOURCE {SID}", root=lib, trash_dir=lib / "trash")["result"] == "nothing_to_purge"


def test_purge_blocked_without_registry_or_master_fingerprint_or_symlink(lib, tmp_path):
    d = lib / "source_library" / SID
    meta = json.loads((d / "source.json").read_text())
    (d / "source.json").write_text(json.dumps({k: v for k, v in meta.items() if k != "master"}))
    with pytest.raises(C.CleanupError, match="fingerprint"):
        C.purge_plan(SID, lib)
    (d / "source.json").write_text(json.dumps(meta))
    (lib / "registry" / "used_clips.json").write_text(json.dumps({"version": 1, "sources": {}, "clips": []}))
    with pytest.raises(C.CleanupError, match="registry"):
        C.purge_plan(SID, lib)
    for bad in ("../x", "short", "ZZZZZZZZZZZZ", ""):
        with pytest.raises(C.CleanupError):
            C.purge_plan(bad, lib)


def test_purge_does_not_follow_symlinked_master(lib, tmp_path):
    outside = tmp_path / "keepme.mp4"
    outside.write_bytes(b"x")
    m = lib / "source_library" / SID / "master.mp4"
    m.unlink()
    m.symlink_to(outside)
    with pytest.raises(C.CleanupError, match="symlink"):
        C.purge_plan(SID, lib)
    assert outside.exists()


def test_active_source_can_never_be_purged(lib):
    d = lib / "source_library" / SID
    meta = json.loads((d / "source.json").read_text())
    (d / "source.json").write_text(json.dumps(dict(meta, status="active")))
    before = snapshot(lib)
    for fn in (lambda: C.purge_preview(SID, lib), lambda: C.purge_execute(SID, f"PURGE SOURCE {SID}", root=lib, trash_dir=lib / "trash")):
        with pytest.raises(C.CleanupError, match="active"):
            fn()
    assert snapshot(lib) == before and (d / "master.mp4").exists()
