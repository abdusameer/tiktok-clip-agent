"""Ranking labels: rank and title are stored separately; '#N Title' is only ever composed on output."""
import re

from . import style

SMALL = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "vs"}


class TitleError(ValueError):
    pass


def words(title: str) -> list[str]:
    return title.split()


def normalize(title: str) -> str:
    """Consistent Title Case; keeps existing all-caps/mixed-case words (IRL, KSI, TikTok) and small words lowercase."""
    out = []
    for i, w in enumerate(words(title)):
        if w.isupper() or any(c.isupper() for c in w[1:]):
            out.append(w)
        elif i and w.lower() in SMALL:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def validate_title(title: str) -> None:
    lim = style.load()["limits"]
    if re.search(r"#\s*\d", title):
        raise TitleError(f"title must not contain a rank number: {title!r}")
    n = len(words(title))
    if not lim["title_min_words"] <= n <= lim["title_max_words"]:
        raise TitleError(f"title must be {lim['title_min_words']}-{lim['title_max_words']} words, got {n}: {title!r}")
    if title != normalize(title):
        raise TitleError(f"inconsistent capitalization: {title!r} (expected {normalize(title)!r})")


def validate_headline(headline: str) -> None:
    """Funny / continuous videos carry one truthful 2-6 word headline (no rank number)."""
    lim = style.load()["limits"]
    if re.search(r"#\s*\d", headline):
        raise TitleError(f"headline must not contain a rank number: {headline!r}")
    n = len(words(headline))
    if not lim["headline_min_words"] <= n <= lim["headline_max_words"]:
        raise TitleError(f"headline must be {lim['headline_min_words']}-{lim['headline_max_words']} words, got {n}: {headline!r}")
    if headline != normalize(headline):
        raise TitleError(f"inconsistent capitalization: {headline!r} (expected {normalize(headline)!r})")


def validate_entries(entries: list[dict], expected_ranks: list[int] | None = None) -> list[dict]:
    """entries: [{'rank': int, 'title': str}]. Ranks must be unique ints; titles unique and valid."""
    lim = style.load()["limits"]
    if not lim["min_ranks"] <= len(entries) <= lim["max_ranks"]:
        raise TitleError(f"need {lim['min_ranks']}-{lim['max_ranks']} ranked moments, got {len(entries)}")
    ranks = []
    for e in entries:
        if set(e) != {"rank", "title"}:
            raise TitleError(f"label entries must be exactly {{rank, title}}: {e}")
        if not isinstance(e["rank"], int) or isinstance(e["rank"], bool):
            raise TitleError(f"rank must be an integer: {e}")
        validate_title(e["title"])
        ranks.append(e["rank"])
    if sorted(ranks) != list(range(1, len(entries) + 1)):
        raise TitleError(f"ranks must be 1..{len(entries)} with no gaps or repeats, got {sorted(ranks)}")
    if expected_ranks is not None and sorted(ranks) != sorted(expected_ranks):
        raise TitleError(f"label ranks {sorted(ranks)} do not match the clip ranks {sorted(expected_ranks)}")
    lowered = [e["title"].lower() for e in entries]
    if len(set(lowered)) != len(lowered):
        raise TitleError("duplicate title within a video")
    return sorted(entries, key=lambda e: -e["rank"])  # countdown order


def label_text(entry: dict) -> str:
    """Combined '#N Title' form for manifests, QA and logs. Not drawn on the pill (the badge shows the number)."""
    return f"#{entry['rank']} {entry['title']}"


def check_unique_across_run(videos: list[list[dict]]) -> None:
    seen: dict[str, int] = {}
    for vi, entries in enumerate(videos):
        for e in entries:
            k = e["title"].lower()
            if k in seen:
                raise TitleError(f"title {e['title']!r} repeated in video {vi + 1} (already used in video {seen[k] + 1})")
            seen[k] = vi
