import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from clipagent import cleanup as C
from clipagent import registry as REG
from clipagent import source_library as SL
from clipagent import transcribe as T
from clipagent.cli import app

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
RIGHTS = "test fixture"


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("srcmedia")
    ff("-f", "lavfi", "-i", "testsrc=d=60:s=160x90:r=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "video_a.mp4"))
    ff("-f", "lavfi", "-i", "testsrc2=d=60:s=160x90:r=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "video_b.mp4"))
    ff("-f", "lavfi", "-i", "anoisesrc=d=60:c=white:r=44100:seed=1:amplitude=0.4", "-c:a", "aac", "-b:a", "128k", str(d / "audio_a.m4a"))
    # SAME content, different filename + sample rate + bitrate (a "re-download")
    ff("-f", "lavfi", "-i", "anoisesrc=d=60:c=white:r=44100:seed=1:amplitude=0.4", "-ar", "48000", "-c:a", "aac", "-b:a", "64k",
       str(d / "totally_other_name.m4a"))
    ff("-f", "lavfi", "-i", "anoisesrc=d=60:c=white:r=44100:seed=2:amplitude=0.4", "-c:a", "aac", "-b:a", "128k", str(d / "audio_b.m4a"))
    ff("-f", "lavfi", "-i", "anoisesrc=d=60:c=white:r=44100:seed=3:amplitude=0.4", "-c:a", "pcm_s16le", str(d / "audio_wav.wav"))
    ff("-f", "lavfi", "-i", "anoisesrc=d=20:c=white:r=44100:seed=1", "-c:a", "aac", str(d / "audio_short.m4a"))
    return d


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_ROOT", str(tmp_path))
    return tmp_path


def video_bitstream_hash(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "h264", "-"],
                         capture_output=True, check=True).stdout
    return hashlib.sha256(out).hexdigest()


def ingest_a(media, root):
    return SL.ingest_source(media / "video_a.mp4", media / "audio_a.m4a", rights=RIGHTS, root=root)


# 1 + 4
def test_separate_video_and_audio_become_a_validated_canonical_master(media, root):
    res = ingest_a(media, root)
    sid = res["source_id"]
    master = root / "source_library" / sid / "master.mp4"
    meta = json.loads((root / "source_library" / sid / "source.json").read_text())
    assert res["created"] and master.is_file() and SL.sha256_file(master) == meta["sha256"] and sid == meta["sha256"][:12]
    assert meta["status"] == "active" and meta["ingest"]["combined_from_separate_files"] is True
    p = SL.probe_media(master)
    assert p["video"]["codec"] == "h264" and p["audio"]["codec"] == "aac" and (p["video"]["width"], p["video"]["height"]) == (160, 90)
    assert abs(p["duration_sec"] - 60) < 1 and meta["validation"]["full_decode_clean"] and meta["validation"]["start_skew_sec"] <= SL.SYNC_TOL
    assert "lip-sync" in meta["validation"]["manual_review"]                       # not claimed as verified
    assert {i["role"] for i in meta["inputs"]} == {"video", "audio"}
    for sub in ("transcripts", "analysis", "fingerprint"):
        assert (root / "source_library" / sid / sub).is_dir()
    assert not [p for p in (root / "source_library").iterdir() if p.name.startswith(".incoming")]     # temp build folder gone
    assert REG.load(root)["sources"][meta["sha256"]]["id"] == sid and REG.load(root)["clips"] == []   # source fingerprint registered


# 2
def test_video_is_stream_copied_not_reencoded(media, root):
    cmd = SL.mux_command(Path("v.mp4"), Path("a.m4a"), "aac", Path("o.mp4"))
    assert cmd[cmd.index("-c:v") + 1] == "copy" and "libx264" not in cmd and cmd[cmd.index("-c:a") + 1] == "copy"
    assert SL.mux_command(Path("v"), Path("a.wav"), "pcm_s16le", Path("o"))[-7:-1] == ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    res = ingest_a(media, root)
    assert video_bitstream_hash(res["master"]) == video_bitstream_hash(media / "video_a.mp4")     # identical video bitstream
    meta = json.loads((root / "source_library" / res["source_id"] / "source.json").read_text())
    assert meta["ingest"] == {"video_reencoded": False, "audio_mode": "copy", "combined_from_separate_files": True}


def test_non_aac_audio_is_encoded_audio_only(media, root):
    res = SL.ingest_source(media / "video_a.mp4", media / "audio_wav.wav", rights=RIGHTS, root=root)
    meta = res["meta"]
    assert meta["ingest"]["audio_mode"] == "encoded_to_aac" and meta["master"]["audio_codec"] == "aac"
    assert video_bitstream_hash(res["master"]) == video_bitstream_hash(media / "video_a.mp4")


# 3
def test_same_content_recognised_despite_filename_and_encoding(media, root):
    first = ingest_a(media, root)
    with pytest.raises(SL.IdentityUncertain) as e:
        SL.ingest_source(media / "video_b.mp4", media / "totally_other_name.m4a", rights=RIGHTS, root=root)
    assert e.value.details[0]["source_id"] == first["source_id"] and e.value.details[0]["verdict"] == "same"
    assert e.value.details[0]["probes_matched"] == e.value.details[0]["probes"]
    assert [p.name for p in (root / "source_library").iterdir() if not p.name.startswith(".")] == [first["source_id"]]   # nothing new stored


def test_different_content_becomes_a_new_independent_source(media, root):
    a = ingest_a(media, root)
    b = SL.ingest_source(media / "video_b.mp4", media / "audio_b.m4a", rights=RIGHTS, root=root)
    assert b["source_id"] != a["source_id"]
    assert b["meta"]["identity"]["results"][0]["verdict"] == "different" and b["meta"]["identity"]["checked_against"] == [a["source_id"]]
    assert {s["id"] for s in SL.list_sources(root)} == {a["source_id"], b["source_id"]}


# 5
def test_failed_validation_creates_nothing_and_never_overwrites_a_master(media, root):
    with pytest.raises(SL.SourceError, match="durations are not compatible"):
        SL.ingest_source(media / "video_a.mp4", media / "audio_short.m4a", rights=RIGHTS, root=root)
    with pytest.raises(SL.SourceError, match="no audio stream"):
        SL.ingest_source(media / "video_a.mp4", media / "video_b.mp4", rights=RIGHTS, root=root)
    with pytest.raises(SL.SourceError, match="not found"):
        SL.ingest_source(media / "nope.mp4", media / "audio_a.m4a", rights=RIGHTS, root=root)
    with pytest.raises(SL.SourceError, match="rights"):
        SL.ingest_source(media / "video_a.mp4", media / "audio_a.m4a", rights=" ", root=root)
    lib_dir = root / "source_library"
    assert not lib_dir.exists() or not any(lib_dir.iterdir())                      # failed attempts left nothing behind
    res = ingest_a(media, root)
    master = Path(res["master"])
    master.write_bytes(b"someone else's file")                                   # a DIFFERENT master now sits at the canonical path
    with pytest.raises(SL.SourceError, match="refusing to overwrite"):
        SL.ingest_source(media / "video_a.mp4", media / "audio_a.m4a", rights=RIGHTS, root=root, skip_identity=True)
    assert master.read_bytes() == b"someone else's file"


def test_reingesting_identical_media_is_idempotent(media, root):
    a = ingest_a(media, root)
    again = SL.ingest_source(media / "video_a.mp4", media / "audio_a.m4a", rights=RIGHTS, root=root, skip_identity=True)
    assert again["created"] is False and again["source_id"] == a["source_id"]


# 6, 7, 8
def test_source_id_mode_reuses_master_transcripts_and_analysis(media, root):
    res = ingest_a(media, root)
    sid = res["source_id"]
    lib = root / "source_library" / sid
    (lib / "transcripts" / f"{sid}.json").write_text('{"segments": []}')
    (lib / "transcripts" / f"{sid}.merged.json").write_text('{"segments": []}')
    (lib / "analysis" / f"{sid}.candidates.json").write_text('{"candidates": []}')
    before = SL.sha256_file(lib / "master.mp4")
    ref = SL.resolve(sid)
    assert ref.library and ref.media == lib / "master.mp4" and ref.transcripts_dir == lib / "transcripts" and ref.analysis_dir == lib / "analysis"
    assert SL.missing_items(sid) == []                                        # nothing to regenerate
    out = CliRunner().invoke(app, ["prepare-run", "--source-id", sid])
    assert out.exit_code == 0, out.output
    run = next((root / "runs").iterdir())
    assert not (run / "source" / "combined.mp4").exists()                      # no re-combining
    orig = json.loads((run / "source" / "originals.json").read_text())["originals"]["video"]
    assert orig["path"] == str(lib / "master.mp4") and orig["sha256"] == before
    assert SL.sha256_file(lib / "master.mp4") == before
    with pytest.raises(T.TranscribeError, match="refusing to overwrite"):       # transcription is not repeated either
        T.transcribe(sid, runner=lambda *a: pytest.fail("must not transcribe again"))


def test_only_missing_items_are_regenerated_in_the_library(media, root):
    sid = ingest_a(media, root)["source_id"]
    lib = root / "source_library" / sid
    (lib / "analysis" / f"{sid}.candidates.json").write_text('{"candidates": []}')
    assert "transcript" in SL.missing_items(sid) and "analysis" not in SL.missing_items(sid)
    seg = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello", "words": []}]
    T.transcribe(sid, runner=lambda media_path, model, progress: (seg, {"language": "en", "language_probability": 1.0}))
    assert (lib / "transcripts" / f"{sid}.json").is_file() and (lib / "transcripts" / f"{sid}.srt").is_file()
    assert (lib / "analysis" / f"{sid}.candidates.json").read_text() == '{"candidates": []}'          # untouched
    assert not (root / "transcripts" / f"{sid}.json").exists()                  # nothing leaked to the legacy folder
    assert "transcript" not in SL.missing_items(sid)


def test_source_id_mode_requires_active_source_with_master(media, root):
    sid = ingest_a(media, root)["source_id"]
    SL.set_status(sid, "archived", root)
    out = CliRunner().invoke(app, ["prepare-run", "--source-id", sid])
    assert out.exit_code == 1 and "not active" in out.output
    assert CliRunner().invoke(app, ["prepare-run", "--source-id", "ffffffffffff"]).exit_code == 1
    assert CliRunner().invoke(app, ["prepare-run", "--source-id", sid, "--video", "x.mp4"]).exit_code == 1


# lifecycle
def test_lifecycle_states(media, root):
    sid = ingest_a(media, root)["source_id"]
    for st in ("exhausted", "archived", "active"):
        assert SL.set_status(sid, st, root)["status"] == st
    with pytest.raises(SL.SourceError, match="PURGE SOURCE"):
        SL.set_status(sid, "purged", root)
    with pytest.raises(SL.SourceError):
        SL.set_status(sid, "bogus", root)
    p = root / "source_library" / sid / "source.json"
    p.write_text(json.dumps(dict(json.loads(p.read_text()), status="purged")))
    with pytest.raises(SL.SourceError, match="cannot be reactivated"):
        SL.set_status(sid, "active", root)


# 9, 10: global duplicate prevention
@pytest.fixture
def reg_root(tmp_path):
    src = {"sha256": "a" * 64, "id": "aaaaaaaaaaaa"}
    clip = {"source_sha256": "a" * 64, "run_id": "r1", "rank": 1, "title": "Used Moment", "start": 100.0, "end": 110.0,
            "transcript_text_sha256": REG.text_fingerprint("you are so bad at this game".split()),
            "sketch": REG.sketch("you are so bad at this game honestly bro".split())}
    REG.register(src, [clip], tmp_path)
    return tmp_path


@pytest.mark.parametrize("start,end", [(100, 110), (102, 108), (95, 115), (105, 120), (90, 102), (108, 130)])
def test_used_and_overlapping_moments_are_rejected(reg_root, start, end):
    assert REG.conflicts("a" * 64, start, end, root=reg_root)


def test_different_unused_moments_from_same_source_stay_eligible(reg_root):
    for start, end in ((0, 20), (130, 150), (116.5, 130), (50, 94), (111, 118), (92, 98), (110, 125), (90, 100)):   # independent neighbours that merely touch/near a used scene are allowed
        assert REG.conflicts("a" * 64, start, end, root=reg_root) == []
    assert REG.conflicts("b" * 64, 100, 110, root=reg_root) == []              # same timestamps, different source, different text


def test_same_exchange_is_caught_globally_even_after_redownload(reg_root):
    new_source = "c" * 64                                                        # a re-downloaded / re-encoded copy: new hash, new timeline
    same_text = REG.text_fingerprint("You are so bad, at this game!".split())
    assert any("same transcript" in w for w in REG.conflicts(new_source, 5000, 5010, text_sha256=same_text, root=reg_root))
    close = REG.sketch("you are so bad at this game honestly bro lol".split())
    assert any("similar" in w for w in REG.conflicts(new_source, 5000, 5010, sk=close, root=reg_root))
    other = REG.sketch("completely different words about another thing entirely here".split())
    assert REG.conflicts(new_source, 5000, 5010, text_sha256="0" * 64, sk=other, root=reg_root) == []     # same creator/topic alone never blocks


def test_active_run_reservations_block_then_release(reg_root):
    REG.reserve("run-x", "a" * 64, [(300.0, 315.0)], reg_root)
    assert any("reserved by active run" in w for w in REG.conflicts("a" * 64, 305, 320, root=reg_root, ignore_run="other"))
    assert REG.conflicts("a" * 64, 305, 320, root=reg_root, ignore_run="run-x") == []
    with pytest.raises(ValueError):
        REG.reserve("run-y", "a" * 64, [(310.0, 320.0)], reg_root)
    assert REG.release("run-x", reg_root) == 1
    assert REG.conflicts("a" * 64, 305, 320, root=reg_root) == []


def test_check_plan_cli_rejects_registry_conflicts(media, root, tmp_path):
    res = ingest_a(media, root)
    sha = res["meta"]["sha256"]
    REG.register({"sha256": sha, "id": res["source_id"]},
                 [{"source_sha256": sha, "run_id": "r1", "rank": 1, "title": "Used Moment", "start": 100.0, "end": 111.0}], root)
    names = ["Chat Roasts Streamer", "Mic Falls Over", "Surprise Jump Scare", "Wrong Button Panic"]

    def plan(starts):
        return [{"category": "A", "moments": [{"rank": 4 - i, "title": names[i], "start": s, "end": s + 11.0, "verified": True}
                                              for i, s in enumerate(starts)]}]
    bad, good = tmp_path / "bad.json", tmp_path / "good.json"
    bad.write_text(json.dumps(plan([20, 103, 300, 400])))
    good.write_text(json.dumps(plan([20, 130, 300, 400])))
    r = CliRunner().invoke(app, ["check-plan", str(bad), "--source-id", res["source_id"]])
    assert r.exit_code == 1 and "used moment" in r.output
    assert CliRunner().invoke(app, ["check-plan", str(good), "--source-id", res["source_id"]]).exit_code == 0


# 11: normal cleanup keeps canonical source assets (uses the cleanup fixtures)
def test_normal_cleanup_preserves_canonical_source_assets(media, root):
    from test_cleanup import RID, record, run_cleanup  # noqa: E402
    sid = ingest_a(media, root)["source_id"]
    lib = root / "source_library" / sid
    for rel in (f"transcripts/{sid}.json", f"transcripts/{sid}.merged.json", f"analysis/{sid}.candidates.json"):
        (lib / rel).write_text("{}")
    run = root / "runs" / RID
    for d in ("source", "transcripts", "candidates", "renders", "qa", "upload-manifests", "tmp"):
        (run / d).mkdir(parents=True)
    final = run / "renders" / "final.mp4"
    ff("-f", "lavfi", "-i", "testsrc=d=1:s=160x96:r=10", "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(final))
    (run / "tmp" / "scratch.png").write_bytes(b"x")
    (run / "qa" / "final.qa.json").write_text('{"verdict": "pass"}')
    (root / "trash").mkdir()
    snap = {p: p.read_bytes() for p in lib.rglob("*") if p.is_file()}
    reg_before = (root / "registry" / "used_clips.json").read_text()
    record(root)
    for evil in (f"source_library/{sid}/master.mp4", f"source_library/{sid}/transcripts/{sid}.json"):
        rec = json.loads((run / "completion.json").read_text())
        (run / "completion.json").write_text(json.dumps(dict(rec, external_cleanup=[evil])))
        with pytest.raises(C.CleanupError):
            C.make_plan(RID, root)
        (run / "completion.json").write_text(json.dumps(rec))
    run_cleanup(root)
    assert {p: p.read_bytes() for p in lib.rglob("*") if p.is_file()} == snap          # master, transcripts, analysis, metadata, probes
    assert (root / "registry" / "used_clips.json").read_text() == reg_before


def test_registry_allows_same_rank_in_different_videos_of_one_run(tmp_path):
    SRC = "d" * 64
    src = {"sha256": SRC, "id": "x"}
    mk = lambda video, rank, a: {"source_sha256": SRC, "run_id": "r1", "rank": rank, "title": f"T {video} {rank}", "start": a, "end": a + 10.0, "video": video}
    REG.register(src, [mk("v1.mp4", 1, 100.0), mk("v1.mp4", 2, 200.0)], tmp_path)
    REG.register(src, [mk("v2.mp4", 1, 400.0), mk("v2.mp4", 2, 500.0)], tmp_path)             # same run, same ranks, other video
    REG.register(src, [mk("v2.mp4", 1, 400.0)], tmp_path)                                      # idempotent re-register
    assert len(REG.load(tmp_path)["clips"]) == 4
    with pytest.raises(ValueError, match="different clip"):
        REG.register(src, [mk("v2.mp4", 1, 410.0)], tmp_path)                                  # same video+rank, different times
