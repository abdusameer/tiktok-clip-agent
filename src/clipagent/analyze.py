"""Rank possible short moments from the merged transcript, then verify them against the video.

Scores are transparent heuristics for discovery, not ground truth. Nothing here edits or renders video.
"""
import difflib
import json
import re
import subprocess
from pathlib import Path

import numpy as np

from . import config, models, source_library
from .merge import norm, recheck

# Search-window bounds for the candidate generator only (a search-space limit, NOT an editorial length rule: duration never decides quality).
MIN_LEN, MAX_LEN = 3.0, 180.0
TERMINAL = (".", "?", "!", "...")

HOOK = re.compile(r"\b(wait|bro|what|no way|oh my god|listen|guess|how|why|hold on|are you|did you|i('m| am) (the|not|very))\b|[?!]", re.I)
REACTION = re.compile(r"\b(oh my god|what the fuck|holy shit|oh shit|oh fuck|no no|oh god|are you crazy|what happened|wait|delusional|"
                      r"i can't|bruh|dead ?ass|unhinged|congratulations|hilarious)\b", re.I)
# Policy lexicon: rough screen for TikTok community-guideline risk, not a moderation system.
HIGH_RISK = re.compile(r"\b(dick|pussy|nutted|condom|sex|suck|oiled|fingered|asshole|poop|penis|peanut|bbc|numb my ass|"
                       r"little boys?|disabled|retard\w*|nigg\w*|fag\w*|rape\w*|cum|nuts are)\b", re.I)
MEDIUM_RISK = re.compile(r"\b(fuck\w*|shit\w*|bitch|ass|dumbass|fat ass|pig|idiots?)\b", re.I)


