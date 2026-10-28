"""Validate a production plan before rendering. Weak videos are rejected, never padded.

Duration is metadata, never an editorial rule (CONTENT_EDITING_RULES.md): there is no per-moment or per-video minimum, maximum or warning. The only
duration check is the destination's technical upload limit; a video that would exceed it is split only at a natural story/scene boundary.
"""
from dataclasses import dataclass, field

from . import style, titles

FORMATS = ("ranking", "standalone", "funny", "continuous")
SINGLE = ("standalone", "funny", "continuous")
SHARED_BOUNDARY_SEC = 3.0   # two scenes meeting at one compilation cut may share up to this much audio (the word that straddles the cut); more is a real overlap
NEAR_IDENTICAL = 0.85       # share of the union of two videos' source footage that is the same => a renamed/recropped duplicate, never a different edit


@dataclass
class PlanResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > SHARED_BOUNDARY_SEC


def _span(moments: list[dict]) -> list[tuple[float, float]]:
    """Merged source intervals of a video's moments."""
    out: list[list[float]] = []
    for a, b in sorted((m["start"], m["end"]) for m in moments):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def footage_similarity(a: list[dict], b: list[dict]) -> float:
    """Intersection over union of the source footage two videos use (0 = disjoint, 1 = identical timelines)."""
    sa, sb = _span(a), _span(b)
    inter = sum(max(0.0, min(x1, y1) - max(x0, y0)) for x0, x1 in sa for y0, y1 in sb)
    union = sum(x1 - x0 for x0, x1 in sa) + sum(y1 - y0 for y0, y1 in sb) - inter
    return inter / union if union > 0 else 0.0


def _cut_long(m: dict, limit: float, boundaries: list[float] | None) -> list[dict]:
    """Cut ONE over-limit moment only at supplied scene/story boundaries; with none it is returned whole (never cut blindly)."""
    pts = sorted(x for x in (boundaries or []) if m["start"] < x < m["end"]) + [m["end"]]
    out, lo, last = [], m["start"], None
    for p in pts:
        if p - lo > limit and last is not None:
            out.append({**m, "start": lo, "end": last})
            lo = last
        last = p
    out.append({**m, "start": lo, "end": m["end"]})
    return out


def split_at_boundaries(moments: list[dict], limit: float, boundaries: list[float] | None = None) -> list[list[dict]]:
    """Split a too-long ordered list of moments at natural story/scene boundaries only (never mid-moment, never to hit a length target).

    Each moment is an atomic story unit; groups are filled in order and a new group starts only when the next whole moment would exceed `limit`.
    A single moment longer than `limit` is cut only at one of `boundaries` (scene/story boundaries inside it); with none it stays whole and the
    caller must report the unsolved technical limit."""
    groups: list[list[dict]] = [[]]
    total = 0.0
    for m in moments:
        d = m["end"] - m["start"]
        pieces = _cut_long(m, limit, boundaries) if d > limit else [m]
        for pc in pieces:
            pd = pc["end"] - pc["start"]
            if groups[-1] and total + pd > limit:
                groups.append([])
                total = 0.0
            groups[-1].append(pc)
            total += pd
    return [g for g in groups if g]


