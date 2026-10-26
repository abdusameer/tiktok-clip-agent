"""Full-video analysis for /make-rankings: the whole video is inspected (audio, frames, scene cuts, on-screen text, motion),
never just its web page, title, description or transcript.

Outputs live in source_library/<source-id>/: transcript.json/.srt, analysis.json, frames/, contact-sheets/, candidates.json
(plus the existing master.mp4, source.json, transcripts/, analysis/). Nothing here installs software, renders clips,
uploads, publishes or deletes. A moment can only be planned once its candidate has been visually reviewed (apply_reviews).
"""
import json
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import analyze as analyze_mod
from . import layout
from . import config, source_library, style, transcribe as transcribe_mod

YOUTUBE_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com/(watch\?|shorts/|live/)|youtu\.be/)[\w\-?=&%./]+$")
CONTINUATION = re.compile(r"^(and|but|so|because|then|which|who|that|cause|or|also)\b", re.I)
PRONOUN = re.compile(r"^(he|she|they|it|his|her|their|him|them|this|that|these|those)\b", re.I)
PHASES = ("beginning", "setup", "main_action", "reaction_payoff", "ending")
CONF = ("high", "medium", "low", "uncertain")
FORMATS = ("ranking", "funny", "continuous", "standalone", "rejected")


class AnalysisError(Exception):
    pass


def _cfg() -> dict:
    return style.load()["analysis"]


def _lib(source_id: str, root=None) -> Path:
    r = Path(root) if root else config.project_root()
    d = r / source_library.LIB / source_id
    if not (d / "source.json").is_file():
        raise AnalysisError(f"unknown source {source_id}")
    layout.migrate(d)                       # legacy root-level files move into the clean layout (moves only)
    layout.ensure(d)
    return d


def _write(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


# ------------------------------------------------------------------ dependencies (reported, never installed)
def _find(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = Path(sys.executable).parent / name          # e.g. the project's .venv/bin/yt-dlp
    return str(local) if local.is_file() else None


def dependencies() -> dict:
    py = style.load()["fonts"]["renderer_python"]
    pillow = subprocess.run([py, "-c", "import PIL"], capture_output=True).returncode == 0
    try:
        import faster_whisper  # noqa: F401
        whisper = True
    except ImportError:
        whisper = False
    return {"ffmpeg": _find("ffmpeg"), "ffprobe": _find("ffprobe"), "yt-dlp": _find("yt-dlp"), "swiftc": _find("swiftc"),
            "pillow": pillow, "faster-whisper": whisper}


def require(*names: str) -> dict:
    deps = dependencies()
    missing = [n for n in names if not deps.get(n)]
    if missing:
        raise AnalysisError("missing dependency (NOT installed automatically): " + ", ".join(missing))
    return deps


# ------------------------------------------------------------------ YouTube (information first, download only when authorized)
def check_url(url: str) -> str:
    if not YOUTUBE_URL.match(url or ""):
        raise AnalysisError(f"not a YouTube video URL: {url!r}")
    return url


def youtube_info(url: str) -> dict:
    """Metadata only: id, url, title, duration, chapters, available captions. Nothing is downloaded."""
    check_url(url)
    ytdlp = require("yt-dlp")["yt-dlp"]
    p = subprocess.run([ytdlp, "--no-playlist", "-J", "--skip-download", url], capture_output=True, text=True)
    if p.returncode:
        raise AnalysisError(f"yt-dlp could not read the video: {p.stderr.strip()[-300:]}")
    j = json.loads(p.stdout)
    return {"video_id": j["id"], "url": j.get("webpage_url") or url, "title": j.get("title"), "duration_sec": j.get("duration"),
            "uploader": j.get("uploader"), "upload_date": j.get("upload_date"),
            "chapters": [{"title": c.get("title"), "start": c.get("start_time"), "end": c.get("end_time")} for c in j.get("chapters") or []],
            "captions": {"manual": sorted((j.get("subtitles") or {}).keys()),
                         "automatic": sorted(k for k in (j.get("automatic_captions") or {}) if k.startswith("en"))},
            "recorded_at": datetime.now(timezone.utc).isoformat()}


def youtube_download(url: str, authorization: str, root=None) -> dict:
    """Download best H.264 video (<=720p, the template's source size) + audio and English captions into source_videos/.

    Only with an explicit authorization note (videos you own, have permission to use, or are otherwise authorized to process).
    """
    if not authorization or not authorization.strip():
        raise AnalysisError("an authorization note is required before downloading (own the video / have permission / otherwise authorized)")
    info = youtube_info(url)
    ytdlp = require("yt-dlp", "ffmpeg", "ffprobe")["yt-dlp"]
    out_dir = (Path(root) if root else config.project_root()) / "source_videos"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"yt_{info['video_id']}.mp4"
    if target.exists():
        raise AnalysisError(f"{target.name} already exists; refusing to overwrite (reuse it with --video)")
    cmd = [ytdlp, "--no-playlist", "-f", _cfg()["youtube_format"], "--merge-output-format", "mp4", "--write-subs", "--write-auto-subs",
           "--sub-langs", "en.*", "--sub-format", "vtt", "-o", str(out_dir / f"yt_{info['video_id']}.%(ext)s"), url]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode or not target.is_file():
        raise AnalysisError(f"download failed: {p.stderr.strip()[-300:]}")
    caps = sorted(out_dir.glob(f"yt_{info['video_id']}.*.vtt"))
    return {"info": info, "video": target, "captions": caps, "authorization": authorization}


def parse_vtt(text: str) -> list[dict]:
    """Minimal WebVTT -> segments (de-duplicating YouTube's rolling captions)."""
    segs, last = [], None
    for blk in re.split(r"\n\s*\n", text):
        m = re.search(r"(\d+):(\d+):([\d.]+) --> (\d+):(\d+):([\d.]+)", blk)
        if not m:
            continue
        a = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        b = int(m[4]) * 3600 + int(m[5]) * 60 + float(m[6])
        lines = [re.sub(r"<[^>]+>", "", ln).strip() for ln in blk.splitlines()[1:] if "-->" not in ln and ln.strip()]
        txt = " ".join(x for x in lines if x).strip()
        if txt and txt != last:
            segs.append({"start": round(a, 2), "end": round(b, 2), "text": txt})
            last = txt
    return segs


# ------------------------------------------------------------------ pieces of the analysis
def probe(media: Path) -> dict:
    p = source_library.probe_media(media)
    v, a = p["video"], p["audio"]
    return {"duration_sec": p["duration_sec"], "video": v, "audio": a}


def scene_cuts_full(media: Path, threshold: float, min_gap: float) -> list[dict]:
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(media), "-an", "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    cuts = []
    for m in re.finditer(r"pts_time:([\d.]+)", p.stderr):
        t = round(float(m.group(1)), 2)
        if not cuts or t - cuts[-1]["t"] > min_gap:
            cuts.append({"t": t})
    return cuts


def _frame(media: Path, t: float, out: Path, width: int) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(media), "-frames:v", "1",
                        "-vf", f"scale={width}:-1", "-q:v", "4", str(out)], capture_output=True, text=True)
    if r.returncode or not out.is_file():
        raise AnalysisError(f"frame extraction failed at {t:.2f}s: {r.stderr.strip()[-200:]}")
    return out


