"""Timeline segmentation and the segment catalog.

The source is usually an already-edited compilation, not one raw recording. Every hard cut is scored for how likely it is a REAL boundary
(a new clip: location/background/audio/conversation reset) versus a camera-angle change inside the same continuing event (kept inside one
segment). Every second of the timeline ends up in exactly one place: a detected segment, or a rejected section with a recorded reason.

Outputs (source_library/<id>/analysis/): timeline.json, segments.csv, candidates.json, rejected.json, thumbnails/ (3-frame strips),
contact-sheets/.
"""
import csv
import json
import re
import subprocess
from pathlib import Path

import numpy as np

from . import analyze as analyze_mod
from . import config, fullanalysis, layout, registry, source_library, style

VISUAL_BOUNDARY = 0.6      # picture similarity below this suggests a different clip
VISUAL_VETO = 0.35        # histogram similarity below this is always a real boundary
MIN_SEGMENT = 2.5          # shorter spans are fragments (recorded as rejected sections, never silently dropped)
END_SCREEN = re.compile(r"thanks for watching|subscribe|watch next|my suggestion|share now", re.I)
CSV_FIELDS = ["segment_id", "start", "end", "duration", "short_description", "transcript_excerpt", "visible_action", "boundary_confidence",
              "humor_score", "context_score", "format_assignment", "rejection_reason", "used_clip_match"]


class SegmentError(Exception):
    pass


# ------------------------------------------------------------------ boundaries
def cut_break(c: dict) -> tuple[bool, float, list[str]]:
    """Is this hard cut a REAL boundary (a new clip) or a camera-angle change inside one continuing event? (is_boundary, confidence, reasons)

    Evidence: picture similarity across the cut, ambience (spectral correlation) and level continuity. A transcript line that seems to span the
    cut is NOT trusted on its own (long auto-transcript segments run across clip boundaries); it only exempts a cut whose picture AND
    ambience clearly continue. A silent side (dropout) makes the audio evidence meaningless, so only the picture counts then.
    """
    k = c.get("continuity") or {}
    vis, corr, jump = k.get("visual_similarity"), k.get("audio_correlation", 1.0), k.get("level_jump_db", 0.0)
    audio_ok = jump < 40
    why = []
    if vis is not None and vis < VISUAL_BOUNDARY:
        why.append(f"background/picture changes (similarity {vis:.2f})")
    if audio_ok and corr < 0.6:
        why.append(f"ambience changes (corr {corr:.2f})")
    if audio_ok and jump > 9.0:
        why.append(f"audio level jumps {jump:.0f} dB")
    if vis is not None and vis < VISUAL_VETO:
        return True, round(min(0.99, 0.75 + (VISUAL_VETO - vis)), 2), why
    if why and k.get("speech_spans_cut") and (vis is None or vis >= 0.7) and (not audio_ok or corr >= 0.8):
        return False, 0.3, ["speech and ambience continue across the cut"]
    if why:
        return True, round(min(0.99, 0.5 + 0.17 * len(why)), 2), why
    return False, 0.3, ["same picture, ambience and level across the cut (camera angle change)"]


# ------------------------------------------------------------------ scores
def humor_score(text: str, dur: float, audio_prom: float, motion: float, reaction: bool) -> float:
    hits = len(analyze_mod.HOOK.findall(text)) + len(analyze_mod.REACTION.findall(text))
    punct = min((text.count("!") + text.count("?")) / 4, 1.0)
    s = 0.25 * min(hits / 3, 1) + 0.2 * min(audio_prom / 15, 1) + 0.2 * min(motion / 25, 1) + 0.15 * punct + 0.2 * (1.0 if reaction else 0.0)
    return round(min(s, 1.0), 2)


def context_score(text: str, dur: float, wps: float, motion: float) -> float:
    s = 0.55
    first = text.strip()
    if first and (fullanalysis.CONTINUATION.match(first) or fullanalysis.PRONOUN.match(first)):
        s -= 0.2
    if dur < 5:
        s -= 0.15
    if not first and motion < 8:
        s -= 0.3
    if wps > 0.5:
        s += 0.2
    if dur >= 8:
        s += 0.1
    return round(max(0.0, min(s, 1.0)), 2)


def short_description(text: str, motion: float, dur: float) -> str:
    t = " ".join(text.split())
    if t:
        return (t[:90] + "...") if len(t) > 90 else t
    return "no speech; " + ("lots of on-screen action" if motion > 20 else "quiet visual moment") + f" ({dur:.0f}s)"


