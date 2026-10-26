"""Final text-safety scan.

Blocks SEVERE language only: racial/ethnic/homophobic/transphobic/ableist slurs and hateful or discriminatory attacks. Ordinary
profanity (fuck, shit, bitch, ass, ...) is never blocked - it stays uncensored. Text is normalized first (case, accents, zero-width
characters, homoglyphs, common character substitutions, separators and stretched letters) so obvious obfuscations are caught.

Scanned: generated titles, captions, cover titles, suggested TikTok captions and hashtags, and the OCR-readable text of the WHOLE visible
frame (chat, overlays, signs) sampled throughout each selected moment. Chat too small to read in the final 1080x1920 output is recorded
as unverified - it is never counted as verified clean.
"""
import json
import re
import subprocess
import unicodedata
from pathlib import Path

from . import config, source_library, style

MIN_READABLE_PX = 14                    # smaller text height (px in the final 1080-wide output) cannot be read by a viewer
LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "6": "g", "8": "b", "9": "g", "@": "a", "$": "s", "!": "i", "|": "i", "+": "t",
        "€": "e", "£": "l", "¡": "i"}
HOMOGLYPH = {"а": "a", "е": "e", "і": "i", "ї": "i", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "ѕ": "s", "ј": "j", "ԁ": "d",
             "ɡ": "g", "ɑ": "a", "ο": "o", "ν": "v", "ı": "i", "ӏ": "l"}
SEP = r"[\W_]{0,3}"


class TextSafetyError(Exception):
    pass