def loudness_curve(media: Path, step: float = 0.5) -> np.ndarray:
    """Per-step loudness in dB across the whole file."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                       capture_output=True)
    x = np.frombuffer(p.stdout, dtype=np.float32)
    n = int(16000 * step)
    x = x[: len(x) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-9)


def policy_risk(text: str):
    high = sorted({m.group(0).lower() for m in HIGH_RISK.finditer(text)})
    med = [m.group(0) for m in MEDIUM_RISK.finditer(text)]
    if high:
        return "high", high
    return ("medium" if len(med) >= 3 else "low"), sorted({m.lower() for m in med})


def candidates_from_segments(segs, db, step=0.5):
    """Every segment-aligned window of MIN_LEN..MAX_LEN seconds that ends on a sentence boundary."""
    med_db, sd = float(np.median(db)), float(np.std(db)) or 1.0
    out = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            start, end = segs[i]["start"], segs[j]["end"]
            dur = end - start
            if dur > MAX_LEN:
                break
            if dur < MIN_LEN or not segs[j]["text"].rstrip().endswith(TERMINAL):
                continue
            if any(segs[k + 1]["start"] - segs[k]["end"] > 4.0 for k in range(i, j)):
                continue  # dead air inside the window
            chunk = segs[i:j + 1]
            text = " ".join(s["text"] for s in chunk)
            words = len(text.split())
            a, b = int(start / step), max(int(end / step), int(start / step) + 1)
            seg_db = db[a:b]
            energy = float(np.clip(((seg_db.mean() - med_db) / sd + (seg_db.max() - med_db) / sd) / 4 + 0.5, 0, 1))
            first = chunk[0]["text"]
            hook = 1.0 if HOOK.search(first) else 0.4
            tail = " ".join(s["text"] for s in chunk[len(chunk) // 2:])
            react = min(1.0, len(REACTION.findall(text)) * 0.5 + (0.3 if REACTION.search(tail) else 0))
            density = float(np.clip(1 - abs(words / dur - 3.0) / 3.0, 0, 1))
            gap_before = (segs[i]["start"] - segs[i - 1]["end"]) if i else 5.0
            complete = 0.5 * (gap_before >= 0.8 or (i and segs[i - 1]["text"].rstrip().endswith(TERMINAL))) + 0.5
            flagged = sum(1 for s in chunk if s["flags"]) / len(chunk)
            risk, risk_terms = policy_risk(text)
            score = 0.25 * hook + 0.25 * react + 0.20 * energy + 0.10 * density + 0.20 * complete
            score -= 0.25 * flagged + (0.15 if risk == "medium" else 0)
            out.append({"start": round(start, 2), "end": round(end, 2), "dur": round(dur, 1), "text": text,
                        "speakers": sorted({s["speaker"] for s in chunk}), "score": round(score, 3),
                        "features": {"hook": hook, "reaction": round(react, 2), "energy": round(energy, 2),
                                     "density": round(density, 2), "complete": complete},
                        "segment_ids": [s["id"] for s in chunk], "flagged_segment_ids": [s["id"] for s in chunk if s["flags"]],
                        "flag_fraction": round(flagged, 2), "flags": sorted({f for s in chunk for f in s["flags"]}),
                        "timing_estimated_fraction": round(sum("TIMING_ESTIMATED" in s["flags"] for s in chunk) / len(chunk), 2),
                        "policy_risk": risk, "policy_terms": risk_terms})
    return out


def pick_non_overlapping(cands, limit, max_overlap=0.3):
    chosen = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if c["policy_risk"] == "high":
            continue
        ok = True
        for k in chosen:
            ov = min(c["end"], k["end"]) - max(c["start"], k["start"])
            if ov > max_overlap * min(c["dur"], k["dur"]):
                ok = False
                break
        if ok:
            chosen.append(c)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda c: c["start"])


def verify_candidate(model, media: Path, c: dict, pad: float = 2.0) -> dict:
    """Independent audio pass + frame check. Tightens start/end to the words actually heard."""
    w0, w1 = c["start"] - pad, c["end"] + pad
    heard = recheck(model, media, w0, w1)
    mine = [n for n in (norm(w) for w in c["text"].split()) if n]
    toks = [h["n"] for h in heard]
    sm = difflib.SequenceMatcher(None, mine, toks, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 2]
    hit = sum(b.size for b in blocks)
    v = {"audio_agreement": round(hit / max(len(mine), 1), 2),
         "heard_text": " ".join(h["n"] for h in heard), "verified": False,
         "heard_words": [(h["n"], h["start"], h["end"]) for h in heard]}
    if blocks:
        first, last = blocks[0], blocks[-1]
        v["start"] = round(heard[first.b]["start"] - 0.10, 2)
        v["end"] = round(heard[last.b + last.size - 1]["end"] + 0.30, 2)
        v["dur"] = round(v["end"] - v["start"], 1)
    # frames at 15% / 50% / 85%: non-black and not frozen
    lum = []
    for f in (0.15, 0.5, 0.85):
        t = (v.get("start", c["start"]) + f * (v.get("end", c["end"]) - v.get("start", c["start"])))
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(media), "-frames:v", "1",
                            "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"], capture_output=True)
        a = np.frombuffer(p.stdout, dtype=np.uint8)
        lum.append((float(a.mean()), float(a.std())) if a.size else (0.0, 0.0))
    v["frames_ok"] = all(m > 15 and s > 8 for m, s in lum)
    v["frame_luma"] = [(round(m), round(s)) for m, s in lum]
    return v


def confidence(c: dict, v: dict) -> float:
    timing = 1 - c["timing_estimated_fraction"]
    conf = (0.45 * v["audio_agreement"] + 0.20 * timing + 0.15 * (1 - c["flag_fraction"])
            + 0.10 * (1.0 if v["frames_ok"] else 0.0) + 0.10 * min(1.0, c["score"] * 2))
    return round(conf, 2)


def load_segments(tdir: Path, video_id: str) -> tuple[list[dict], str]:
    """Merged (Core1 text+speakers) transcript when it exists, otherwise the plain small.en transcript with neutral flags/speaker."""
    merged = tdir / f"{video_id}.merged.json"
    if merged.is_file():
        return json.loads(merged.read_text())["segments"], merged.name
    plain = tdir / f"{video_id}.json"
    if not plain.is_file():
        raise FileNotFoundError(f"no transcript for {video_id}; run `transcribe` first")
    segs = [{**s, "flags": [], "speaker": "unknown"} for s in json.loads(plain.read_text())["segments"]]
    return segs, plain.name


def analyze(video_id: str, model_factory=None, limit: int = 60, progress=print) -> dict:
    ref = source_library.resolve(video_id)
    tdir, media = ref.transcripts_dir, ref.media
    segs, tname = load_segments(tdir, video_id)
    out_path = ref.analysis_dir / f"{video_id}.candidates.json"
    if out_path.exists():
        raise FileExistsError(f"refusing to overwrite {out_path.name}")
    progress("measuring loudness")
    db = loudness_curve(media)
    pool = candidates_from_segments(segs, db)
    picked = pick_non_overlapping(pool, limit)
    progress(f"{len(pool)} windows -> {len(picked)} ranked; verifying against the video")
    if model_factory is None:
        from faster_whisper import WhisperModel
        model_factory = lambda: models.whisper("small.en")
    model = model_factory()
    for n, c in enumerate(picked, 1):
        v = verify_candidate(model, media, c)
        c["verification"] = v
        c["verified"] = bool(v["audio_agreement"] >= 0.6 and v["frames_ok"] and "start" in v and v["dur"] > 0)
        c["confidence"] = confidence(c, v)
        c["id"] = f"m{n:02d}"
        if n % 10 == 0:
            progress(f"  verified {n}/{len(picked)}")
    rec = {"video_id": video_id, "transcript": tname,
           "method": "heuristic score (hook, reaction, energy, density, completeness) minus flag/policy penalties; "
                     "verified by independent audio re-transcription + frame check",
           "candidates": picked}
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("x") as f:
        json.dump(rec, f, indent=2)
    return {"record": rec, "path": out_path}


def scene_cuts(media: Path, start: float, end: float, threshold: float = 0.35) -> list[float]:
    """Visual cuts inside [start, end]; clusters closer than 0.6 s count once (camera shake, flashes)."""
    p = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{start}", "-t", f"{end - start}", "-i", str(media), "-an",
                        "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], capture_output=True, text=True)
    raw = sorted(round(float(x) + start, 2) for x in re.findall(r"pts_time:([\d.]+)", p.stderr))
    cuts = []
    for c in raw:
        if c > start + 0.3 and (not cuts or c - cuts[-1] > 0.6):
            cuts.append(c)
    return cuts


PROFANITY = re.compile(r"\b(fuck\w*|shit\w*|bitch|ass|dumbass)\b", re.I)


def build_rankings(video_id: str, curation_path: Path, model_factory=None, progress=print) -> dict:
    """Verify hand-picked moments against the video; derive exact timestamps, flags and confidence."""
    ref = source_library.resolve(video_id)
    root = ref.analysis_dir
    out_path = root / f"{video_id}.rankings.json"
    if out_path.exists():
        raise FileExistsError(f"refusing to overwrite {out_path.name}")
    by_id = {s["id"]: s for s in load_segments(ref.transcripts_dir, video_id)[0]}
    media = ref.media
    cur = json.loads(Path(curation_path).read_text())
    if model_factory is None:
        from faster_whisper import WhisperModel
        model_factory = lambda: models.whisper("small.en")
    model = model_factory()
    db = loudness_curve(media)
    tiktoks = []
    for t in cur["tiktoks"]:
        moments = []
        for m in t["moments"]:
            segs = [by_id[i] for i in m["segments"]]
            text = " ".join(s["text"] for s in segs)
            flagged = [s for s in segs if s["flags"]]
            cand = {"start": segs[0]["start"], "end": segs[-1]["end"], "text": text,
                    "flag_fraction": len(flagged) / len(segs),
                    "timing_estimated_fraction": sum("TIMING_ESTIMATED" in s["flags"] for s in segs) / len(segs)}
            subset = candidates_from_segments(segs, db)
            full = [c for c in subset if c["segment_ids"] == m["segments"]]
            cand["score"] = full[0]["score"] if full else 0.5
            v = verify_candidate(model, media, cand, pad=m.get("pad", 2.0))  # wider window only where manually reviewed
            if "end_max" in m and "end" in v:  # manual trim, e.g. a source cut just after the last word
                v["end"] = min(v["end"], m["end_max"])
                v["dur"] = round(v["end"] - v["start"], 1)
            ok = bool(v["audio_agreement"] >= 0.6 and v["frames_ok"] and "start" in v)
            cuts = scene_cuts(media, v["start"], v["end"]) if "start" in v else []
            heard = " ".join(n for n, a, b in v["heard_words"]  # only the words inside the cut, not the padded window
                             if "start" in v and a >= v["start"] - 0.1 and b <= v["end"] + 0.1)
            risk, terms = policy_risk(text)
            moments.append({
                "rank": m["rank"], "label": m["label"], "verified": ok,
                "start": v.get("start"), "end": v.get("end"), "duration": v.get("dur"),
                "start_clock": f"{int(v['start']) // 60}:{v['start'] % 60:05.2f}" if "start" in v else None,
                "end_clock": f"{int(v['end']) // 60}:{v['end'] % 60:05.2f}" if "end" in v else None,
                "confidence": confidence(cand, v), "audio_agreement": v["audio_agreement"],
                "overlaps_transcript_flag": bool(flagged),
                "flagged_segments": [{"id": s["id"], "flags": s["flags"]} for s in flagged],
                "manual_review": m.get("review", "No flagged segments overlap this moment."),
                "segments": m["segments"], "merged_text": text, "audio_heard_text": heard,
                "policy_risk": risk, "policy_terms": terms,
                "profanity_count": len(PROFANITY.findall(heard)),
                "frames_ok": v["frames_ok"], "frame_luma": v["frame_luma"], "visual_cuts_inside": cuts,
                "visual_note": m.get("visual"),
                "speakers": sorted({s["speaker"] for s in segs})})
            progress(f"  {t['title'][:28]:28} #{m['rank']} {m['label'][:34]:34} "
                     f"{moments[-1]['start_clock']}-{moments[-1]['end_clock']} conf {moments[-1]['confidence']}")
        moments.sort(key=lambda x: -x["rank"])  # countdown order: #N first, #1 last
        tiktoks.append({"title": t["title"], "moments": moments,
                        "total_seconds": round(sum(x["duration"] or 0 for x in moments), 1),
                        "mean_confidence": round(sum(x["confidence"] for x in moments) / len(moments), 2)})
    rec = {"video_id": video_id, "curation": Path(curation_path).name, "tiktoks": tiktoks}
    with out_path.open("x") as f:
        json.dump(rec, f, indent=2)
    return {"record": rec, "path": out_path}


def write_report(video_id: str) -> Path:
    """Human-readable summary generated from rankings.json + candidates.json (never hand-edited numbers)."""
    root = source_library.resolve(video_id, require_active=False).analysis_dir
    rk = json.loads((root / f"{video_id}.rankings.json").read_text())
    pool = json.loads((root / f"{video_id}.candidates.json").read_text())["candidates"]
    path = root / f"{video_id}.analysis.md"
    moments = [m for t in rk["tiktoks"] for m in t["moments"]]
    L = [f"# Clip analysis: {video_id}", "",
         f"- Pool: {len(pool)} ranked non-overlapping moments, {sum(c['verified'] for c in pool)} verified against the video.",
         f"- Curated: {len(rk['tiktoks'])} ranking TikToks, {len(moments)} moments, {sum(m['verified'] for m in moments)} verified, "
         f"{sum(m['overlaps_transcript_flag'] for m in moments)} overlap a transcript flag (each reviewed by hand, see notes).", "",
         "## Read this first",
         "- **This video is a compilation of different streamers' clips**, not one stream. Each moment comes from a different source, "
         "so the on-screen people and overlays differ per clip. The only rights record is your confirmation in chat; rights for "
         "third-party footage inside the compilation have not been checked.",
         "- **Burned-in captions and Twitch chat overlays are already on the video** (and some words are already starred out). "
         "Any new caption will collide with them, so rendering needs a crop or a different caption area.",
         "- **Core1's wording is often wrong** (e.g. 'Kimmy lays off stream', 'allowed', 'Poseidon', '$3.60 deal'). Captions should use the "
         "audio wording in `audio_heard_text`, not Core1's text.",
         "- **Verification = independent audio re-transcription (small.en, no voice filter) + frame checks + scene-cut detection.** "
         "I read the frames and text; nobody has listened to the audio. Treat the numbers as discovery aids.",
         "- **Language:** nearly every moment has profanity, and the stream contains sexual content that I excluded. "
         "TikTok may limit reach or flag mature language even where nothing is removed.", "",
         "## Confidence",
         "0.45 audio agreement + 0.20 non-estimated timing + 0.15 not-flagged share + 0.10 frames OK + 0.10 heuristic score. "
         "0.9+ = strong, 0.7-0.9 = good, below 0.7 = verify by ear before use.", ""]
    for n, t in enumerate(rk["tiktoks"], 1):
        L += [f"## TikTok {n}: {t['title']}",
              f"{len(t['moments'])} moments, {t['total_seconds']} s of footage, mean confidence {t['mean_confidence']}. Countdown order (last = #1).", "",
              "| Rank | Moment | Verified start - end | Dur | Conf | Transcript flag overlap | Visual cuts inside | Language |",
              "|---|---|---|---|---|---|---|---|"]
        for m in t["moments"]:
            fl = ("Yes: seg " + ", ".join(str(f["id"]) for f in m["flagged_segments"])) if m["overlaps_transcript_flag"] else "No"
            cuts = ", ".join(f"{c:.1f}s" for c in m["visual_cuts_inside"]) or "none"
            L.append(f"| #{m['rank']} | {m['label']} | {m['start_clock']} - {m['end_clock']} ({m['start']}-{m['end']} s) | {m['duration']} s "
                     f"| {m['confidence']} | {fl} | {cuts} | {m['profanity_count']} profane, policy {m['policy_risk']} |")
        L.append("")
        for m in t["moments"]:
            L += [f"**#{m['rank']} {m['label']}**", f"- Review: {m['manual_review']}"]
            if m.get("visual_note"):
                L.append(f"- Visual: {m['visual_note']}")
            L += [f"- Audio wording: \"{m['audio_heard_text']}\"", ""]
    bad = [c for c in pool if not c["verified"]]
    L += ["## Excluded", "",
          "**Could not be verified against the audio** (pool candidates, agreement below 0.6): "
          + ", ".join(f"{c['id']} {int(c['start']) // 60}:{int(c['start']) % 60:02d} (agreement {c['verification']['audio_agreement']})" for c in bad) + ".", "",
          "**Excluded for content/policy by hand:** the opening story and everything sexual or explicit (about 0:00-0:45, 8:42-8:55 "
          "'how big' exchange, 14:44-15:50 roleplay, 24:28-24:50 'sex videos' bit, 27:08-27:20 innuendo); the 'little boys' misquote joke at ~13:45-14:08 "
          "(sexualises minors in wording, so high takedown risk); the 'disabled kid' line at ~26:11 and the wheelchair-mocking rant at 14:46-15:05.", "",
          "**Excluded for no clean payoff or unclear audio:** the bomb sequence (19:55-20:10, audio agreement 0.23), 'Switch me... click play' (video payoff has no speech), "
          "and the burgers/hot dogs line (no context).", "",
          "**Not verified by ear:** speaker labels. They are carried over from Core1 and were not used for selection.", ""]
    with path.open("x") as f:
        f.write("\n".join(L) + "\n")
    return path
