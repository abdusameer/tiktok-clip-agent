"""Canonical source library: source_library/<source-id>/{master.mp4, source.json, transcripts/, analysis/, fingerprint/}.

* Separate video+audio are combined into ONE synchronized master (video stream-copied, audio copied when AAC else encoded to AAC).
* The master is built in a temporary folder and only linked into its canonical place after validation; a different existing master
  is never overwritten.
* Identity is by CONTENT (audio probes), not by filename, encoding or SHA-256.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config, registry

STATES = ("active", "exhausted", "archived", "purged")
LEGACY_STATES = ("awaiting_replacement",)           # source whose local media was cleaned and not yet re-supplied
SOURCE_ID = re.compile(r"^[0-9a-f]{12}$")
LIB = "source_library"
COMPAT_TOL = 0.75       # max |video duration - audio duration| (s) to accept a pair
SYNC_TOL = 0.15         # max stream start/duration skew (s) in the finished master
SR = 8000               # probe sample rate
N_PROBES, PROBE_SEC = 8, 8
MATCH_NCC = 0.6


class SourceError(Exception):
    pass


class IdentityUncertain(SourceError):
    """The new media partially matches an existing source; the user must decide."""

    def __init__(self, msg, details):
        super().__init__(msg)
        self.details = details


def lib_root(root: Path | None = None) -> Path:
    return (Path(root) if root else config.project_root()) / LIB


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


# --- probing ----------------------------------------------------------------------
def probe_media(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        raise SourceError(f"ffprobe could not read {Path(path).name}: {r.stderr.strip()[-200:]}")
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)

    def num(x, d=None):
        try:
            return float(x)
        except (TypeError, ValueError):
            return d
    fps = None
    if v and v.get("avg_frame_rate", "0/0") not in ("0/0", ""):
        n, d = v["avg_frame_rate"].split("/")
        fps = round(float(n) / float(d), 3) if float(d) else None
    return {"path": str(path), "size_bytes": Path(path).stat().st_size, "format": info["format"].get("format_name"),
            "duration_sec": num(info["format"].get("duration"), 0.0),
            "video": None if not v else {"codec": v["codec_name"], "width": v["width"], "height": v["height"], "fps": fps,
                                          "duration_sec": num(v.get("duration")), "start_time": num(v.get("start_time"), 0.0)},
            "audio": None if not a else {"codec": a["codec_name"], "sample_rate": int(a["sample_rate"]), "channels": a["channels"],
                                          "duration_sec": num(a.get("duration")), "start_time": num(a.get("start_time"), 0.0)}}


def decodes_cleanly(path: Path) -> bool:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True).returncode == 0


def mux_command(video: Path, audio: Path, audio_codec: str, out: Path) -> list[str]:
    """Video is always stream-copied. Audio is copied if already AAC, otherwise encoded to AAC (audio only)."""
    a = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    return ["ffmpeg", "-v", "error", "-n", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", *a, "-movflags", "+faststart", str(out)]


def validate_master(path: Path, expected_duration: float) -> dict:
    """Streams present, sync skew within tolerance, full decode clean. Lip-sync itself cannot be proven automatically."""
    p = probe_media(path)
    if not p["video"] or not p["audio"]:
        raise SourceError("master must contain both a video and an audio stream")
    skew_start = abs(p["video"]["start_time"] - p["audio"]["start_time"])
    vd, ad = p["video"]["duration_sec"] or p["duration_sec"], p["audio"]["duration_sec"] or p["duration_sec"]
    skew_dur = abs(vd - ad)
    if skew_start > SYNC_TOL or skew_dur > SYNC_TOL:
        raise SourceError(f"audio/video skew too large (start {skew_start:.3f}s, duration {skew_dur:.3f}s)")
    if abs(p["duration_sec"] - expected_duration) > COMPAT_TOL:
        raise SourceError(f"master duration {p['duration_sec']:.2f}s differs from the inputs ({expected_duration:.2f}s)")
    if not decodes_cleanly(path):
        raise SourceError("master did not decode cleanly")
    return {"streams_present": True, "start_skew_sec": round(skew_start, 4), "duration_skew_sec": round(skew_dur, 4),
            "full_decode_clean": True, "av_sync": "stream timing verified",
            "manual_review": "lip-sync cannot be verified automatically; spot-check a speaking moment"}


# --- audio fingerprint / identity -------------------------------------------------------------
def pcm(path: Path, ss: float | None = None, t: float | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error"] + (["-ss", str(ss)] if ss is not None else []) + (["-t", str(t)] if t is not None else [])
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise SourceError(f"could not decode audio of {Path(path).name}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def make_probes(audio: np.ndarray) -> tuple[list[float], np.ndarray]:
    """N short audio snippets spread over the source; stored (float16) so a re-download can be recognised later."""
    n = int(PROBE_SEC * SR)
    if len(audio) < n * 2:
        n = max(SR, len(audio) // 3)
    ts, snips = [], []
    for i in range(N_PROBES):
        s = int(len(audio) * (i + 0.5) / N_PROBES) - n // 2
        for _ in range(20):                           # step past silence
            seg = audio[max(0, s):max(0, s) + n]
            if len(seg) == n and float(np.std(seg)) > 1e-3:
                break
            s += n
        else:
            continue
        ts.append(round(max(0, s) / SR, 3))
        snips.append(seg.astype(np.float16))
    if not snips:
        raise SourceError("audio is silent; cannot fingerprint")
    return ts, np.stack(snips)


def best_match(ref: np.ndarray, hay: np.ndarray, chunk_sec: int = 600) -> tuple[float, float]:
    """Normalised cross-correlation of `ref` inside `hay`, in chunks (works for 2-hour files). Returns (score, seconds)."""
    ref = ref.astype(np.float64)
    n = len(ref)
    r_e = float(np.sum(ref ** 2)) or 1e-9
    best = (-1.0, 0)
    step = chunk_sec * SR
    for s in range(0, max(1, len(hay) - n + 1), step):
        seg = hay[s:s + step + n].astype(np.float64)
        if len(seg) < n:
            break
        F = 1 << int(np.ceil(np.log2(len(seg) + n)))
        corr = np.fft.irfft(np.fft.rfft(seg, F) * np.fft.rfft(ref[::-1], F), F)[n - 1:len(seg)]
        cs = np.concatenate([[0], np.cumsum(seg ** 2)])
        en = (cs[n:] - cs[:-n])[:len(corr)]
        ncc = corr / np.sqrt(np.maximum(en * r_e, 1e-9))
        i = int(np.argmax(ncc))
        if ncc[i] > best[0]:
            best = (float(ncc[i]), s + i)
    return round(best[0], 4), round(best[1] / SR, 3)


def identify(media: Path, root: Path | None = None) -> list[dict]:
    """Compare new media with every library source that has stored probes. verdict: same | uncertain | different."""
    base = lib_root(root)
    out = []
    if not base.is_dir():
        return out
    hay = None
    for d in sorted(base.iterdir()):
        npz, meta = d / "fingerprint" / "probes.npz", d / "source.json"
        if not (npz.is_file() and meta.is_file()):
            continue
        hay = pcm(media) if hay is None else hay
        info = json.loads(meta.read_text())
        data = np.load(npz)
        ts = info["probes"]["times_sec"]
        hits = []
        for t, snip in zip(ts, data["probes"]):
            score, at = best_match(snip, hay)
            hits.append({"probe_t": t, "score": score, "found_t": at, "offset": round(at - t, 3)})
        good = [h for h in hits if h["score"] >= MATCH_NCC]
        consistent = bool(good) and max(h["offset"] for h in good) - min(h["offset"] for h in good) <= 0.5
        verdict = "same" if len(good) == len(hits) and consistent else ("uncertain" if good else "different")
        out.append({"source_id": d.name, "verdict": verdict, "probes_matched": len(good), "probes": len(hits),
                    "offset_sec": round(good[0]["offset"], 3) if good and consistent else None, "hits": hits})
    return out


# --- ingest ---------------------------------------------------------------------------
def ingest_source(video: Path, audio: Path | None = None, *, rights: str, root: Path | None = None,
                  identity_evidence: dict | None = None, skip_identity: bool = False) -> dict:
    """Validate, combine, verify and store a new canonical source. Never overwrites a different master."""
    if not rights or not rights.strip():
        raise SourceError("a rights/authorization note is required")
    video, audio = Path(video).expanduser().resolve(), (Path(audio).expanduser().resolve() if audio else None)
    for p in (video, audio):
        if p is not None and not p.is_file():
            raise SourceError(f"file not found: {p}")
    vp = probe_media(video)
    if not vp["video"]:
        raise SourceError(f"{video.name} has no video stream")
    ap = probe_media(audio) if audio else None
    if audio and not ap["audio"]:
        raise SourceError(f"{audio.name} has no audio stream")
    if not audio and not vp["audio"]:
        raise SourceError("video has no audio stream; supply --audio")
    audio_src = ap if audio else vp
    if audio:
        vd, ad = vp["duration_sec"], ap["duration_sec"]
        if abs(vd - ad) > COMPAT_TOL:
            raise SourceError(f"video ({vd:.2f}s) and audio ({ad:.2f}s) durations are not compatible")
    expected = vp["duration_sec"]
    base = lib_root(root)
    base.mkdir(exist_ok=True)
    tmp = base / f".incoming-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        master_tmp = tmp / "master.mp4"
        if audio:
            r = subprocess.run(mux_command(video, audio, audio_src["audio"]["codec"], master_tmp), capture_output=True, text=True)
            if r.returncode:
                raise SourceError(f"combining failed: {r.stderr.strip()[-300:]}")
        else:
            shutil.copy2(video, master_tmp)                 # already one file: byte copy, no re-encode
        report = validate_master(master_tmp, expected)
        digest = sha256_file(master_tmp)
        sid = digest[:12]
        mp = probe_media(master_tmp)
        ident = []
        if not skip_identity:
            ident = identify(master_tmp, root)
            same = [i for i in ident if i["verdict"] == "same"]
            unsure = [i for i in ident if i["verdict"] == "uncertain"]
            if unsure:
                raise IdentityUncertain("media partially matches an existing source; ask the user", unsure)
            if same:
                raise IdentityUncertain(f"media matches existing source {same[0]['source_id']}; confirm attach vs new", same)
        dest = base / sid
        master = dest / "master.mp4"
        if master.exists():
            if sha256_file(master) != digest:
                raise SourceError(f"a different master already exists for {sid}; refusing to overwrite")
            return {"source_id": sid, "master": str(master), "created": False, "report": report}
        dest.mkdir(exist_ok=True)
        for sub in ("transcripts", "analysis", "fingerprint"):
            (dest / sub).mkdir(exist_ok=True)
        os.link(master_tmp, master)                          # atomic, fails if it appeared meanwhile
        ts, snips = make_probes(pcm(master))
        np.savez_compressed(dest / "fingerprint" / "probes.npz", probes=snips)
        inputs = []
        for role, p, info in (("video", video, vp), ("audio", audio, ap)):
            if p is not None:
                inputs.append({"role": role, "filename": p.name, "path": str(p), "sha256": sha256_file(p),
                               "size_bytes": info["size_bytes"], "duration_sec": info["duration_sec"]})
        meta = {"id": sid, "status": "active", "sha256": digest, "created_at": datetime.now(timezone.utc).isoformat(),
                "rights_note": rights.strip(),
                "master": {"path": f"{LIB}/{sid}/master.mp4", "sha256": digest, "size_bytes": mp["size_bytes"],
                           "duration_sec": mp["duration_sec"], "width": mp["video"]["width"], "height": mp["video"]["height"],
                           "fps": mp["video"]["fps"], "video_codec": mp["video"]["codec"], "audio_codec": mp["audio"]["codec"],
                           "sample_rate": mp["audio"]["sample_rate"], "channels": mp["audio"]["channels"]},
                "inputs": inputs,
                "ingest": {"video_reencoded": False, "audio_mode": "copy" if audio_src["audio"]["codec"] == "aac" else "encoded_to_aac",
                           "combined_from_separate_files": bool(audio)},
                "validation": report,
                "identity": {"checked_against": [i["source_id"] for i in ident], "results": ident, **(identity_evidence or {})},
                "probes": {"times_sec": ts, "seconds_each": PROBE_SEC, "file": "fingerprint/probes.npz"},
                "local_media_cleaned": False}
        write_json_atomic(dest / "source.json", meta)
        registry.register({"sha256": digest, "id": sid, "duration_sec": mp["duration_sec"], "size_bytes": mp["size_bytes"],
                           "original_names": [i["filename"] for i in inputs]}, [], root)
        return {"source_id": sid, "master": str(master), "created": True, "report": report, "meta": meta}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)               # only our own temporary build folder


# --- reuse ----------------------------------------------------------------------------------
@dataclass
class SourceRef:
    id: str
    media: Path
    meta: dict
    transcripts_dir: Path
    analysis_dir: Path
    library: bool


def resolve(video_id: str, root: Path | None = None, require_active: bool = True) -> SourceRef:
    """Library source (preferred) or legacy source_videos/<id>.json. Raises FileNotFoundError for unknown ids."""
    r = Path(root) if root else config.project_root()
    d = r / LIB / video_id
    if SOURCE_ID.fullmatch(video_id or "") and (d / "source.json").is_file() and not d.is_symlink():
        info = json.loads((d / "source.json").read_text())
        if require_active and info.get("status") != "active":
            raise SourceError(f"source {video_id} is '{info.get('status')}', not active")
        media = d / "master.mp4"
        if not media.is_file():
            raise SourceError(f"source {video_id} has no master.mp4 (status {info.get('status')}); supply the media again")
        meta = {**info["master"], "id": video_id, "sha256": info["sha256"], "stored_file": "master.mp4", "has_audio": True}
        return SourceRef(video_id, media, meta, d / "transcripts", d / "analysis", True)
    legacy = r / "source_videos" / f"{video_id}.json"
    if not legacy.exists():
        raise FileNotFoundError(f"unknown video id {video_id}; run `ingest` first")
    meta = json.loads(legacy.read_text())
    return SourceRef(video_id, r / "source_videos" / meta["stored_file"], meta, r / "transcripts", r / "clip_candidates", False)


def missing_items(video_id: str, root: Path | None = None) -> list[str]:
    """Reusable data that must still be generated (everything else is reused as-is)."""
    ref = resolve(video_id, root)
    out = []
    if not (ref.transcripts_dir / f"{video_id}.json").is_file():
        out.append("transcript")
    if not (ref.transcripts_dir / f"{video_id}.merged.json").is_file():
        out.append("merged_transcript(optional)")
    if not (ref.analysis_dir / f"{video_id}.candidates.json").is_file():
        out.append("analysis")
    return out


def set_status(video_id: str, state: str, root: Path | None = None) -> dict:
    if state == "purged":
        raise SourceError("'purged' is set only by the PURGE SOURCE flow")
    if state not in STATES:
        raise SourceError(f"state must be one of {', '.join(STATES)}")
    p = lib_root(root) / video_id / "source.json"
    if not p.is_file():
        raise SourceError(f"unknown source {video_id}")
    meta = json.loads(p.read_text())
    if meta.get("status") == "purged":
        raise SourceError("a purged source cannot be reactivated; ingest the media again")
    meta["status"] = state
    meta["status_changed_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(p, meta)
    return meta


def list_sources(root: Path | None = None) -> list[dict]:
    base, reg = lib_root(root), registry.load(root)
    out = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if (d / "source.json").is_file():
                m = json.loads((d / "source.json").read_text())
                out.append({"id": d.name, "status": m.get("status"), "master": (d / "master.mp4").is_file(),
                            "used_clips": sum(c["source_sha256"] == m.get("sha256") for c in reg["clips"]),
                            "duration_sec": (m.get("master") or {}).get("duration_sec") or (m.get("expected") or {}).get("original_duration_sec")})
    return out