BANDS = 24


def _band_profile(x: np.ndarray) -> np.ndarray:
    """Log power in BANDS bands (a coarse timbre/ambience fingerprint)."""
    if len(x) < 512:
        return np.zeros(BANDS)
    win = np.hanning(512)
    frames = [np.abs(np.fft.rfft(x[i:i + 512] * win)) ** 2 for i in range(0, len(x) - 512, 256)]
    spec = np.mean(frames, axis=0)
    edges = np.unique(np.geomspace(2, len(spec) - 1, BANDS + 1).astype(int))
    prof = np.array([spec[a:b].mean() if b > a else spec[a] for a, b in zip(edges[:-1], edges[1:])])
    return 10 * np.log10(prof + 1e-12)


def _tiny_rgb(media: Path, t: float) -> np.ndarray | None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(media), "-frames:v", "1", "-vf", "scale=32:18,format=rgb24",
                        "-f", "rawvideo", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, 3) if len(r.stdout) == 32 * 18 * 3 else None


def _hist_similarity(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None:
        return None
    ha = np.concatenate([np.histogram(a[:, c], bins=8, range=(0, 256))[0] for c in range(3)]).astype(float)
    hb = np.concatenate([np.histogram(b[:, c], bins=8, range=(0, 256))[0] for c in range(3)]).astype(float)
    return float(np.minimum(ha / ha.sum(), hb / hb.sum()).sum())      # 0..1 histogram intersection


def cut_continuity(media: Path, cuts: list[dict], segs: list[dict]) -> list[dict]:
    """Classify every scene cut: a camera-angle change inside the same continuing event ('continuing': ambience/level carries across, or a
    line of speech spans the cut) versus an unrelated scene accidentally joined ('unrelated')."""
    sr = source_library.SR
    x = source_library.pcm(media)
    out = []
    for c in cuts:
        t = c["t"]
        a, b = x[int(max(t - 1.0, 0) * sr): int(max(t - 0.05, 0) * sr)], x[int((t + 0.05) * sr): int((t + 1.0) * sr)]
        pa, pb = _band_profile(a), _band_profile(b)
        corr = float(np.corrcoef(pa, pb)[0, 1]) if pa.std() > 0 and pb.std() > 0 else 0.0
        db = lambda v: 20 * np.log10(float(np.sqrt((v ** 2).mean())) + 1e-9) if len(v) else -90.0
        jump = abs(db(a) - db(b))
        spans = any(s["start"] <= t - 0.15 and s["end"] >= t + 0.15 for s in segs)                       # one line of speech runs across the cut
        near = [s for s in segs if s["end"] <= t + 0.2 and s["end"] >= t - 1.0] and [s for s in segs if s["start"] >= t - 0.2 and s["start"] <= t + 1.0]
        vis = _hist_similarity(_tiny_rgb(media, t - 0.3), _tiny_rgb(media, t + 0.3))
        continuing = spans or (corr >= 0.8 and jump <= 9.0)
        why = ("speech continues across the cut" if spans else
               f"ambience/level carry across (corr {corr:.2f}, level jump {jump:.1f} dB)" if continuing else
               f"ambience/level change at the cut (corr {corr:.2f}, level jump {jump:.1f} dB)")
        out.append({**c, "continuity": {"kind": "continuing" if continuing else "unrelated", "audio_correlation": round(corr, 3), "level_jump_db": round(jump, 1),
                                        "speech_spans_cut": bool(spans), "speech_each_side": bool(near), "visual_similarity": None if vis is None else round(vis, 3),
                                        "why": why}})
    return out


def ensure_continuity(source_id: str, root=None, progress=print, force: bool = False) -> int:
    """Add per-cut continuity to an existing analysis.json (no re-extraction). Returns how many cuts were classified."""
    root = Path(root) if root else config.project_root()
    lib = _lib(source_id, root)
    ap = lib / "analysis" / "signals.json"
    a = json.loads(ap.read_text())
    cuts = a["scenes"]["cuts"]
    if cuts and all("continuity" in c for c in cuts) and not force and a.get("continuity_version") == 2:
        return 0
    segs = json.loads((lib / "transcript" / "transcript.json").read_text())["segments"]
    media = source_library.resolve(source_id, root, require_active=False).media
    progress(f"classifying {len(cuts)} scene cuts (camera-angle change vs unrelated scene)")
    a["scenes"]["cuts"] = cut_continuity(media, cuts, segs)
    a["scenes"]["unrelated_cuts"] = sum(1 for c in a["scenes"]["cuts"] if c["continuity"]["kind"] == "unrelated")
    a["continuity_version"] = 2                     # v2: corrected histogram similarity scale
    _write(ap, a)
    return len(cuts)


def unrelated_cuts_in(source_id: str, start: float, end: float, root=None) -> list[dict]:
    """Unrelated scene changes inside [start, end] (camera-angle changes within one event are allowed)."""
    try:
        lib = _lib(source_id, root)
        a = json.loads((lib / "analysis" / "signals.json").read_text())
    except (AnalysisError, FileNotFoundError):
        return []
    return [c for c in a["scenes"]["cuts"] if start + 0.3 < c["t"] < end - 0.3 and c.get("continuity", {}).get("kind") == "unrelated"]


def sample_frames(media: Path, lib: Path, duration: float, cuts: list[dict]) -> dict:
    cfg = _cfg()
    step = max(cfg["frame_step_seconds"], duration / cfg["max_frames"])
    d = lib / "analysis" / "thumbnails" / "_sampled"
    d.mkdir(exist_ok=True)
    index = []
    n = int(duration // step) + 1
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vf", f"fps=1/{step},scale={cfg['thumb_width']}:-1", "-q:v", "4",
                        str(d / "u_%05d.jpg")], capture_output=True, text=True)
    if r.returncode:
        raise AnalysisError(f"frame sampling failed: {r.stderr.strip()[-200:]}")
    for i in range(n):
        f = d / f"u_{i + 1:05d}.jpg"
        if f.is_file():
            t = round(i * step, 2)
            new = d / f"f_{int(round(t * 10)):06d}.jpg"
            f.rename(new)
            index.append({"t": t, "file": str(new.relative_to(lib)), "kind": "uniform"})
    for c in cuts[:200]:                       # one frame just after every scene change
        t = round(min(c["t"] + 0.3, max(duration - 0.2, 0)), 2)     # stay inside the media for cuts at the very end
        f = d / f"s_{int(round(t * 10)):06d}.jpg"
        if not f.exists():
            _frame(media, t, f, cfg["thumb_width"])
        index.append({"t": t, "file": str(f.relative_to(lib)), "kind": "scene_cut"})
    index.sort(key=lambda x: x["t"])
    return {"step_sec": round(step, 3), "count": len(index), "dir": "frames", "index": index}