# ------------------------------------------------------------------ build
def _strip(media: Path, a: float, b: float, out_dir: Path, sid: str) -> dict:
    """Three small frames (beginning, middle, end) + one combined strip per segment. Thumbnails only - never video copies."""
    files, items = {}, []
    for name, fr in (("begin", 0.08), ("mid", 0.5), ("end", 0.92)):
        t = a + (b - a) * fr
        f = out_dir / f"{sid}_{name}.jpg"
        fullanalysis._frame(media, t, f, 320)
        files[name] = str(f)
        items.append({"file": str(f), "label": f"{name} {t:.0f}s"})
    spec = {"out": str(out_dir / f"{sid}.jpg"), "cols": 3, "thumb_w": 320, "font": style.load()["fonts"]["bold"], "items": items}
    sp = out_dir / f".{sid}.json"
    sp.write_text(json.dumps(spec))
    subprocess.run([style.load()["fonts"]["renderer_python"], str(Path(__file__).with_name("_sheet.py")), str(sp)], capture_output=True, check=True)
    sp.unlink()
    return files


def _used_match(sha: str, a: float, b: float, root: Path) -> str:
    out = registry.conflicts(sha, a + 0.2, b - 0.2, root=root)
    if not out:
        return "none"
    return "overlap" if any("overlaps" in x for x in out) else ("near" if any("within" in x for x in out) else "text/reserved")


