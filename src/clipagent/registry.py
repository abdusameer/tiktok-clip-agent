"""Permanent used-clips registry (registry/used_clips.json). Survives run cleanup so a moment is never reused.

Entries are only ever appended (or a source flagged as 'local media cleaned'); nothing is removed or rewritten.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

REJECTED = "rejected_by_user"
MIN_OVERLAP_SEC = 0.25
BOUNDARY_SEC = 5.0          # a moment starting/ending within ~5 s of a used exchange is the same exchange
SIMILAR = 0.6               # transcript sketch similarity treated as the same moment


def path(root: Path | None = None) -> Path:
    return (Path(root) if root else config.project_root()) / "registry" / "used_clips.json"


def load(root: Path | None = None) -> dict:
    p = path(root)
    return json.loads(p.read_text()) if p.is_file() else {"version": 1, "sources": {}, "clips": []}


def _save(reg: dict, root: Path | None) -> None:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    os.replace(tmp, p)


def text_fingerprint(words: list[str]) -> str:
    norm = " ".join(re.sub(r"[^a-z0-9']", "", w.lower()) for w in words if re.sub(r"[^a-z0-9']", "", w.lower()))
    return hashlib.sha256(norm.encode()).hexdigest()


def overlaps(reg: dict, source_sha256: str, start: float, end: float) -> list[dict]:
    return [c for c in reg["clips"] if c.get("status") != REJECTED and c["source_sha256"] == source_sha256
            and min(end, c["end"]) - max(start, c["start"]) > MIN_OVERLAP_SEC]


def register(source: dict, clips: list[dict], root: Path | None = None) -> dict:
    """source: {sha256, id, files:[{name,sha256,size_bytes}], duration_sec, ...}; clips: dicts with source_sha256/run_id/rank/title/start/end/..."""
    reg = load(root)
    reg["sources"].setdefault(source["sha256"], {**source, "local_media_cleaned": False, "cleaned_at": None})
    for c in clips:
        dup = [x for x in reg["clips"] if x["source_sha256"] == c["source_sha256"] and x["run_id"] == c["run_id"] and x["rank"] == c["rank"]
               and x.get("video") == c.get("video")]   # one run can hold several videos that each have ranks 1..N
        if dup:
            if dup[0]["start"] != c["start"] or dup[0]["end"] != c["end"]:
                raise ValueError(f"registry already holds a different clip for run {c['run_id']} rank {c['rank']}")
            continue
        other = overlaps(reg, c["source_sha256"], c["start"], c["end"])
        if other:
            raise ValueError(f"clip {c['start']}-{c['end']} overlaps an already-used moment ({other[0]['title']!r}, run {other[0]['run_id']})")
        reg["clips"].append({**c, "registered_at": datetime.now(timezone.utc).isoformat()})
    _save(reg, root)
    return reg


def mark_source_cleaned(source_sha256: str, when: str, root: Path | None = None) -> None:
    reg = load(root)
    if source_sha256 not in reg["sources"]:
        raise ValueError("source not in registry")
    reg["sources"][source_sha256].update({"local_media_cleaned": True, "cleaned_at": when})
    _save(reg, root)


def _norm_words(words: list[str]) -> list[str]:
    return [w for w in (re.sub(r"[^a-z0-9']", "", x.lower()) for x in words) if w]


def sketch(words: list[str], k: int = 3, size: int = 64) -> list[int]:
    """MinHash-style sketch of word 3-grams: lets different cuts/encodings of the same exchange be recognised globally."""
    w = _norm_words(words)
    grams = {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))} if w else set()
    return sorted(int(hashlib.sha1(g.encode()).hexdigest()[:12], 16) for g in grams)[:size]


def similarity(a: list[int], b: list[int]) -> float:
    if not a or not b:
        return 0.0
    return len(set(a) & set(b)) / min(len(a), len(b))


def conflicts(source_sha256: str, start: float, end: float, text_sha256: str | None = None, sk: list[int] | None = None,
              root: Path | None = None, ignore_run: str | None = None) -> list[str]:
    """Reasons a candidate moment must be rejected: used (registry) or reserved (active runs). Same speaker/topic is NOT a reason."""
    reg, out = load(root), []
    for c in reg["clips"]:
        if c.get("status") == REJECTED or (ignore_run and c["run_id"] == ignore_run):     # rejected: audit only; own run: planned together
            continue
        if c["source_sha256"] == source_sha256:
            if min(end, c["end"]) - max(start, c["start"]) > MIN_OVERLAP_SEC:
                out.append(f"overlaps used moment {c['title']!r} (run {c['run_id']})")
            elif start < c["end"] + BOUNDARY_SEC and end > c["start"] - BOUNDARY_SEC:
                out.append(f"within {BOUNDARY_SEC:g}s of used moment {c['title']!r} (run {c['run_id']})")
        if text_sha256 and c.get("transcript_text_sha256") == text_sha256:
            out.append(f"same transcript as used moment {c['title']!r} (run {c['run_id']})")
        elif sk and c.get("sketch") and similarity(sk, c["sketch"]) >= SIMILAR:
            out.append(f"transcript substantially similar to used moment {c['title']!r} (run {c['run_id']})")
    for r in load_reservations(root)["reservations"]:
        if r["status"] == "active" and r["run_id"] != ignore_run and r["source_sha256"] == source_sha256 \
                and start < r["end"] + BOUNDARY_SEC and end > r["start"] - BOUNDARY_SEC:
            out.append(f"reserved by active run {r['run_id']}")
    return out


def reservations_path(root: Path | None = None) -> Path:
    return path(root).with_name("reservations.json")


def load_reservations(root: Path | None = None) -> dict:
    p = reservations_path(root)
    return json.loads(p.read_text()) if p.is_file() else {"reservations": []}


def reserve(run_id: str, source_sha256: str, moments: list[tuple[float, float]], root: Path | None = None) -> None:
    """Hold moments for an in-progress run so a parallel run cannot pick them."""
    data = load_reservations(root)
    for a, b in moments:
        bad = conflicts(source_sha256, a, b, root=root, ignore_run=run_id)
        if bad:
            raise ValueError(f"cannot reserve {a}-{b}: {bad[0]}")
        data["reservations"].append({"run_id": run_id, "source_sha256": source_sha256, "start": a, "end": b, "status": "active",
                                     "reserved_at": datetime.now(timezone.utc).isoformat()})
    p = reservations_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def release(run_id: str, root: Path | None = None) -> int:
    data = load_reservations(root)
    n = 0
    for r in data["reservations"]:
        if r["run_id"] == run_id and r["status"] == "active":
            r["status"] = "released"
            n += 1
    p = reservations_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)
    return n


def reject_run(run_id: str, reason: str, root: Path | None = None, note: dict | None = None) -> int:
    """The user rejected a finished run: mark its clips `rejected_by_user` (history kept, nothing removed) and release its reservations."""
    reg = load(root)
    n = 0
    for c in reg["clips"]:
        if c["run_id"] == run_id and c.get("status") != REJECTED:
            c.update(status=REJECTED, rejected_at=datetime.now(timezone.utc).isoformat(), rejection_reason=reason, **({"rejection_note": note} if note else {}))
            n += 1
    _save(reg, root)
    release(run_id, root)
    return n