def contact_sheets(lib: Path, frames: dict) -> list[dict]:
    cfg = _cfg()
    uniform = [f for f in frames["index"] if f["kind"] == "uniform"]
    per = cfg["contact_sheet_cols"] * cfg["contact_sheet_rows"]
    out_dir = lib / "analysis" / "contact-sheets"
    out_dir.mkdir(exist_ok=True)
    py = style.load()["fonts"]["renderer_python"]
    sheets = []
    for k in range(0, len(uniform), per):
        chunk = uniform[k:k + per]
        spec = {"out": str(out_dir / f"sheet_{k // per + 1:03d}.jpg"), "cols": cfg["contact_sheet_cols"], "thumb_w": 320,
                "font": style.load()["fonts"]["bold"],
                "items": [{"file": str(lib / f["file"]), "label": f"{int(f['t'] // 60):02d}:{f['t'] % 60:04.1f}"} for f in chunk]}
        sp = out_dir / f".spec_{k}.json"
        sp.write_text(json.dumps(spec))
        r = subprocess.run([py, str(Path(__file__).with_name("_sheet.py")), str(sp)], capture_output=True, text=True)
        sp.unlink()
        if r.returncode:
            raise AnalysisError(f"contact sheet failed: {r.stderr.strip()[-200:]}")
        sheets.append({"file": str(Path(spec["out"]).relative_to(lib)), "t0": chunk[0]["t"], "t1": chunk[-1]["t"], "count": len(chunk)})
    return sheets


def _ocr_tool(root: Path) -> Path | None:
    from . import textsafety
    return textsafety.ocr_tool(root)


