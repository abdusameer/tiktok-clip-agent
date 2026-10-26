"""Merge Core1 text + speaker labels with small.en word timing.

Core1 timestamps drift against the real video (about 1.6%, ~26 s by the end), so lines are
aligned to small.en by *text*, never by Core1's clock. Both source transcripts are read-only.
Everything here reuses the cached small.en model; nothing is downloaded.
"""
import bisect
import difflib
import json
import re
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import config, source_library
from .transcribe import clock, srt_time

HDR = re.compile(r"(\d\d):(\d\d):(\d\d) \[(.+)\]$")
TERMINAL = (".", "?", "!", "...")
TOLERANCE = 5.0  # seconds a text match may sit from the predicted position
RECHECK_PAD = 4.0


class MergeError(Exception):
    pass


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def parse_core1(path: Path) -> list[dict]:
    lines = Path(path).read_text().splitlines()
    rows, i = [], 0
    while i < len(lines):
        m = HDR.match(lines[i].strip())
        if m and i + 1 < len(lines):
            text = lines[i + 1].strip()
            tokens = [n for n in (norm(w) for w in text.split()) if n]
            rows.append({"t": int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]), "speaker": m[4], "text": text,
                         "tokens": tokens, "norm": " ".join(tokens), "flags": []})
            i += 2
        else:
            i += 1
    return rows


def small_words(small: dict) -> list[dict]:
    out = []
    for seg in small["segments"]:
        for w in seg["words"]:
            n = norm(w["word"])
            if n:
                out.append({"n": n, "start": w["start"], "end": w["end"], "src": "small.en"})
    return out


# ---------- alignment ----------

def align(rows: list[dict], words: list[dict]):
    """Match Core1 tokens to timed words. Returns (per-row matches, offset function)."""
    ct, owner = [], []
    for k, r in enumerate(rows):
        for tok in r["tokens"]:
            ct.append(tok)
            owner.append(k)
    sm = difflib.SequenceMatcher(None, ct, [w["n"] for w in words], autojunk=False)
    blocks = sm.get_matching_blocks()
    anchors = sorted((rows[owner[b.a]]["t"], words[b.b]["start"] - rows[owner[b.a]]["t"])
                     for b in blocks if b.size >= 4)
    if not anchors:
        raise MergeError("no reliable text anchors between the transcripts")
    a_t = [a[0] for a in anchors]

    def offset(t: float) -> float:
        i = bisect.bisect_left(a_t, t)
        near = sorted(anchors[max(0, i - 8): i + 8], key=lambda a: abs(a[0] - t))[:7]
        return statistics.median(o for _, o in near)

    per_row = defaultdict(list)  # row -> [(word idx)]
    for b in blocks:
        if b.size < 2:
            continue
        for d in range(b.size):
            r = rows[owner[b.a + d]]
            w = words[b.b + d]
            if abs(w["start"] - (r["t"] + offset(r["t"]))) <= TOLERANCE:
                per_row[owner[b.a + d]].append(b.b + d)
    return per_row, offset


def time_rows(rows, words, per_row, offset):
    """Set start/end/match per row from matched words; estimate when unmatched."""
    for k, r in enumerate(rows):
        idx = per_row.get(k, [])
        n = max(len(r["tokens"]), 1)
        r["match"] = round(len(idx) / n, 2)
        if idx:
            first, last = words[min(idx)], words[max(idx)]
            r["start"], r["end"] = first["start"], last["end"]
            r["src"] = "recheck" if any(words[i]["src"] == "recheck" for i in idx) else "small.en"
        else:
            r["start"] = r["t"] + offset(r["t"])
            r["end"] = r["start"] + max(0.4, 0.35 * n)
            r["src"] = "estimated"
            r["flags"].append("TIMING_ESTIMATED")
        if idx and r["match"] < 0.6:
            r["flags"].append("PARTIAL_TEXT_MATCH")
    for k, r in enumerate(rows):  # bound estimated lines by their matched neighbours
        if r["src"] != "estimated":
            continue
        lo = rows[k - 1]["end"] if k else 0.0
        nxt = next((x for x in rows[k + 1:] if x["src"] != "estimated"), None)
        hi = nxt["start"] if nxt else float("inf")
        dur = min(r["end"] - r["start"], max(0.2, hi - lo))
        r["start"] = min(max(r["start"], lo), max(lo, hi - dur))
        r["end"] = r["start"] + dur
    prev_end = 0.0
    for r in rows:  # keep the timeline monotonic
        r["start"] = round(max(r["start"], prev_end - 0.05), 3)
        r["end"] = round(max(r["end"], r["start"] + 0.2), 3)
        prev_end = r["end"]