def normalize(text: str) -> str:
    """Casefold, strip accents/zero-width/format characters, fold homoglyphs and character substitutions."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if unicodedata.category(c) not in ("Mn", "Cf", "Cc") or c in "\n\t ")
    t = t.casefold()
    t = "".join(HOMOGLYPH.get(c, c) for c in t)
    t = "".join(LEET.get(c, c) for c in t)
    return re.sub(r"\s+", " ", t)


def _w(*parts: str) -> str:
    """Letters/groups joined by optional separators, each letter allowed to repeat (stretched spelling)."""
    return SEP.join(parts)


def _letters(word: str) -> str:
    return SEP.join(f"{c}+" for c in word)


def _term(body: str) -> "re.Pattern":
    return re.compile(rf"(?<![a-z]){body}(?![a-z])")


SEVERE = [
    ("racial_slur", "n-word", _term(_w(_letters("nigg"), r"(?:e+" + SEP + r"r+|a+|u+h+|a+h+)") + r"s*")),
    ("racial_slur", "n-word", _term(_w(_letters("nibb"), r"(?:e+" + SEP + r"r+|a+)") + r"s*")),
    ("racial_slur", "n-word", _term(_w(_letters("nig"), r"(?:e+" + SEP + r"r+)") + r"s*")),
    ("antisemitic_slur", "kike", _term(_letters("kike") + r"s*")),
    ("racial_slur", "chink", _term(_letters("chink") + r"s*")),
    ("racial_slur", "gook", _term(_letters("gook") + r"s*")),
    ("racial_slur", "spic", _term(_letters("spic") + r"k?s*")),
    ("racial_slur", "wetback", _term(_letters("wetback") + r"s*")),
    ("racial_slur", "beaner", _term(_letters("beaner") + r"s*")),
    ("racial_slur", "coon", _term(_letters("coon") + r"s*")),
    ("racial_slur", "towelhead", _term(_letters("towelhead") + r"s*")),
    ("racial_slur", "raghead", _term(_letters("raghead") + r"s*")),
    ("racial_slur", "paki", _term(_letters("paki") + r"s*")),
    ("homophobic_slur", "f-slur", _term(_w(_letters("fag"), r"(?:g+" + SEP + r"o+" + SEP + r"t+|g+" + SEP + r"y+)?") + r"s*")),
    ("homophobic_slur", "dyke", _term(_letters("dyke") + r"s*")),
    ("transphobic_slur", "tranny", _term(_letters("tranny") + r"s*")),
    ("ableist_slur", "retard", _term(_letters("retard") + r"(?:e+d+|s)?")),
    ("ableist_slur", "mongoloid", _term(_letters("mongoloid") + r"s*")),
]
GROUPS = r"(?:jews?|blacks?|negroes|gays?|lesbians?|muslims?|mexicans?|asians?|arabs?|immigrants?|trans(?:gender)?s?|women|disabled)"
HATE_PHRASES = [
    ("hate_phrase", "violence against a group", re.compile(rf"\b(?:kill|gas|hang|lynch|exterminate|eliminate|shoot)\s+(?:all\s+)?(?:the\s+)?{GROUPS}\b")),
    ("hate_phrase", "dehumanizing a group", re.compile(rf"\b{GROUPS}\s+(?:are|is)\s+(?:all\s+)?(?:animals?|vermin|subhuman|filth|trash|a disease|cancer|rats|apes|monkeys|inferior)\b")),
    ("hate_phrase", "go back to", re.compile(r"\bgo back to (?:africa|your country|mexico|china|where you came from)\b")),
    ("hate_phrase", "white power", re.compile(r"\bwhite (?:power|pride|supremacy)\b")),
    ("hate_phrase", "nazi slogan", re.compile(r"\b(?:heil hitler|sieg heil|hitler was right)\b")),
]


def scan_text(text: str) -> list[dict]:
    """Severe findings in one string (ordinary profanity is ignored)."""
    norm = normalize(text)
    out = []
    for cat, term, rx in SEVERE:
        for m in rx.finditer(norm):
            out.append({"category": cat, "term": term, "match": m.group(0)})
    for cat, term, rx in HATE_PHRASES:
        for m in rx.finditer(re.sub(r"[^a-z0-9 ]+", " ", norm)):
            out.append({"category": cat, "term": term, "match": m.group(0)})
    return out


def scan_fields(fields: dict[str, str]) -> list[dict]:
    out = []
    for name, text in fields.items():
        for f in scan_text(text):
            out.append({"field": name, **f})
    return out


def generated_text(meta: dict | None = None, spec: dict | None = None, labels: list[dict] | None = None) -> dict[str, str]:
    """Every generated string that can reach a viewer: titles, caption lines, cover title, TikTok caption, hashtags."""
    f: dict[str, str] = {}
    if meta:
        for k in ("video_title", "cover_title", "caption"):
            if meta.get(k):
                f[k] = meta[k]
        for i, h in enumerate(meta.get("hashtags") or []):
            f[f"hashtag[{i}]"] = h
        for m in meta.get("moments") or []:
            f[f"moment_title[{m.get('rank')}]"] = m["title"]
    if spec:
        if spec.get("title"):
            f["spec_title"] = spec["title"]
        if spec.get("headline"):
            f["headline"] = spec["headline"]
        for r, m in (spec.get("moments") or {}).items():
            if m.get("label"):
                f[f"label[{r}]"] = m["label"]
            for i, ln in enumerate(m.get("lines") or []):
                f[f"caption[{r}.{i}]"] = ln["text"]
    for e in labels or []:
        f[f"label[{e.get('rank')}]"] = e["title"]
    return f


def assert_clean(fields: dict[str, str]) -> None:
    found = scan_fields(fields)
    if found:
        raise TextSafetyError("severe language in generated text: " + "; ".join(f"{x['field']}: {x['category']} ({x['term']})" for x in found))


# ------------------------------------------------------------------ on-screen text (whole frame, sampled through the moment)
def ocr_tool(root: Path | None = None) -> Path | None:
    from . import fullanalysis
    swiftc = fullanalysis.dependencies()["swiftc"]
    if not swiftc:
        return None
    root = Path(root) if root else config.project_root()
    exe = root / source_library.LIB / "_tools" / "ocr2"
    if not exe.is_file():
        exe.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([swiftc, "-O", str(Path(__file__).with_name("_ocr.swift")), "-o", str(exe)], capture_output=True, text=True)
        if r.returncode:
            return None
    return exe


def _tiles(w: int, h: int) -> list[tuple[str, str]]:
    """ffmpeg crop expressions covering the entire frame: the whole picture plus a 2x2 grid (small chat becomes legible when enlarged)."""
    return [("full", f"crop={w}:{h}:0:0"), ("tl", f"crop={w // 2}:{h // 2}:0:0"), ("tr", f"crop={w // 2}:{h // 2}:{w // 2}:0"),
            ("bl", f"crop={w // 2}:{h // 2}:0:{h // 2}"), ("br", f"crop={w // 2}:{h // 2}:{w // 2}:{h // 2}")]


def scan_moment(media: Path, start: float, end: float, *, step: float = 1.0, root: Path | None = None, work: Path | None = None,
                src_dims: tuple[int, int] | None = None, out_width: int = 1080) -> dict:
    """OCR the whole visible frame at samples across [start, end]; report severe language, readability and clean spans.

    Chat/text that would be smaller than MIN_READABLE_PX in the final 1080-wide output is recorded and NOT counted as verified.
    """
    import shutil
    import tempfile
    root = Path(root) if root else config.project_root()
    exe = ocr_tool(root)
    if exe is None:
        return {"ocr_available": False, "verified_clean": False, "findings": [], "note": "OCR helper unavailable (swiftc) - on-screen text NOT verified"}
    sw, sh = src_dims or (source_library.probe_media(media)["video"]["width"], source_library.probe_media(media)["video"]["height"])
    scale_out = out_width / sw
    tmp = Path(work) if work else Path(tempfile.mkdtemp(prefix="clipagent-textscan-"))
    tmp.mkdir(parents=True, exist_ok=True)
    times = []
    t = start + 0.2
    while t < end - 0.1:
        times.append(round(t, 2))
        t += step
    times.append(round(max(end - 0.2, start), 2))
    jobs = []
    for ti in times:
        for name, crop in _tiles(sw, sh):
            out = tmp / f"t{int(ti * 100):07d}_{name}.png"
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ti:.2f}", "-i", str(media), "-frames:v", "1", "-vf",
                                f"{crop},scale=iw*{2 if name != 'full' else 1.5}:-2", str(out)], capture_output=True)
            if r.returncode == 0 and out.is_file():
                jobs.append((ti, name, out, 2 if name != "full" else 1.5, (sh // 2) if name != "full" else sh))
    findings, boxes_seen, unreadable, readable, incomplete = [], 0, 0, 0, 0
    for k in range(0, len(jobs), 40):
        batch = jobs[k:k + 40]
        try:
            p = subprocess.run([str(exe), *[str(b[2]) for b in batch]], capture_output=True, text=True, timeout=180)
            res = json.loads(p.stdout)
        except (subprocess.TimeoutExpired, ValueError):          # a hung/failed OCR batch is NOT "clean": the scan is marked incomplete
            incomplete += len(batch)
            continue
        for (ti, name, _, up, tile_h), r in zip(batch, res):
            for b in r.get("boxes", []):
                if len(b["text"].strip()) < 3:
                    continue
                boxes_seen += 1
                out_px = b["h"] * tile_h * scale_out          # text height in the final output
                if out_px < MIN_READABLE_PX:
                    unreadable += 1
                else:
                    readable += 1
                for fnd in scan_text(b["text"]):
                    findings.append({"t": ti, "tile": name, "text": b["text"][:120], "output_px": round(out_px, 1), **fnd})
    if not work:
        shutil.rmtree(tmp, ignore_errors=True)
    blocked = _blocked_spans(findings, start, end)
    return {"ocr_available": True, "samples": len(times), "text_boxes": boxes_seen, "readable_in_output": readable, "too_small_in_output": unreadable,
            "findings": findings, "blocked_spans": blocked, "clean_spans": clean_spans(start, end, blocked),
            "chat_unverified": unreadable > 0 or incomplete > 0, "incomplete_ocr_images": incomplete,
            "verified_clean": not findings and unreadable == 0 and incomplete == 0,
            "note": ("severe on-screen language found - trim around it or reject the candidate" if findings else
                     ("part of the OCR scan did not complete; recorded as UNVERIFIED, not clean" if incomplete else
                      ("text is too small to read in the 1080x1920 output; it is recorded as UNVERIFIED, not clean" if unreadable else "no severe on-screen text")))}


def _blocked_spans(findings: list[dict], start: float, end: float, pad: float = 1.2) -> list[list[float]]:
    """A chat line stays on screen for a while: block from the first to the last sighting of the same text, plus padding."""
    by: dict[str, list[float]] = {}
    for f in findings:
        by.setdefault(normalize(f["text"]), []).append(f["t"])
    spans = sorted([max(min(ts) - pad, start), min(max(ts) + pad, end)] for ts in by.values())
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[round(a, 2), round(b, 2)] for a, b in merged]


def clean_spans(start: float, end: float, blocked: list[list[float]], min_len: float = 5.0) -> list[list[float]]:
    """Longest-first spans of [start, end] free of blocked time that are still long enough to be a moment."""
    out, cur = [], start
    for a, b in blocked:
        if a - cur >= min_len:
            out.append([round(cur, 2), round(a, 2)])
        cur = max(cur, b)
    if end - cur >= min_len:
        out.append([round(cur, 2), round(end, 2)])
    return sorted(out, key=lambda s: s[0] - s[1])


def trim_suggestion(scan: dict) -> list[float] | None:
    """Best clean span when severe text appears: trim around it when possible, otherwise None (reject the candidate)."""
    spans = scan.get("clean_spans") or []
    return spans[0] if scan.get("findings") and spans else None
