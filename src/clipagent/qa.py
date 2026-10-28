"""Technical QA for finished renders. Anything that cannot be confirmed automatically is 'manual_review', never 'pass'."""
import json
import re
import subprocess
from pathlib import Path

from . import style

PASS, FAIL, MANUAL = "pass", "fail", "manual_review"


def check(name, status, detail=""):
    return {"check": name, "status": status, "detail": detail}


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return json.loads(r.stdout)


def parse_intervals(stderr: str, start_key: str, dur_key: str) -> list[tuple[float, float]]:
    starts = [float(x) for x in re.findall(rf"{start_key}:\s*([\d.]+)", stderr)]
    durs = [float(x) for x in re.findall(rf"{dur_key}:\s*([\d.]+)", stderr)]
    return list(zip(starts, durs))


def parse_max_volume(stderr: str) -> float | None:
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", stderr)
    return float(m.group(1)) if m else None


def parse_clipped_ratio(stderr: str) -> float | None:
    """Share of samples sitting at digital full scale (volumedetect histogram_0db / n_samples); None when not reported."""
    n = re.findall(r"n_samples:\s*(\d+)", stderr)           # volumedetect prints an empty first report; the real one is last
    h = re.findall(r"histogram_0db:\s*(\d+)", stderr)
    return (int(h[-1]) / int(n[-1])) if n and h and int(n[-1]) else None


CLIP_RATIO = 0.0002      # more than 0.02 % of samples at full scale = audible clipping; a loud peak that merely touches 0 dB is not


def classify_freezes(frozen: list, dur: float, tail_limit: float = 1.0) -> tuple[list, list]:
    """(failing, allowed). Freeze frames and holds are a permitted creative effect (a scoreboard payoff, a deliberate freeze); only a dead frame
    at the very start or a held tail longer than `tail_limit` seconds at the very end is a defect."""
    bad, ok = [], []
    for a, d in frozen:
        if a <= 0.5 or (a + d >= dur - 0.3 and d > tail_limit):
            bad.append((a, d))
        else:
            ok.append((a, d))
    return bad, ok


def _ff(args: list[str]) -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-v", "info", *args], capture_output=True, text=True).stderr


# Visual items no script can prove. They are manual_review BY DESIGN; any other manual_review (e.g. odd duration) is a real doubt.
DESIGN_MANUAL = {"label_matches_moment", "faces_and_action_visible", "no_stacked_burned_in_captions", "no_repeated_moments",
                 "hook_and_payoff_clear"}
AUTO_APPROVED = "auto_qa_approved"


def auto_decision(report: dict) -> tuple[bool, list[str]]:
    """Unattended gate: approve only if nothing failed and the only manual_review items are the by-design visual ones."""
    reasons = []
    for c in report["checks"]:
        if c["status"] == FAIL:
            reasons.append(f"FAIL {c['check']} {c['detail']}".strip())
        elif c["status"] == MANUAL and c["check"] not in DESIGN_MANUAL:
            reasons.append(f"uncertain {c['check']} {c['detail']}".strip())
    return (not reasons), reasons


def container_checks(info: dict, path: Path, fmt: str = "ranking") -> list[dict]:
    S = style.load()
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"]["duration"])
    cap = S["limits"]["video_platform_max_seconds"]
    out = [check("resolution_1080x1920", PASS if (v["width"], v["height"]) == (S["canvas"]["width"], S["canvas"]["height"]) else FAIL,
                 f"{v['width']}x{v['height']}"),
           check("aspect_9_16", PASS if v["width"] * 16 == v["height"] * 9 else FAIL),
           check("h264_video", PASS if v["codec_name"] == "h264" else FAIL, v["codec_name"]),
           check("aac_audio", PASS if a and a["codec_name"] == "aac" else FAIL, a["codec_name"] if a else "no audio"),
           # Length is content-driven: only TikTok's own upload limit is a technical failure (a video over 60 s passes).
           check("duration_within_platform_limit", PASS if 0 < dur <= cap else FAIL, f"{dur:.2f}s (limit {cap}s)")]
    head = path.open("rb").read(1_000_000)
    i, j = head.find(b"moov"), head.find(b"mdat")
    out.append(check("faststart", PASS if 0 < i < j or (i > 0 and j == -1) else FAIL))
    if a:
        gap = abs(float(a.get("duration", dur)) - float(v.get("duration", dur)))
        out.append(check("av_sync_stream_durations", PASS if gap <= 0.12 else FAIL, f"audio/video duration gap {gap:.3f}s"))
    return out