# ---------- loop detection / audio verification ----------

def find_runs(rows: list[dict]) -> list[tuple[int, int]]:
    """Index ranges around seconds with an implausible number of Core1 lines."""
    by = defaultdict(list)
    for k, r in enumerate(rows):
        by[r["t"]].append(k)
    spans = []
    for ks in by.values():
        mean_tokens = sum(len(rows[k]["tokens"]) for k in ks) / len(ks)
        # 5 one-word "no"s in a second is real; 6+ lines, or 4+ multi-word lines, is not
        if len(ks) < 6 and not (len(ks) >= 4 and mean_tokens >= 2):
            continue
        lo, hi = ks[0], ks[-1]
        phrases = {rows[k]["norm"] for k in ks}
        while lo > 0 and rows[lo - 1]["norm"] in phrases and rows[lo - 1]["t"] >= rows[lo]["t"] - 3:
            lo -= 1
        while hi < len(rows) - 1 and rows[hi + 1]["norm"] in phrases and rows[hi + 1]["t"] <= rows[hi]["t"] + 3:
            hi += 1
        spans.append((lo, hi))
    spans.sort()
    merged = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def window_audio(media: Path, start: float, end: float):
    start = max(0.0, start)
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(end - start), "-i", str(media),
                        "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True)
    if p.returncode != 0:
        raise MergeError(p.stderr.decode(errors="replace").strip())
    return np.frombuffer(p.stdout, dtype=np.float32)


def recheck(model, media: Path, start: float, end: float) -> list[dict]:
    """Independent pass over a short window: no voice filter, no carried-over context."""
    start = max(0.0, start)
    segs, _ = model.transcribe(window_audio(media, start, end), vad_filter=False,
                               condition_on_previous_text=False, word_timestamps=True, beam_size=5)
    out = []
    for s in segs:
        for w in s.words or []:
            n = norm(w.word)
            if n:
                out.append({"n": n, "start": round(start + w.start, 3), "end": round(start + w.end, 3),
                            "src": "recheck"})
    return out


def count_phrase(tokens: list[str], phrase: list[str], min_ratio: float = 0.6) -> int:
    """Non-overlapping fuzzy occurrences of `phrase` in `tokens` (ASR and Core1 word the same speech differently)."""
    n, i, c = len(phrase), 0, 0
    while phrase and i + n <= len(tokens):
        sm = difflib.SequenceMatcher(None, phrase, tokens[i:i + n], autojunk=False)
        if tokens[i:i + n] == phrase or (n >= 3 and sm.ratio() >= min_ratio):
            c += 1
            i += n
        else:
            i += 1
    return c


def unsupported_stretches(rows, per_row, min_rows=3):
    """Runs of >=min_rows consecutive rows with no text match to small.en at all."""
    out, cur = [], []
    for k in range(len(rows)):
        if k not in per_row or not per_row[k]:
            cur.append(k)
        else:
            if len(cur) >= min_rows:
                out.append((cur[0], cur[-1]))
            cur = []
    if len(cur) >= min_rows:
        out.append((cur[0], cur[-1]))
    return out


