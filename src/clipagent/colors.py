"""Controlled accent color for titles/headlines (config/ranking_style.json: label.title_accent).

ONE solid palette color per finished video (consistent across every title in it), seeded by run id + video id + title so a re-render reproduces
it; contrast-checked against the label background and replaced (another palette color, else white) when it fails. No gradients, per-letter
colors, flashing or off-palette colors.
"""
import hashlib
import re

from . import style

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ColorError(ValueError):
    pass


def _cfg() -> dict:
    return style.load()["label"]["title_accent"]


def to_rgb(h: str) -> tuple[int, int, int]:
    if not _HEX.match(h or ""):
        raise ColorError(f"not a #RRGGBB color: {h!r}")
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def luminance(rgb) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def label_background() -> tuple[float, float, float]:
    """The label pill fill composited over the reference backdrop (a mid-grey stand-in for the video behind it)."""
    fill = style.load()["label"]["background"]["fill"]
    ref = _cfg()["reference_backdrop"]
    a = fill[3] / 255
    return tuple(fill[i] * a + ref[i] * (1 - a) for i in range(3))


def readable(hex_color: str) -> bool:
    return contrast(to_rgb(hex_color), label_background()) >= _cfg()["min_contrast"]


def palette() -> list[str]:
    return [p["hex"].upper() for p in _cfg()["palette"]]


def seed_for(run_id: str, video_id: str, title: str = "") -> str:
    return f"{run_id}|{video_id}|{title}"


def video_color(seed: str) -> str:
    """The ONE accent color of a finished video (deterministic for the seed); another palette color / white if contrast ever fails."""
    ok = [c for c in palette() if readable(c)]
    if not ok:
        return _cfg()["fallback"].upper()
    return ok[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(ok)]


def assign(seed: str, ranks: list[str]) -> dict[str, str]:
    """{rank: '#RRGGBB'}: every title of a video shares its single accent color (consistent look; videos differ from each other)."""
    c = video_color(seed)
    return {r: c for r in ranks}


def resolve(explicit: dict | None, seed: str, ranks: list[str]) -> dict[str, str]:
    """Explicit colors are kept only when they are one palette color, readable, shared by all ranks; otherwise the seeded color is used."""
    vals = {(explicit or {}).get(r, "").upper() for r in ranks}
    if explicit and len(vals) == 1:
        c = vals.pop()
        if c in set(palette()) and readable(c):
            return {r: c for r in ranks}
    return assign(seed, ranks)


def validate(colors: dict[str, str], ranks: list[str]) -> list[str]:
    cfg, pal, probs = _cfg(), set(palette()), []
    used = {(colors.get(r) or "").upper() for r in ranks}
    if len(used) != 1:
        probs.append("a video must use ONE consistent accent color")
    for c in used:
        if c not in pal:
            probs.append(f"{c or 'missing'} is not in the approved palette")
        elif not readable(c):
            probs.append(f"{c} fails the {cfg['min_contrast']}:1 contrast check")
    return probs