def signal_checks(path: Path) -> list[dict]:
    black = parse_intervals(_ff(["-i", str(path), "-vf", "blackdetect=d=0.1:pix_th=0.05", "-an", "-f", "null", "-"]),
                            "black_start", "black_duration")
    frozen = parse_intervals(_ff(["-i", str(path), "-vf", "freezedetect=n=-60dB:d=1", "-an", "-f", "null", "-"]),
                             "lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_duration")
    vtxt = _ff(["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    vol, ratio = parse_max_volume(vtxt), parse_clipped_ratio(vtxt)
    dur = float(probe(path)["format"]["duration"])
    bad_freeze, held = classify_freezes(frozen, dur)
    clipped = vol is not None and vol >= -0.1 and (ratio is None or ratio > CLIP_RATIO)
    return [check("no_black_frames", FAIL if black else PASS, f"{black}" if black else ""),
            check("no_frozen_frames", FAIL if bad_freeze else PASS, f"{bad_freeze}" if bad_freeze else (f"holds allowed: {held}" if held else "")),
            check("no_clipped_audio", MANUAL if vol is None else (FAIL if clipped else PASS),
                  f"max_volume {vol} dB" + (f", {ratio:.5%} of samples at full scale" if ratio is not None else ""))]


def layout_checks(fmt: str = "ranking") -> list[dict]:
    S = style.load()
    cap_bottom = S["captions"]["y"] + S["captions"]["height"]
    if fmt in ("standalone", "headline"):     # no ranking header/chips/badge in these formats
        out = [check("captions_inside_safe_area", PASS if cap_bottom <= S["safe_margins"]["bottom_limit_px"] else FAIL, f"bottom {cap_bottom}")]
        if fmt == "headline":                  # funny / continuous: the headline pill must clear the captions
            lb_bot = S["label"]["y"] + S["label"]["height"] + S["label"]["rise_px"]
            out.append(check("headline_clear_of_captions", PASS if lb_bot < S["captions"]["y"] else FAIL, f"headline {S['label']['y']}-{lb_bot}, captions from {S['captions']['y']}"))
        return out
    lb_top, lb_bot = S["label"]["y"], S["label"]["y"] + S["label"]["height"] + S["label"]["rise_px"]
    return [check("captions_inside_safe_area", PASS if cap_bottom <= S["safe_margins"]["bottom_limit_px"] else FAIL, f"bottom {cap_bottom}"),
            check("label_clear_of_captions", PASS if lb_bot < S["captions"]["y"] else FAIL, f"label {lb_top}-{lb_bot}, captions from {S['captions']['y']}"),
            check("header_clear_of_top_ui", PASS if S["header"]["title_y"] + S["header"]["dy"] >= S["safe_margins"]["top_clear_px"] else FAIL)]


def manual_checks(labels: list[dict] | None, fmt: str = "ranking") -> list[dict]:
    if fmt in ("standalone", "headline"):
        return [check("hook_and_payoff_clear", MANUAL, "needs eyes on the contact sheet"),
                check("faces_and_action_visible", MANUAL, "needs eyes on the contact sheet"),
                check("no_stacked_burned_in_captions", MANUAL, "crop removes the old strip by design; confirm visually"),
                check("no_repeated_moments", MANUAL, "plan-level check passed before render; confirm no repeated action on screen")]
    shown = ", ".join(f"#{e['rank']} {e['title']}" for e in labels or [])
    return [check("label_matches_moment", MANUAL, f"review contact sheet; labels: {shown}"),
            check("faces_and_action_visible", MANUAL, "needs eyes on the contact sheet"),
            check("no_stacked_burned_in_captions", MANUAL, "crop removes the old strip by design; confirm visually"),
            check("no_repeated_moments", MANUAL, "plan-level check passed before render; confirm no repeated action on screen")]


def contact_sheet(path: Path, out_png: Path, n: int = 12) -> Path:
    dur = float(probe(path)["format"]["duration"])
    r = subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", str(path), "-vf", f"fps={n / dur:.4f},scale=270:-1,tile=6x{(n + 5) // 6}",
                        "-frames:v", "1", str(out_png)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return out_png


def run_qa(path: Path, qa_dir: Path, labels: list[dict] | None = None, fmt: str = "ranking", extra: list[dict] | None = None) -> dict:
    path, qa_dir = Path(path), Path(qa_dir)
    checks = container_checks(probe(path), path, fmt) + signal_checks(path) + layout_checks(fmt) + manual_checks(labels, fmt) + list(extra or [])
    sheet = qa_dir / f"{path.stem}.contact.png"
    try:
        contact_sheet(path, sheet)
    except RuntimeError as exc:
        checks.append(check("contact_sheet", FAIL, str(exc)))
    verdict = FAIL if any(c["status"] == FAIL for c in checks) else (MANUAL if any(c["status"] == MANUAL for c in checks) else PASS)
    rep = {"file": path.name, "format": fmt, "verdict": verdict, "checks": checks, "contact_sheet": sheet.name if sheet.exists() else None}
    with (qa_dir / f"{path.stem}.qa.json").open("x") as f:
        json.dump(rep, f, indent=2)
    return rep