def validate(videos: list[dict], used_clips: list[dict] | None = None, conflict_fn=None) -> PlanResult:
    """videos: [{'format': 'ranking'|'standalone'|'funny'|'continuous' (default ranking), 'category': str (rankings only), 'id': optional str,
    'reuse': optional {'of': <other video id>, 'reason': str} when the same source moment deliberately appears in a second, materially different video,
    'moments': [{'rank': int, 'title': str, 'start': float, 'end': float, 'verified': bool}]}].

    Ranking moments are in any order; rank 1 is the last clip played (countdown). A ranking needs at least `min_ranks` honest entries and has no
    padded count; a single-moment video has exactly one moment (rank 1, never drawn as a ranking). There is no cap on the number of videos per run.
    """
    S = style.load()
    lim = S["limits"]
    res = PlanResult()
    cap = (S.get("production") or {}).get("max_videos_per_run")
    if cap and len(videos) > cap:
        res.errors.append(f"at most {cap} videos per run, got {len(videos)}")
    for vi, v in enumerate(videos, 1):
        if v.get("format", "ranking") not in FORMATS:
            res.errors.append(f"video {vi}: unknown format {v.get('format')!r} (expected {', '.join(FORMATS)})")
    cats = [v.get("category", "").strip().lower() for v in videos if v.get("format", "ranking") == "ranking"]
    if "" in cats or len(set(cats)) != len(cats):
        res.errors.append("every ranking video needs a distinct, non-empty ranking category")
    used: list[tuple[float, float, int]] = []
    for u in used_clips or []:       # permanent registry: moments from earlier finished videos (already filtered to this source)
        used.append((u["start"], u["end"], f"an earlier run ({u['run_id']}: {u['title']})"))
    by_id = {v.get("id"): (vi, v) for vi, v in enumerate(videos, 1) if v.get("id")}
    partners: dict[int, set] = {}                      # declared reuse pairs (both directions): overlap between them is intentional and recorded
    for vi, v in enumerate(videos, 1):
        tv = by_id.get((v.get("reuse") or {}).get("of"))
        if tv:
            partners.setdefault(vi, set()).add(tv[0])
            partners.setdefault(tv[0], set()).add(vi)
    for vi, v in enumerate(videos, 1):
        tag = f"video {vi}"
        ms = v["moments"]
        standalone = v.get("format", "ranking") in SINGLE
        reuse = v.get("reuse")
        if reuse:
            tv = by_id.get(reuse.get("of"))
            if not tv:
                res.errors.append(f"{tag}: reuse refers to unknown video {reuse.get('of')!r}")
            elif not (reuse.get("reason") or "").strip():
                res.errors.append(f"{tag}: intentional reuse needs a recorded reason (what materially different experience it gives)")
            elif (tv[1].get("format", "ranking") in SINGLE) == standalone:
                res.errors.append(f"{tag}: reuse is only allowed between a ranking and a standalone video, not two videos of the same kind")
        if standalone:
            if len(ms) != 1:
                res.errors.append(f"{tag}: a {v.get('format')} video has exactly one moment, got {len(ms)}")
            for m in ms:
                try:
                    (titles.validate_headline if v.get("format") in ("funny", "continuous") else titles.validate_title)(m["title"])
                except titles.TitleError as exc:
                    res.errors.append(f"{tag}: {exc}")
        else:
            try:
                titles.validate_entries([{"rank": m["rank"], "title": m["title"]} for m in ms])
            except titles.TitleError as exc:
                res.errors.append(f"{tag}: {exc}")
        for m in ms:
            if conflict_fn:                       # permanent registry + active reservations, checked globally
                for why in conflict_fn(m):
                    res.errors.append(f"{tag} #{m['rank']}: {why}")
            if not m.get("verified"):
                res.errors.append(f"{tag} #{m['rank']}: timestamp not verified against footage/audio")
            if not m["end"] > m["start"]:
                res.errors.append(f"{tag} #{m['rank']}: end must be after start")
            for (a, b, other) in used:
                if overlaps((m["start"], m["end"]), (a, b)) and other not in partners.get(vi, ()):
                    res.errors.append(f"{tag} #{m['rank']}: source moment {m['start']}-{m['end']} already used in video {other}"
                                      + ("" if standalone or reuse else " (record an intentional reuse if the two videos genuinely differ)"))
        used += [(m["start"], m["end"], vi) for m in ms]
        total = sum(m["end"] - m["start"] for m in ms)
        if total > lim["video_platform_max_seconds"]:   # the ONLY duration check: the destination's technical upload limit
            res.errors.append(f"{tag}: {total:.1f}s exceeds the platform's {lim['video_platform_max_seconds']}s technical upload limit; "
                              "split at a natural story/scene boundary (plan.split_at_boundaries)")
    for i, a in enumerate(videos, 1):                 # an identical or renamed/recropped timeline is not a different edit
        for j, b in enumerate(videos[i:], i + 1):
            sim = footage_similarity(a["moments"], b["moments"])
            if sim >= NEAR_IDENTICAL:
                res.errors.append(f"video {i} and video {j} use {sim:.0%} of the same footage: a near-identical duplicate, not a different edit")
    try:
        titles.check_unique_across_run([[{"title": m["title"]} for m in v["moments"]] for v in videos])
    except titles.TitleError as exc:
        res.errors.append(str(exc))
    return res