def verify_runs(model, media, rows, runs, offset):
    """Decide per run which Core1 lines the audio actually supports. Returns (drop set, evidence)."""
    drop, evidence = set(), []
    for lo, hi in runs:
        t0 = rows[lo]["t"] + offset(rows[lo]["t"])
        t1 = rows[hi]["t"] + offset(rows[hi]["t"])
        w0, w1 = t0 - 8, t1 + 8  # Core1 placement is only good to a few seconds
        words = recheck(model, media, w0, w1)
        toks = [w["n"] for w in words]
        phrases = defaultdict(list)
        for k in range(lo, hi + 1):
            phrases[rows[k]["norm"]].append(k)
        detail = {}
        for p, ks in phrases.items():
            audio_n = count_phrase(toks, p.split())
            covered = sum(1 for k, x in enumerate(rows) if not lo <= k <= hi and x["norm"] == p
                          and rows[lo]["t"] - 8 <= x["t"] <= rows[hi]["t"] + 8)  # already present outside the burst
            keep = ks[:min(len(ks), max(0, audio_n - covered))]
            for k in ks[len(keep):]:
                drop.add(k)
            for k in keep:
                rows[k]["flags"].append("REPEAT_PARTIALLY_VERIFIED")
            detail[p] = {"core1_lines": len(ks), "heard_in_audio": audio_n, "already_covered": covered, "kept": len(keep)}
        evidence.append({"core1_time": clock(rows[lo]["t"]), "video_time": clock(t0), "lines": hi - lo + 1,
                         "window": [round(w0, 1), round(w1, 1)], "phrases": detail,
                         "audio_text": " ".join(w["n"] for w in words)})
    return drop, evidence


# ---------- speakers ----------

def assign_speakers(rows):
    """Carry Core1 labels, but use UNKNOWN where the label is shaky. Rules are listed in the QA report."""
    for k, r in enumerate(rows):
        r["core1_speaker"] = r["speaker"]
        prev = rows[k - 1] if k else None
        why = None
        if "REPEAT_PARTIALLY_VERIFIED" in r["flags"]:
            why = "inside a verified repeat run"
        elif prev and prev["speaker"] != r["speaker"] and not prev["text"].endswith(TERMINAL):
            why = "label changes mid-sentence"
        elif r["speaker"] != "Speaker 1" and len(r["tokens"]) <= 2 and \
                (not prev or prev["speaker"] != r["speaker"]) and \
                (k + 1 >= len(rows) or rows[k + 1]["speaker"] != r["speaker"]):
            why = "isolated one/two-word line from a minor speaker"
        if why:
            r["speaker"] = "UNKNOWN"
            r["flags"].append("SPEAKER_UNCERTAIN")
            r["speaker_note"] = why


# ---------- segments / outputs ----------

def build_segments(rows, words):
    segs, cur = [], []

    def close():
        if not cur:
            return
        text = " ".join(r["text"] for r in cur)
        flags = sorted({f for r in cur for f in r["flags"]})
        seg = {"id": len(segs), "start": cur[0]["start"], "end": cur[-1]["end"], "speaker": cur[0]["speaker"],
               "text": text, "flags": flags, "timing_source": "/".join(sorted({r["src"] for r in cur})),
               "match": round(statistics.mean(r["match"] for r in cur), 2), "core1_time": clock(cur[0]["t"])}
        if "PARTIAL_TEXT_MATCH" in flags or "TIMING_ESTIMATED" in flags:
            seg["small_en_text"] = " ".join(w["n"] for w in words
                                            if w["src"] == "small.en" and seg["start"] - 0.5 <= w["start"] <= seg["end"] + 0.5)
        segs.append(seg)

    for r in rows:
        if cur:
            gap = r["start"] - cur[-1]["end"]
            dur = r["end"] - cur[0]["start"]
            words_n = sum(len(x["tokens"]) for x in cur)
            if r["speaker"] != cur[-1]["speaker"] or gap > 1.0 or dur > 7.0 or \
                    (cur[-1]["text"].endswith(TERMINAL) and words_n >= 8):
                close()
                cur.clear()
        cur.append(r)
    close()
    return segs


def to_srt(segs):
    out = []
    for i, s in enumerate(segs, 1):
        end = s["end"]
        if i < len(segs):
            end = min(end, segs[i]["start"] - 0.02)
        end = max(end, s["start"] + 0.3)
        out.append(f"{i}\n{srt_time(s['start'])} --> {srt_time(end)}\n{s['text']}\n")
    return "\n".join(out)


def to_txt(segs):
    out = []
    for s in segs:
        mark = "  (!)" if s["flags"] else ""
        out.append(f"[{clock(s['start'])}] {s['speaker']}: {s['text']}{mark}")
    return "\n".join(out) + "\n"


