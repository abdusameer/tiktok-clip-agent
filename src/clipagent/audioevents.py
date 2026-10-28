"""Non-speech audio-event analysis of the ORIGINAL source audio: yelling/loud peaks, impacts, silence gaps, music/timbre changes, laughter-like bursts.

Heuristics on a 16 kHz mono mix, 0.25 s hop (no model download). They give the review evidence that a still frame cannot: a payoff that is
heard (a scream, a bang, a pause before a punchline, a crowd of laughs) now shows up as events with times and strengths.
"""
import json
import subprocess
from pathlib import Path

import numpy as np

SR = 16000
HOP = 0.25
FRAME = int(SR * HOP)


def load_mono(media: Path) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def features(x: np.ndarray) -> dict:
    n = len(x) // FRAME
    fr = x[: n * FRAME].reshape(n, FRAME)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    spec = np.abs(np.fft.rfft(fr * np.hanning(FRAME), axis=1))
    freqs = np.fft.rfftfreq(FRAME, 1 / SR)
    cen = (spec * freqs).sum(axis=1) / (spec.sum(axis=1) + 1e-9)
    flux = np.r_[0, np.abs(np.diff(spec / (spec.sum(axis=1, keepdims=True) + 1e-9), axis=0)).sum(axis=1)]
    return {"db": db, "cen": cen, "flux": flux}


def _runs(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            if j - i >= min_len:
                out.append((i, j))
            i = j
        else:
            i += 1
    return out


def detect(f: dict) -> list[dict]:
    db, cen, flux = f["db"], f["cen"], f["flux"]
    med = float(np.median(db))
    sd = float(np.std(db)) or 1.0
    ev: list[dict] = []
    for a, b in _runs(db > med + 1.2 * sd, 2):                      # sustained loud = yelling / screaming / shouting
        ev.append({"type": "loud", "t0": a * HOP, "t1": b * HOP, "strength": round(float(db[a:b].max() - med), 1)})
    d = np.r_[0, np.diff(db)]
    for i in np.where(d > 12)[0]:                                   # abrupt level jump = impact / bang / sting
        ev.append({"type": "impact", "t0": i * HOP, "t1": (i + 1) * HOP, "strength": round(float(d[i]), 1)})
    for a, b in _runs(db < med - 1.6 * sd, 3):                      # silence gap >= 0.75 s inside speech = comedic pause
        ev.append({"type": "silence", "t0": a * HOP, "t1": b * HOP, "strength": round((b - a) * HOP, 2)})
    w = 8                                                           # 2 s windows: timbre/centroid jump = music or scene change
    for i in range(w, len(cen) - w):
        before, after = cen[i - w:i].mean(), cen[i:i + w].mean()
        if abs(after - before) > 0.35 * (before + 1e-9) and flux[i - 1:i + 2].max() > np.percentile(flux, 97):
            ev.append({"type": "music_change", "t0": i * HOP, "t1": i * HOP + HOP, "strength": round(float(abs(after - before)), 1)})
    env = np.maximum(db - med, 0)                                    # laughter-like = bursty energy: many alternating loud/quiet frames within 3 s
    for i in range(0, len(env) - 12, 4):
        seg = env[i:i + 12]
        if seg.mean() > 0.4 * sd and (np.abs(np.diff(seg)) > 0.6 * sd).sum() >= 6:
            ev.append({"type": "laughter_like", "t0": i * HOP, "t1": (i + 12) * HOP, "strength": round(float(seg.mean()), 1)})
    return sorted(ev, key=lambda e: e["t0"])


def analyze(media: Path, cache: Path | None = None) -> dict:
    if cache and cache.is_file():
        return json.loads(cache.read_text())
    x = load_mono(media)
    f = features(x)
    med = float(np.median(f["db"]))
    out = {"hop_sec": HOP, "duration_sec": round(len(x) / SR, 2), "median_db": round(med, 1), "events": detect(f),
           "method": "ffmpeg 16 kHz mono; rms/spectral-centroid/flux heuristics (loud, impact, silence, music_change, laughter_like)"}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
    return out


def summarize(events: list[dict], start: float, end: float) -> dict:
    """Counts and strongest examples of each event type inside [start, end]."""
    inside = [e for e in events if e["t1"] > start and e["t0"] < end]
    out: dict = {}
    for e in inside:
        s = out.setdefault(e["type"], {"count": 0, "max_strength": 0.0, "first_t": round(e["t0"], 2)})
        s["count"] += 1
        s["max_strength"] = max(s["max_strength"], e["strength"])
    return out


def evidence_rows(source_dir: Path, candidates: list[dict], events: list[dict]) -> list[dict]:
    """One row per candidate for the recovery review: full transcript text inside it (word-level transcript), audio-event summary, neighbour ids."""
    tr = json.loads((source_dir / "transcript" / "transcript.json").read_text())["segments"]
    ids = [c["id"] for c in candidates]
    rows = []
    for i, c in enumerate(candidates):
        text = " ".join(s["text"].strip() for s in tr if s["end"] > c["start"] and s["start"] < c["end"])
        rows.append({"id": c["id"], "start": c["start"], "end": c["end"], "transcript": text, "audio_events": summarize(events, c["start"], c["end"]),
                     "neighbors": [ids[j] for j in (i - 1, i + 1) if 0 <= j < len(ids)]})
    return rows