def build(source_id: str, root=None, progress=print, coverage: bool = True) -> dict:
    """Detect every segment, account for the whole timeline, write the catalog + thumbnails + contact sheets. Returns summary."""
    root = Path(root) if root else config.project_root()
    lib = fullanalysis._lib(source_id, root)
    sig_path = lib / "analysis" / "signals.json"
    if not sig_path.is_file():
        raise SegmentError("run the full analysis first (analyze-source)")
    fullanalysis.ensure_continuity(source_id, root, progress)
    a = json.loads(sig_path.read_text())
    segs_t = json.loads((lib / "transcript" / "transcript.json").read_text())["segments"]
    info = json.loads((lib / "source.json").read_text())
    sha, dur = info["sha256"], a["probe"]["duration_sec"]
    media = source_library.resolve(source_id, root, require_active=False).media
    sig = a["signals"]
    step, fps = sig["step_sec"], sig["motion_fps"]
    adb, mot = np.array(sig["audio_db"]), np.array(sig["motion"])
    cuts = a["scenes"]["cuts"]
    breaks = []
    for c in cuts:
        is_b, conf, why = cut_break(c)
        breaks.append({"t": c["t"], "boundary": is_b, "confidence": conf, "why": why})
    edges = [0.0] + [b["t"] for b in breaks if b["boundary"]] + [dur]
    conf_at = {b["t"]: b["confidence"] for b in breaks}

    def build_spans(min_len):
        out = []
        for lo, hi in zip(edges, edges[1:]):
            out.append((lo, hi, "segment" if hi - lo >= min_len else "rejected"))
        return out

    spans = build_spans(MIN_SEGMENT)
    usable = sum(1 for lo, hi, k in spans if k == "segment" and hi - lo >= 4.0)
    second_pass = False
    if coverage and usable < max(8, dur / 60):                  # too few: split long segments at their best internal pause and accept shorter spans
        second_pass = True
        new_edges = set(edges)
        for lo, hi, k in spans:
            if k == "segment" and hi - lo > 30:
                mid = (lo + hi) / 2
                gaps = [(s["start"] - p["end"], (p["end"] + s["start"]) / 2) for p, s in zip(segs_t, segs_t[1:]) if lo + 8 < (p["end"] + s["start"]) / 2 < hi - 8]
                new_edges.add(round(max(gaps)[1], 2) if gaps else round(mid, 2))
        edges = sorted(new_edges)
        spans = build_spans(2.0)
    layout.ensure(lib)
    thumbs = lib / "analysis" / "thumbnails"
    timeline, rows, records, rejected = [], [], [], []
    n = 0
    for lo, hi, kind in spans:
        a0, b0 = round(lo, 2), round(hi, 2)
        txt = " ".join(s["text"].strip() for s in segs_t if s["start"] >= a0 - 0.2 and s["end"] <= b0 + 0.2)
        if kind == "rejected":
            timeline.append({"start": a0, "end": b0, "kind": "rejected", "id": None, "reason": f"fragment shorter than {MIN_SEGMENT:g}s"})
            rejected.append({"start": a0, "end": b0, "reason": f"fragment shorter than {MIN_SEGMENT:g}s between two cuts"})
            continue
        ocr_text = " ".join(h["text"] for h in a["ocr"].get("hits", []) if a0 <= h["t"] <= b0)
        if END_SCREEN.search(ocr_text) or (b0 >= dur - 0.5 and END_SCREEN.search(txt)):
            timeline.append({"start": a0, "end": b0, "kind": "rejected", "id": None, "reason": "end screen"})
            rejected.append({"start": a0, "end": b0, "reason": "end screen / outro (thanks-for-watching card)"})
            continue
        n += 1
        sid = f"S{n:03d}"
        i0, i1 = int(a0 / step), max(int(b0 / step), int(a0 / step) + 1)
        prom = float(np.max(adb[i0:i1]) - np.median(adb)) if i1 > i0 else 0.0
        m0, m1 = int(a0 * fps), max(int(b0 * fps), int(a0 * fps) + 1)
        motion = float(np.mean(mot[m0:m1])) if m1 > m0 and len(mot) else 0.0
        words = sum(len(s["text"].split()) for s in segs_t if s["start"] >= a0 - 0.2 and s["end"] <= b0 + 0.2)
        wps = words / max(b0 - a0, 1)
        react = any(a0 + (b0 - a0) * 0.4 <= p["t"] <= b0 + 1 for p in sig["peaks"]["audio"])
        hs, cs = humor_score(txt, b0 - a0, prom, motion, react), context_score(txt, b0 - a0, wps, motion)
        files = _strip(media, a0, b0, thumbs, sid)
        used = _used_match(sha, a0, b0, root)
        d = b0 - a0
        fmt = ("continuous" if d >= 15 and wps >= 0.8 else ("funny" if hs >= 0.45 and d <= 25 else "ranking")) if 4.0 <= d else "ranking"
        reason = "" if used == "none" else f"already used or reserved ({used})"
        rec = {"id": sid, "kind": "segment", "start": a0, "end": b0, "duration": round(d, 2), "transcript_excerpt": txt[:300],
               "short_description": short_description(txt, motion, d), "visible_action": "", "boundary_confidence": conf_at.get(lo, 0.99 if lo == 0 else 0.5),
               "humor_score": hs, "context_score": cs, "suggested_format": fmt if used == "none" else "rejected", "format": None,
               "rejection_reason": reason, "used_clip_match": used, "words_per_second": round(wps, 2),
               "signals": {"audio_prominence_db": round(prom, 1), "motion": round(motion, 1), "reaction_after": react},
               "frames": {k: str(Path(v).relative_to(lib)) for k, v in files.items()}, "strip": str((thumbs / f"{sid}.jpg").relative_to(lib)),
               "review": {"status": "needs_visual_review"}}
        records.append(rec)
        timeline.append({"start": a0, "end": b0, "kind": "segment", "id": sid, "reason": ""})
        if used != "none":
            rejected.append({"id": sid, "start": a0, "end": b0, "reason": reason})
        rows.append({"segment_id": sid, "start": a0, "end": b0, "duration": round(d, 2), "short_description": rec["short_description"],
                     "transcript_excerpt": txt[:160], "visible_action": "", "boundary_confidence": rec["boundary_confidence"], "humor_score": hs,
                     "context_score": cs, "format_assignment": rec["suggested_format"], "rejection_reason": reason, "used_clip_match": used})
        if n % 10 == 0:
            progress(f"  catalogued {n} segments")
    # contact sheets of the strips (8 segments per sheet)
    cs_dir = lib / "analysis" / "contact-sheets"
    for old in cs_dir.glob("segments_*.jpg"):
        old.unlink()
    for k in range(0, len(records), 8):
        chunk = records[k:k + 8]
        spec = {"out": str(cs_dir / f"segments_{k // 8 + 1:02d}.jpg"), "cols": 1, "thumb_w": 960, "font": style.load()["fonts"]["bold"],
                "items": [{"file": str(lib / r["strip"]), "label": f"{r['id']} {r['start']:.0f}-{r['end']:.0f}s"} for r in chunk]}
        sp = cs_dir / ".spec.json"
        sp.write_text(json.dumps(spec))
        subprocess.run([style.load()["fonts"]["renderer_python"], str(Path(__file__).with_name("_sheet.py")), str(sp)], capture_output=True, check=True)
        sp.unlink()
    accounted = round(sum(t["end"] - t["start"] for t in timeline), 2)
    tl = {"source_id": source_id, "duration_sec": dur, "accounted_sec": accounted, "second_coverage_pass": second_pass,
          "boundaries": breaks, "timeline": timeline}
    (lib / "analysis" / "timeline.json").write_text(json.dumps(tl, indent=2))
    (lib / "analysis" / "rejected.json").write_text(json.dumps(rejected, indent=2))
    with (lib / "analysis" / "segments.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    (lib / "analysis" / "candidates.json").write_text(json.dumps({
        "source_id": source_id, "version": fullanalysis._cfg()["version"], "evidence_required": True, "granularity": "segment",
        "rule": "A segment may only be planned after its thumbnails were visually reviewed (apply-reviews); never from transcript text alone.",
        "counts": {"total": len(records)}, "candidates": records}, indent=2))
    return {"segments": len(records), "rejected_sections": len(rejected), "accounted_sec": accounted, "duration_sec": dur, "second_coverage_pass": second_pass,
            "boundaries_real": sum(1 for b in breaks if b["boundary"]), "boundaries_camera_angle": sum(1 for b in breaks if not b["boundary"])}


def verify_timeline(tl: dict, tol: float = 0.05) -> list[str]:
    """Every part of the source is in exactly one entry: no gaps, no overlaps."""
    probs, cur = [], 0.0
    for e in tl["timeline"]:
        if abs(e["start"] - cur) > tol:
            probs.append(f"gap/overlap at {cur:.2f}s -> {e['start']:.2f}s")
        cur = e["end"]
    if abs(cur - tl["duration_sec"]) > tol:
        probs.append(f"timeline ends at {cur:.2f}s, source is {tl['duration_sec']:.2f}s")
    return probs