def sample_check(model, media, segs, spans):
    """Re-transcribe short windows of the real video and compare with the merged text."""
    results = []
    for a, b in spans:
        words = recheck(model, media, a, b)
        heard = [w["n"] for w in words]
        mine_segs = [s for s in segs if s["start"] >= a and s["end"] <= b]
        mine = [n for s in mine_segs for n in (norm(w) for w in s["text"].split()) if n]
        sm = difflib.SequenceMatcher(None, mine, heard, autojunk=False)
        hit = sum(x.size for x in sm.get_matching_blocks())
        results.append({"window": [a, b], "merged_tokens": len(mine), "audio_tokens": len(heard),
                        "merged_found_in_audio": round(hit / max(len(mine), 1), 2),
                        "audio_found_in_merged": round(hit / max(len(heard), 1), 2),
                        "segments": len(mine_segs), "audio_text": " ".join(heard)[:400],
                        "merged_text": " ".join(mine)[:400]})
    return results


def merge(video_id: str, core1_path: Path, model_factory=None, progress=print) -> dict:
    ref = source_library.resolve(video_id)
    meta, media, tdir = ref.meta, ref.media, ref.transcripts_dir
    small_path = tdir / f"{video_id}.json"
    core1_path = Path(core1_path)
    paths = {e: tdir / f"{video_id}.merged.{e}" for e in ("json", "srt", "txt")}
    paths["qa"] = tdir / f"{video_id}.merged.qa.md"
    if any(p.exists() for p in paths.values()):
        raise MergeError("refusing to overwrite existing merged outputs")

    small = json.loads(small_path.read_text())
    rows = parse_core1(core1_path)
    words = small_words(small)
    n_in = len(rows)
    if model_factory is None:
        from faster_whisper import WhisperModel
        model_factory = lambda: WhisperModel("small.en", device="cpu", compute_type="int8")
    model = model_factory()

    progress("first alignment")
    per_row, offset = align(rows, words)
    drift = {m: round(offset(m * 60), 1) for m in (0, 5, 10, 15, 20, 25) if m * 60 <= rows[-1]["t"]}

    runs = find_runs(rows)
    progress(f"verifying {len(runs)} repeat runs against the audio")
    drop, loop_evidence = verify_runs(model, media, rows, runs, offset)
    removed = [{"core1_time": clock(rows[k]["t"]), "speaker": rows[k]["speaker"], "text": rows[k]["text"],
                "reason": "repeat not supported by audio re-check"} for k in sorted(drop)]

    keep_idx = [k for k in range(len(rows)) if k not in drop]
    rows = [rows[k] for k in keep_idx]
    per_row, offset = align(rows, words)

    stretches = unsupported_stretches(rows, per_row)
    progress(f"re-checking {len(stretches)} stretches small.en found no speech for")
    extra, stretch_info = [], []
    for lo, hi in stretches:
        t0 = rows[lo]["t"] + offset(rows[lo]["t"]) - RECHECK_PAD
        t1 = rows[hi]["t"] + offset(rows[hi]["t"]) + RECHECK_PAD + 2
        new = [w for w in recheck(model, media, t0, t1)
               if not any(o["start"] - 0.3 < w["end"] and w["start"] < o["end"] + 0.3
                          for o in words if abs(o["start"] - w["start"]) < 3)]
        extra += new
        stretch_info.append({"core1_time": clock(rows[lo]["t"]), "lines": hi - lo + 1, "recovered_words": len(new)})
    words = sorted(words + extra, key=lambda w: w["start"])
    per_row, offset = align(rows, words)
    time_rows(rows, words, per_row, offset)
    assign_speakers(rows)
    segs = build_segments(rows, words)

    mid = segs[len(segs) // 2]["start"]
    last = segs[-1]["end"]
    spans = [(0, 30), (mid - 15, mid + 15), (last - 30, last)]
    progress("sampling beginning / middle / end against the video")
    samples = sample_check(model, media, segs, spans)

    flagged = [s for s in segs if s["flags"]]
    record = {
        "video_id": video_id,
        "sources": {"core1": {"file": core1_path.name, "lines": n_in},
                    "small_en": {"file": small_path.name, "segments": len(small["segments"])}},
        "core1_clock_drift_s": drift,
        "removed_lines": removed, "loop_verification": loop_evidence, "rechecked_stretches": stretch_info,
        "sample_check": samples,
        "stats": {"segments": len(segs), "flagged_segments": len(flagged),
                  "unknown_speaker_segments": sum(s["speaker"] == "UNKNOWN" for s in segs),
                  "timing_estimated_segments": sum("TIMING_ESTIMATED" in s["flags"] for s in segs)},
        "segments": segs,
    }
    with paths["json"].open("x") as f:
        json.dump(record, f, indent=2)
    with paths["srt"].open("x") as f:
        f.write(to_srt(segs))
    with paths["txt"].open("x") as f:
        f.write(to_txt(segs))
    with paths["qa"].open("x") as f:
        f.write(qa_report(record))
    return {"record": record, "paths": paths}


def qa_report(rec: dict) -> str:
    st = rec["stats"]
    L = [f"# QA report: merged transcript for {rec['video_id']}", "",
         "Merges Core1's text and speaker labels with small.en word timing. Neither source file was modified.", "",
         "## Summary",
         f"- Core1 lines in: {rec['sources']['core1']['lines']}; removed as unsupported repeats: {len(rec['removed_lines'])}",
         f"- Merged segments: {st['segments']}; flagged: {st['flagged_segments']} "
         f"(timing estimated: {st['timing_estimated_segments']}); speaker UNKNOWN: {st['unknown_speaker_segments']}", "",
         "## Core1 clock drift",
         "Core1 timestamps run behind the real video. Offset to add (seconds) at each minute mark: "
         + ", ".join(f"{m} min: +{v}" for m, v in rec["core1_clock_drift_s"].items()) +
         ". Core1 times were never used directly; lines were placed by matching their text to small.en words.", "",
         "## Repeat/loop verification",
         "Suspect = 6+ Core1 lines in one second, or 4+ lines averaging 2+ words (rapid one-word repeats such as 'no no no no no' are plausible and were left alone). Each run was re-transcribed from the video "
         "audio (small.en, voice filter off). A phrase is kept only as many times as it is heard, minus the times it already appears just outside the burst.", "",
         "| Core1 time | ~Video time | Lines | Phrase: Core1 lines / heard in audio / already covered / kept |", "|---|---|---|---|"]
    for e in rec["loop_verification"]:
        ph = "; ".join(f"'{p}' {v['core1_lines']}/{v['heard_in_audio']}/{v['already_covered']}/{v['kept']}" for p, v in e["phrases"].items())
        L.append(f"| {e['core1_time']} | {e['video_time']} | {e['lines']} | {ph} |")
    L += ["", "Kept lines from these runs are flagged and their speaker is UNKNOWN. Removed lines are listed in the JSON "
          "(`removed_lines`).", "", "## Stretches small.en had no speech for",
          "Core1 had text but small.en's voice filter found none. These were re-checked with the filter off; "
          "words were added only where they did not overlap existing small.en words."]
    for s in rec["rechecked_stretches"]:
        L.append(f"- Core1 {s['core1_time']}: {s['lines']} lines, {s['recovered_words']} words recovered")
    L += ["", "## Beginning / middle / end spot checks (against the video's audio)",
          "| Window (s) | Merged words heard in audio | Audio words found in merged |", "|---|---|---|"]
    for s in rec["sample_check"]:
        L.append(f"| {s['window'][0]:.0f}-{s['window'][1]:.0f} | {s['merged_found_in_audio']:.0%} "
                 f"| {s['audio_found_in_merged']:.0%} |")
    L += ["", "## Speaker rule",
          "Core1 labels are carried over unverified (no diarization model is available here). A segment is set to UNKNOWN when "
          "its line sits inside a verified repeat run, the label changes mid-sentence, or it is an isolated one/two-word line "
          "from a minor speaker.", "",
          "## Flags", "- `TIMING_ESTIMATED`: no word of the line matched anything heard in the audio (small.en or re-check), so the text is unconfirmed and its time is placed between matched neighbours using the drift curve.",
          "- `PARTIAL_TEXT_MATCH`: under 60% of the words matched small.en; `small_en_text` shows what small.en heard.",
          "- `REPEAT_PARTIALLY_VERIFIED`: line is part of a repeat run, kept only as often as heard.",
          "- `SPEAKER_UNCERTAIN`: speaker set to UNKNOWN.", "",
          "## Limits", "- Checks re-transcribe the audio with the same small.en model, so they confirm agreement, not ground truth.",
          "- Nobody listened to the clip; wording and speaker labels still need a human pass before captions go out."]
    return "\n".join(L) + "\n"