def run_ocr(media: Path, lib: Path, frames: dict, root: Path, work: Path) -> dict:
    """OCR the bottom strip (existing burned-in captions) and the top region (chat/overlay text) of sampled frames."""
    cfg = _cfg()
    exe = _ocr_tool(root)
    if exe is None:
        return {"available": False, "missing": "swiftc (macOS Vision OCR helper could not be built)", "hits": [],
                "burned_in_captions": {"present": "unknown", "fraction": None}}
    work.mkdir(parents=True, exist_ok=True)
    uniform = [f for f in frames["index"] if f["kind"] == "uniform"]
    jobs = []
    for f in uniform:
        for region, frac, y0 in (("bottom", cfg["ocr_bottom_fraction"], 1 - cfg["ocr_bottom_fraction"]), ("top", cfg["ocr_top_fraction"], 0.0)):
            out = work / f"{region}_{int(round(f['t'] * 10)):06d}.png"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{f['t']:.2f}", "-i", str(media), "-frames:v", "1", "-vf",
                                f"crop=iw:ih*{frac}:0:ih*{y0},scale=iw*1.5:-1", str(out)], capture_output=True)
            if r.returncode == 0 and out.is_file():
                jobs.append((f["t"], region, out))
    hits, bottom_with_text, frames_checked = [], 0, 0
    for k in range(0, len(jobs), 40):
        batch = jobs[k:k + 40]
        p = subprocess.run([str(exe), *[str(b[2]) for b in batch]], capture_output=True, text=True)
        try:
            res = json.loads(p.stdout)
        except ValueError:
            continue
        for (t, region, _), r in zip(batch, res):
            lines = [x.strip() for x in r.get("lines", []) if len(x.strip()) >= 4]
            if region == "bottom":
                frames_checked += 1
                if any(sum(c.isalpha() for c in x) >= 0.6 * len(x) and len(x) >= 8 for x in lines):
                    bottom_with_text += 1
            if lines:
                hits.append({"t": t, "region": region, "text": " | ".join(lines)[:200]})
    frac = bottom_with_text / frames_checked if frames_checked else 0.0
    shutil.rmtree(work, ignore_errors=True)
    return {"available": True, "frames_checked": frames_checked, "hits": hits,
            "burned_in_captions": {"present": frac >= cfg["burned_in_caption_fraction"], "fraction": round(frac, 3)}}


