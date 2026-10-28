"""Caption wording rule, enforced in code: a caption word is only allowed when TWO independent decodes of the moment agree on it.

When wording cannot be confirmed, omit it (or move the clip boundary); never guess. A spec carries its evidence per moment as
`decodes: [beam1_words, beam5_words]` (lists of [word, start, end]). Specs built from a supplied/verified transcript may declare
`"wording_verified_by": "supplied_transcript"` instead.
"""
import difflib
import re
from pathlib import Path

WINDOW = 0.7          # seconds of tolerance between a caption line and the decoded word's time


class CaptionError(Exception):
    pass


def tokens(text: str) -> list[str]:
    out = []
    for w in (text or "").lower().split():
        w = re.sub(r"[^a-z0-9']", "", w.replace("’", "'")).strip("'")
        if w:
            out.append(w)
    return out


def agreed_words(dec_a: list, dec_b: list) -> list[dict]:
    """Words both decodes heard (sequence-aligned, case/punctuation-insensitive). Timing comes from the second decode."""
    a = [tokens(w[0]) for w in dec_a]
    b = [tokens(w[0]) for w in dec_b]
    fa = [(t[0] if t else "") for t in a]
    fb = [(t[0] if t else "") for t in b]
    sm = difflib.SequenceMatcher(None, fa, fb, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                if fb[j]:
                    out.append({"w": fb[j], "start": dec_b[j][1], "end": dec_b[j][2]})
    return out


def problems(spec: dict) -> list[str]:
    """Every caption word must be confirmed by both decodes near its time; returns the list of violations (empty = enforceable pass)."""
    if spec.get("wording_verified_by") == "supplied_transcript":
        return []
    out = []
    for rank, m in (spec.get("moments") or {}).items():
        if not (m.get("lines") or []):                     # nothing is captioned, so nothing needs confirming (the audio still plays)
            continue
        dec = m.get("decodes")
        if not dec or len(dec) != 2 or not dec[0] or not dec[1]:
            out.append(f"moment {rank}: caption wording has no two-decode evidence (add `decodes`: two independent decodes of this moment)")
            continue
        agreed = agreed_words(dec[0], dec[1])
        for i, ln in enumerate(m.get("lines") or []):
            first, last = m["words"][ln["first"]], m["words"][ln["last"]]
            a, b = first[1] - WINDOW, last[2] + WINDOW
            pool = [x for x in agreed if x["end"] >= a and x["start"] <= b]
            for tok in tokens(ln["text"]):
                hit = next((x for x in pool if x["w"] == tok), None)
                if hit is None:
                    out.append(f"moment {rank} line {i} {ln['text']!r}: word {tok!r} is not confirmed by both decodes; omit it or adjust the boundary")
                else:
                    pool.remove(hit)
    return out


def assert_agreed(spec: dict) -> None:
    p = problems(spec)
    if p:
        raise CaptionError("; ".join(p[:6]) + (f" (+{len(p) - 6} more)" if len(p) > 6 else ""))


def suggest_lines(dec_a: list, dec_b: list, max_words: int = 8, gap: float = 0.6) -> list[list]:
    """Caption lines built ONLY from agreed words, split at pauses/length. Returns [[text, start, end], ...]."""
    words = agreed_words(dec_a, dec_b)
    lines, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > gap or len(cur) >= max_words):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        text = " ".join(x["w"] for x in ln)
        out.append([text[:1].upper() + text[1:], ln[0]["start"], ln[-1]["end"]])
    return out


def decode_moment(media: Path, start: float, end: float, model_name: str = "medium.en", pad: float = 1.0) -> dict:
    """Two independent decodes (beam 1 and beam 5) of [start, end], with word timestamps in source time."""
    import subprocess

    import numpy as np
    from . import models
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(start - pad, 0)}", "-to", f"{end + pad}", "-i", str(media), "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    model = models.whisper(model_name)
    res = []
    for beam in (1, 5):
        segs, _ = model.transcribe(audio, beam_size=beam, word_timestamps=True, language="en", condition_on_previous_text=False)
        res.append([[w.word.strip(), round(w.start + max(start - pad, 0), 2), round(w.end + max(start - pad, 0), 2)] for s in segs for w in s.words])
    return {"model": model_name, "decodes": res, "suggested_lines": suggest_lines(res[0], res[1])}
