"""Validate a production plan before rendering. Weak videos are rejected, never padded."""
from dataclasses import dataclass, field

from . import style, titles

FORMATS = ("ranking", "standalone", "funny", "continuous")
SINGLE = ("standalone", "funny", "continuous")


@dataclass
class PlanResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def validate(videos: list[dict], used_clips: list[dict] | None = None, conflict_fn=None) -> PlanResult:
    """videos: [{'format': 'ranking'|'standalone' (default ranking), 'category': str (rankings only),
    'moments': [{'rank': int, 'title': str, 'start': float, 'end': float, 'verified': bool}]}].

    Ranking moments are in any order; rank 1 is the last clip played (countdown). A standalone video has exactly one moment
    (rank 1, never drawn) and its own 2-5 word title. There is no fixed cap on the number of videos per run.
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
    for vi, v in enumerate(videos, 1):
        tag = f"video {vi}"
        ms = v["moments"]
        standalone = v.get("format", "ranking") in SINGLE
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
            if len(ms) > lim["max_ranks"]:
                res.errors.append(f"{tag}: more than {lim['max_ranks']} ranks")
        for m in ms:
            if conflict_fn:                       # permanent registry + active reservations, checked globally
                for why in conflict_fn(m):
                    res.errors.append(f"{tag} #{m['rank']}: {why}")
            if not m.get("verified"):
                res.errors.append(f"{tag} #{m['rank']}: timestamp not verified against footage/audio")
            if not m["end"] > m["start"]:
                res.errors.append(f"{tag} #{m['rank']}: end must be after start")
            for (a, b, other) in used:
                if overlaps((m["start"], m["end"]), (a, b)):
                    res.errors.append(f"{tag} #{m['rank']}: source moment {m['start']}-{m['end']} already used in video {other}")
        used += [(m["start"], m["end"], vi) for m in ms]
        total = sum(m["end"] - m["start"] for m in ms)
        for m in ms:                                  # per-moment guidance: warnings only, never padded or force-trimmed
            d = m["end"] - m["start"]
            if d < lim["moment_min_seconds"] and not m.get("complete_when_short"):
                res.warnings.append(f"{tag} #{m['rank']}: {d:.1f}s is under {lim['moment_min_seconds']}s; keep it only if it is completely "
                                    "understandable (set complete_when_short) - never extend a weak moment to reach the minimum")
            if not standalone and d > lim["moment_max_seconds"] + lim["moment_max_tolerance_seconds"]:
                res.warnings.append(f"{tag} #{m['rank']}: {d:.1f}s is above the ~{lim['moment_max_seconds']}s guide; trim to the shortest span that "
                                    "keeps setup, dialogue/action and payoff (no cut-off words or reactions)")
        if total > lim["video_platform_max_seconds"]:
            res.errors.append(f"{tag}: {total:.1f}s exceeds TikTok's {lim['video_platform_max_seconds']}s upload limit")
    try:
        titles.check_unique_across_run([[{"title": m["title"]} for m in v["moments"]] for v in videos])
    except titles.TitleError as exc:
        res.errors.append(str(exc))
    return res