def signals(media: Path, cuts: list[dict], segs: list[dict], duration: float) -> dict:
    """0.5 s timelines: audio loudness (dB), within-scene motion energy, speech density; plus peak lists."""
    cfg = _cfg()
    step = cfg["signal_step_seconds"]
    db = analyze_mod.loudness_curve(media, step)
    fps = cfg["motion_fps"]
    w, h = 96, 54
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-an", "-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, dtype=np.uint8)
    fr = fr[: len(fr) // (w * h) * (w * h)].reshape(-1, w * h).astype(np.float32)
    motion = np.abs(np.diff(fr, axis=0)).mean(axis=1) if len(fr) > 1 else np.zeros(1)
    mt = (np.arange(len(motion)) + 1) / fps
    cut_t = np.array([c["t"] for c in cuts]) if cuts else np.array([])
    for i, t in enumerate(mt):                                  # a scene change is not "action": mask it out
        if len(cut_t) and np.min(np.abs(cut_t - t)) < 0.75:
            motion[i] = 0.0
    n = len(db)
    speech_wps = np.zeros(n)
    for s in segs:
        wps = max(len(s["text"].split()), 1) / max(s["end"] - s["start"], step)
        a, b = int(s["start"] / step), min(int(np.ceil(s["end"] / step)), n)
        speech_wps[a:b] = np.maximum(speech_wps[a:b], wps)

    def peaks(vals, times, pct, min_gap=3.0):
        if not len(vals):
            return []
        thr = np.percentile(vals, pct)
        order = np.argsort(-vals)
        out = []
        for i in order:
            if vals[i] < thr:
                break
            if all(abs(times[i] - o["t"]) >= min_gap for o in out):
                out.append({"t": round(float(times[i]), 2), "value": round(float(vals[i]), 2)})
        return sorted(out, key=lambda x: x["t"])

    atimes = (np.arange(n) + 0.5) * step
    mpeaks = peaks(motion, mt, 96)
    bounds = [0.0] + [c["t"] for c in cuts] + [duration + 1]
    for lo, hi in zip(bounds, bounds[1:]):                       # the action peak of EACH scene (a quiet scene's best moment counts too)
        idx = np.where((mt >= lo + 0.75) & (mt < hi - 0.25))[0]
        if len(idx) >= 3:
            sc = motion[idx]
            k = int(np.argmax(sc))
            if sc[k] >= max(0.8, 2.0 * float(np.median(sc))) and all(abs(mt[idx[k]] - o["t"]) >= 3.0 for o in mpeaks):
                mpeaks.append({"t": round(float(mt[idx[k]]), 2), "value": round(float(sc[k]), 2)})
    mpeaks.sort(key=lambda x: x["t"])
    return {"step_sec": step, "audio_db": [round(float(x), 1) for x in db], "motion_fps": fps,
            "motion": [round(float(x), 2) for x in motion], "speech_wps": [round(float(x), 2) for x in speech_wps],
            "peaks": {"audio": peaks(db, atimes, 96), "motion": mpeaks}, "duration_sec": duration}


# ------------------------------------------------------------------ transcript
def ensure_transcript(source_id: str, lib: Path, captions: list[Path] | None, progress=print) -> dict:
    """Supplied/manual captions when present; otherwise a local transcript. Auto-generated captions are not trusted."""
    root_json = lib / "transcript" / "transcript.json"
    if root_json.is_file():
        return {"source": "existing", "file": "transcript.json"}
    yt = lib / "youtube.json"
    manual = bool(yt.is_file() and json.loads(yt.read_text())["captions"]["manual"])
    segs, used = None, None
    for c in captions or []:
        if c.suffix == ".vtt" and manual:               # YouTube auto-captions are not reliable enough to trust
            segs, used = parse_vtt(c.read_text()), f"supplied captions ({c.name})"
            break
    tjson = lib / "transcripts" / f"{source_id}.json"
    if segs is None:
        if not tjson.is_file():
            require("faster-whisper")
            progress("no reliable captions: transcribing locally (small.en)")
            transcribe_mod.transcribe(source_id)
        segs, used = json.loads(tjson.read_text())["segments"], "local_whisper_small.en"
    rec = {"source_id": source_id, "transcript_source": used, "segments": segs}
    _write(root_json, rec)
    (lib / "transcript" / "transcript.srt").write_text(transcribe_mod.to_srt(segs))
    return {"source": used, "file": "transcript.json"}


# ------------------------------------------------------------------ candidates with evidence
def _scene_bounds(cuts: list[float], t: float, duration: float) -> tuple[float, float]:
    lo = max([c for c in cuts if c <= t], default=0.0)
    hi = min([c for c in cuts if c > t], default=duration)
    return lo, hi


def _window(vals: list[float], step: float, a: float, b: float) -> tuple[float, float]:
    i0, i1 = max(int(a / step), 0), min(int(b / step) + 1, len(vals))
    seg = vals[i0:i1] or [0.0]
    k = int(np.argmax(seg))
    return round((i0 + k) * step + step / 2, 2), float(seg[k])


def context_check(excerpt: str, dur: float, cuts_inside: int, starts_mid_scene: bool) -> dict:
    reasons = []
    first = excerpt.strip()
    if CONTINUATION.match(first):
        reasons.append("starts as a continuation (and/but/so/because...)")
    if PRONOUN.match(first):
        reasons.append("opens with a pronoun whose referent is not in the clip")
    if dur < 4.0:
        reasons.append("very short; setup may be missing")
    if cuts_inside:
        reasons.append(f"crosses {cuts_inside} scene cut(s); setup/payoff may belong to different scenes")
    if starts_mid_scene and first and first[0].islower():
        reasons.append("begins mid-sentence")
    level = "likely" if len(reasons) >= 2 else ("possible" if reasons else "low")
    return {"needs_outside_context": level, "reasons": reasons}


def build_candidates(source_id: str, lib: Path, media: Path, analysis: dict, segs: list[dict], progress=print, only_raw: list[dict] | None = None,
                     id_start: int = 1) -> dict:
    cfg = _cfg()
    dur = analysis["probe"]["duration_sec"]
    cuts = [c["t"] for c in analysis["scenes"]["cuts"]]
    sig = analysis["signals"]
    step = sig["step_sec"]
    ocr_hits = analysis["ocr"].get("hits", [])

    def _generate() -> list[dict]:
        base_path = lib / "analysis" / f"{source_id}.candidates.json"
        if not base_path.is_file():
            progress("ranking spoken moments from the transcript (loudness + verification)")
            analyze_mod.analyze(source_id, limit=60, progress=progress)
        spoken = json.loads(base_path.read_text())["candidates"]
        ocr_hits = analysis["ocr"].get("hits", [])
        raw = []
        for c in spoken:
            raw.append({"kind": "spoken", "start": c["start"], "end": c["end"], "excerpt": c["text"], "base_score": c.get("score"),
                        "policy_risk": c.get("policy_risk"), "audio_verified": c.get("verified")})
        # visual moments: strong within-scene motion/audio peaks with little speech around them
        taken = [(c["start"], c["end"]) for c in raw]
        vis = []
        for pk in sorted(sig["peaks"]["motion"], key=lambda x: -x["value"]):
            lo, hi = _scene_bounds(cuts, pk["t"], dur)
            a, b = max(pk["t"] - 3.0, lo, 0), min(pk["t"] + 5.0, hi, dur)          # one scene only
            i0, i1 = int(a / step), int(b / step) + 1
            spoken_frac = float(np.mean(np.array(sig["speech_wps"][i0:i1]) > 0)) if i1 > i0 else 1.0
            if spoken_frac < 0.4 and b - a >= cfg["min_candidate_seconds"] and not any(
                    a < y and b > x and (min(b, y) - max(a, x)) > 0.5 * (b - a) for x, y in taken):
                vis.append({"kind": "visual", "start": round(a, 2), "end": round(b, 2), "excerpt": "", "motion_peak": pk})
                taken.append((a, b))
            if len(vis) >= cfg["max_visual_candidates"]:
                break
        raw += vis
        # reactions: loud/low-speech stretch right after a short line (laughter, shouting, crowd)
        react = []
        adb = np.array(sig["audio_db"])
        thr = float(np.percentile(adb, 85)) if len(adb) else 0.0
        for s in segs:
            a, b = s["end"], min(s["end"] + 3.0, dur)
            i0, i1 = int(a / step), int(b / step) + 1
            if i1 - i0 < 3:
                continue
            loud = adb[i0:i1] >= thr
            quiet_speech = float(np.mean(sig["speech_wps"][i0:i1])) < 0.5
            if loud.sum() >= cfg["reaction_min_loud_steps"] and quiet_speech and (s["end"] - s["start"]) <= 6.0:
                start = max(s["start"] - 5.0, 0)
                lo, hi = _scene_bounds(cuts, s["end"], dur)
                start = max(start, lo)
                end = min(b + 0.5, hi)
                if end - start >= cfg["min_candidate_seconds"] and not any(start < y and end > x and (min(end, y) - max(start, x)) > 0.5 * (end - start) for x, y in taken):
                    react.append({"kind": "reaction", "start": round(start, 2), "end": round(end, 2), "excerpt": s["text"],
                                  "after_line": s["text"]})
                    taken.append((start, end))
            if len(react) >= cfg["max_reaction_candidates"]:
                break
        raw += react
        return sorted(raw, key=lambda x: x["start"])[: cfg["max_candidates"]]

    raw = _generate() if only_raw is None else only_raw
    out = []
    for n, c in enumerate(raw, id_start):
        cid = f"c{n:03d}"
        a, b = c["start"], c["end"]
        within = [x for x in cuts if a + 0.3 < x < b - 0.3]
        kinds = {c["t"]: c.get("continuity", {}).get("kind", "unknown") for c in analysis["scenes"]["cuts"]}
        unrelated = [x for x in within if kinds.get(x) == "unrelated"]
        prev_cut = max([x for x in cuts if x <= a + 0.3], default=0.0)
        next_cut = min([x for x in cuts if x >= b - 0.3], default=dur)
        # exact boundaries: snap to transcript segment edges, then clamp to the scene that holds the main action
        main_t, main_motion = _window(sig["motion"], 1 / sig["motion_fps"], a, b)
        peak_t, peak_db = _window(sig["audio_db"], step, a, b)
        main = peak_t if c["kind"] != "visual" else main_t
        lo, hi = _scene_bounds(cuts, main, dur)
        sa = max(min([s["start"] for s in segs if s["end"] > a and s["start"] <= a + 1.0] or [a]), a - 1.5)     # snap only within +-1.5 s
        sb = min(max([s["end"] for s in segs if s["start"] < b and s["end"] >= b - 1.0] or [b]), b + 1.5)
        bounds = {"start": round(max(sa - 0.15, lo + 0.05), 2), "end": round(min(sb + 0.3, hi - 0.05), 2),
                  "snapped_to": "transcript segment edges, clamped to the scene containing the main action"}
        phases = {"beginning": bounds["start"] + 0.2, "setup": bounds["start"] + 0.25 * (bounds["end"] - bounds["start"]), "main_action": main,
                  "reaction_payoff": min(main + 1.5, bounds["end"] - 0.2), "ending": bounds["end"] - 0.2}
        fdir = lib / "analysis" / "thumbnails" / "_sampled" / f"cand_{cid}"
        files = {}
        for ph, t in phases.items():
            t = round(min(max(t, bounds["start"]), max(bounds["end"] - 0.05, bounds["start"])), 2)
            files[ph] = {"t": t, "file": str(_frame(media, t, fdir / f"{ph}.jpg", cfg["thumb_width"]).relative_to(lib))}
        after = [x for x in sig["peaks"]["audio"] if b <= x["t"] <= b + 3.0] + [x for x in sig["peaks"]["motion"] if b <= x["t"] <= b + 3.0]
        words = sum(len(s["text"].split()) for s in segs if s["start"] >= a and s["end"] <= b)
        excerpt = c["excerpt"] or " ".join(s["text"] for s in segs if s["start"] >= a - 0.5 and s["end"] <= b + 0.5)[:300]
        ocr = [h for h in ocr_hits if a <= h["t"] <= b]
        ctx = context_check(excerpt, b - a, len(within), a - prev_cut > 1.5)
        out.append({
            "id": cid, "kind": c["kind"], "start": a, "end": b, "duration": round(b - a, 2), "bounds": bounds,
            "transcript_excerpt": excerpt[:400], "words_per_second": round(words / max(b - a, 1), 2),
            "on_screen_text": [{"t": h["t"], "region": h["region"], "text": h["text"]} for h in ocr[:6]],
            "scene_cuts": {"internal": within, "internal_count": len(within), "previous_cut": prev_cut, "next_cut": next_cut,
                           "starts_after_cut_sec": round(a - prev_cut, 2), "ends_before_cut_sec": round(next_cut - b, 2),
                           "unrelated_internal": unrelated,
                           "quality": "single_scene" if not within else ("crosses_unrelated_cuts" if unrelated else f"crosses_{len(within)}_continuing_cuts")},
            "signals": {"audio_peak": {"t": peak_t, "db": round(peak_db, 1)}, "motion_peak": {"t": main_t, "value": round(main_motion, 2)},
                        "reaction_after": {"detected": bool(after), "peaks": after[:3]}},
            "context": ctx,
            "confidence_auto": {"audio": "high" if c.get("audio_verified") else ("medium" if c["kind"] == "spoken" else "low"),
                                "visual": "medium" if main_motion > 0 else "low"},
            "policy_risk": c.get("policy_risk"),
            "frames": files,
            "review": {"status": "needs_visual_review"},
        })
    return {"source_id": source_id, "version": cfg["version"], "evidence_required": True,
            "rule": "A moment may only be planned after its candidate was visually reviewed (apply_reviews); never from transcript text alone.",
            "counts": {"total": len(out), **{k: sum(1 for c in out if c["kind"] == k) for k in ("spoken", "visual", "reaction")}},
            "candidates": out}


# ------------------------------------------------------------------ orchestration
def status(source_id: str, root=None) -> dict:
    lib = _lib(source_id, root)
    names = ("analysis/signals.json", "transcript/transcript.json", "transcript/transcript.srt", "analysis/candidates.json")
    return {**{n: (lib / n).is_file() for n in names}, "frames": (lib / "analysis" / "thumbnails" / "_sampled").is_dir(), "contact-sheets": (lib / "analysis" / "contact-sheets").is_dir()}


def analyze_source(source_id: str, root=None, force: bool = False, captions: list[Path] | None = None, progress=print) -> dict:
    """Run (or reuse) the full analysis. Reuse = analysis.json/candidates.json exist for this exact master sha256."""
    root = Path(root) if root else config.project_root()
    lib = _lib(source_id, root)
    info = json.loads((lib / "source.json").read_text())
    ap = lib / "analysis" / "signals.json"
    if ap.is_file() and not force:
        old = json.loads(ap.read_text())
        if old.get("sha256") == info["sha256"] and old.get("version") == _cfg()["version"] and (lib / "analysis" / "candidates.json").is_file():
            progress("full analysis already exists for this master; reusing it (no download, transcription or visual analysis repeated)")
            ensure_continuity(source_id, root, progress)                  # older analyses gain cut continuity without redoing anything else
            return {"reused": True, "analysis": json.loads(ap.read_text())}
    require("ffmpeg", "ffprobe", "pillow")
    ref = source_library.resolve(source_id, root, require_active=False)
    media = ref.media
    if not media.is_file():
        raise AnalysisError(f"master.mp4 missing for {source_id}; supply the media again (nothing is re-downloaded automatically)")
    cfg = _cfg()
    progress("1/8 inspecting media")
    pr = probe(media)
    progress("2/8 transcript")
    tr = ensure_transcript(source_id, lib, captions, progress)
    segs = json.loads((lib / "transcript" / "transcript.json").read_text())["segments"]
    progress("3/8 scene changes")
    cuts = scene_cuts_full(media, cfg["scene_threshold"], cfg["scene_min_gap_seconds"])
    progress(f"   {len(cuts)} scene changes")
    cuts = cut_continuity(media, cuts, segs)
    progress("4/8 frames")
    frames = sample_frames(media, lib, pr["duration_sec"], cuts)
    progress("5/8 contact sheets")
    sheets = contact_sheets(lib, frames)
    progress("6/8 on-screen text (OCR)")
    ocr = run_ocr(media, lib, frames, root, lib / ".ocr_tmp")
    progress("7/8 audio and motion signals")
    sig = signals(media, cuts, segs, pr["duration_sec"])
    analysis = {"version": cfg["version"], "continuity_version": 2, "source_id": source_id, "sha256": info["sha256"], "created_at": datetime.now(timezone.utc).isoformat(),
                "probe": pr, "transcript": {**tr, "segments": len(segs)}, "scenes": {"threshold": cfg["scene_threshold"], "cuts": cuts},
                "frames": frames, "contact_sheets": sheets, "ocr": ocr, "signals": sig,
                "youtube": json.loads((lib / "youtube.json").read_text()) if (lib / "youtube.json").is_file() else None}
    progress("8/8 candidates with visual evidence")
    cands = build_candidates(source_id, lib, media, analysis, segs, progress)
    analysis["detections"] = {
        "spoken_moments": cands["counts"]["spoken"], "visual_moments_not_in_dialogue": cands["counts"]["visual"],
        "reaction_moments": cands["counts"]["reaction"], "scene_changes": len(cuts),
        "existing_on_screen_captions": ocr["burned_in_captions"],
        "candidates_crossing_scene_cuts": sum(1 for c in cands["candidates"] if c["scene_cuts"]["internal_count"]),
        "candidates_likely_needing_outside_context": sum(1 for c in cands["candidates"] if c["context"]["needs_outside_context"] == "likely"),
        "candidates_with_reaction_after_line": sum(1 for c in cands["candidates"] if c["signals"]["reaction_after"]["detected"])}
    _write(lib / "analysis" / "candidates.json", cands)
    _write(ap, analysis)
    return {"reused": False, "analysis": analysis, "candidates": cands}


def rebuild_candidates(source_id: str, root=None, progress=print) -> dict:
    """Recompute candidates.json from the stored analysis (no re-transcription / re-extraction). Refuses to discard reviews."""
    root = Path(root) if root else config.project_root()
    lib = _lib(source_id, root)
    cp = lib / "analysis" / "candidates.json"
    if cp.is_file() and any(c["review"]["status"] == "reviewed" for c in json.loads(cp.read_text())["candidates"]):
        raise AnalysisError("candidates already have visual reviews; refusing to overwrite them")
    analysis = json.loads((lib / "analysis" / "signals.json").read_text())
    segs = json.loads((lib / "transcript" / "transcript.json").read_text())["segments"]
    media = source_library.resolve(source_id, root, require_active=False).media
    cands = build_candidates(source_id, lib, media, analysis, segs, progress)
    _write(cp, cands)
    return cands


def add_candidates(source_id: str, ranges: list[tuple[float, float]], root=None, progress=print) -> list[str]:
    """Add hand-picked windows (kind 'manual') with full evidence packs to candidates.json. Existing candidates and reviews are untouched."""
    root = Path(root) if root else config.project_root()
    lib = _lib(source_id, root)
    analysis = json.loads((lib / "analysis" / "signals.json").read_text())
    segs = json.loads((lib / "transcript" / "transcript.json").read_text())["segments"]
    media = source_library.resolve(source_id, root, require_active=False).media
    data = load_candidates(source_id, root)
    nxt = max(int(c["id"][1:]) for c in data["candidates"]) + 1
    raw = []
    for a, b in ranges:
        excerpt = " ".join(s["text"].strip() for s in segs if s["start"] >= a - 0.3 and s["end"] <= b + 0.3)[:300]
        raw.append({"kind": "manual", "start": round(a, 2), "end": round(b, 2), "excerpt": excerpt})
    new = build_candidates(source_id, lib, media, analysis, segs, progress, only_raw=raw, id_start=nxt)["candidates"]
    data["candidates"] += new
    data["counts"]["total"] = len(data["candidates"])
    data["counts"]["manual"] = data["counts"].get("manual", 0) + len(new)
    _write(lib / "analysis" / "candidates.json", data)
    return [c["id"] for c in new]


def load_candidates(source_id: str, root=None) -> dict:
    lib = _lib(source_id, root)
    p = lib / "analysis" / "candidates.json"
    if not p.is_file():
        raise AnalysisError(f"no candidates.json for {source_id}; run the full analysis first")
    return json.loads(p.read_text())


def apply_reviews(source_id: str, reviews: list[dict], root=None) -> dict:
    """Record the visual review of candidates. Every field is checked; a review must cite the frames that were actually viewed."""
    root = Path(root) if root else config.project_root()
    lib = _lib(source_id, root)
    data = load_candidates(source_id, root)
    by_id = {c["id"]: c for c in data["candidates"]}
    done = []
    for r in reviews:
        c = by_id.get(r.get("id"))
        if c is None:
            raise AnalysisError(f"unknown candidate {r.get('id')!r}")
        fmt = r.get("suggested_format")
        if fmt not in FORMATS:
            raise AnalysisError(f"{c['id']}: suggested_format must be one of {FORMATS}")
        viewed = r.get("viewed_frames") or []
        own = {(v["file"] if isinstance(v, dict) else v) for v in c["frames"].values()}
        if len(set(viewed) & own) < 3:
            raise AnalysisError(f"{c['id']}: the review must cite at least 3 of this candidate's own frames (viewed_frames)")
        if any(not (lib / f).is_file() for f in viewed):
            raise AnalysisError(f"{c['id']}: a cited frame does not exist")
        for k in ("audio_confidence", "visual_confidence"):
            if r.get(k) not in CONF:
                raise AnalysisError(f"{c['id']}: {k} must be one of {CONF}")
        if r.get("works_without_context") not in (True, False, "uncertain"):
            raise AnalysisError(f"{c['id']}: works_without_context must be true, false or 'uncertain'")
        if fmt == "rejected":
            if not (r.get("rejection_reason") or "").strip():
                raise AnalysisError(f"{c['id']}: a rejected candidate needs a rejection_reason")
        else:
            for k in ("visible_action", "hook", "setup", "payoff"):
                if not (r.get(k) or "").strip():
                    raise AnalysisError(f"{c['id']}: {k} is required for a {fmt} candidate")
            if "uncertain" in (r["audio_confidence"], r["visual_confidence"]) or r["works_without_context"] == "uncertain":
                raise AnalysisError(f"{c['id']}: uncertain evidence cannot be {fmt}; reject it or fix the evidence")
        c["review"] = {"status": "reviewed", "reviewed_at": datetime.now(timezone.utc).isoformat(),
                       **{k: r.get(k) for k in ("visible_action", "works_without_context", "hook", "setup", "payoff", "audio_confidence",
                                                "visual_confidence", "suggested_format", "rejection_reason", "viewed_frames", "title", "category",
                                                "use_start", "use_end")}}
        done.append(c["id"])
    _write(lib / "analysis" / "candidates.json", data)
    return {"reviewed": done, "remaining": sum(1 for c in data["candidates"] if c["review"]["status"] != "reviewed")}


def evidence_for(source_id: str, start: float, end: float, root=None) -> tuple[bool, str]:
    """Plan gate: the moment must be backed by a reviewed, non-rejected candidate that covers >=60% of it."""
    try:
        data = load_candidates(source_id, root)
    except AnalysisError:
        return True, "no full analysis for this source (legacy source); evidence gate not applied"
    covering = [c for c in data["candidates"] if min(end, c["end"]) - max(start, c["start"]) >= 0.6 * (end - start) > 0]
    if not covering:
        return False, f"no analyzed candidate covers {start:.1f}-{end:.1f}s; moments must come from the full-video analysis"
    for c in covering:                       # a later, more generous review supersedes an earlier rejection
        if c["review"]["status"] == "reviewed" and c["review"]["suggested_format"] != "rejected":
            return True, ""
    c = max(covering, key=lambda c: min(end, c["end"]) - max(start, c["start"]))
    if c["review"]["status"] != "reviewed":
        return False, f"candidate {c['id']} ({start:.1f}-{end:.1f}s) was not visually reviewed; never select from transcript text alone"
    return False, f"candidate {c['id']} was rejected in review: {c['review']['rejection_reason']}"


def sample_report(source_id: str, n: int = 5, seed: int | None = None, root=None) -> str:
    """Random evidence sample for spot-checking the analysis against the real video."""
    lib = _lib(source_id, root)
    data = load_candidates(source_id, root)
    rng = random.Random(seed)
    pick = sorted(rng.sample(data["candidates"], min(n, len(data["candidates"]))), key=lambda c: c["start"])
    L = []
    for c in pick:
        L += [f"{c['id']} [{c['kind']}] {c['start']:.2f}-{c['end']:.2f}s (bounds {c['bounds']['start']}-{c['bounds']['end']}) "
              f"scene cuts: {c['scene_cuts']['quality']}, context: {c['context']['needs_outside_context']}",
              f"   transcript: {c['transcript_excerpt'][:160]!r}",
              f"   on-screen text: {[t['text'][:50] for t in c['on_screen_text'][:2]]}",
              f"   signals: audio peak {c['signals']['audio_peak']}, motion peak {c['signals']['motion_peak']}, "
              f"reaction after: {c['signals']['reaction_after']['detected']}"]
        L += [f"   frame {ph}: {lib / (v['file'] if isinstance(v, dict) else v)}" for ph, v in c["frames"].items()]
    return "\n".join(L)
