"""/make-content: plan -> specs -> render -> QA/organize -> dashboard -> cleanup -> report.

Everything here is deterministic and unattended. The only judgement input is the visual review recorded in analysis/candidates.json
(`apply-reviews`): format, title/headline, category and optional trim per segment.
"""
import base64
import html
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import captions as captions_mod
from . import cleanup, colors, config, cutqa, framing, fullanalysis, layout, plan as plan_mod, qa, registry, render, runs, source_library, style, textsafety, titles
from .status import RunStatus

FORMAT_DIR = {"ranking": "rankings", "funny": "funny-moments", "continuous": "continuous-moments"}       # working/ + plan file names (internal)
APPROVED_ROOT = "approved_clips"
APPROVED_DIR = {"ranking": "rankings", "funny": "standalone/funny", "continuous": "standalone/continuous"}   # approved_clips/<this>/<run>/<ID-slug>/
PREFIX = {"ranking": "R", "funny": "F", "continuous": "C"}
HALLUCINATION = re.compile(r"thanks for watching|subscribe|see you (in the )?next|^bye\b|like and", re.I)
PROFANITY = re.compile(r"\b(fuck\w*|shit\w*|bitch|ass|dick|pussy)\b", re.I)


class ContentError(Exception):
    pass


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "untitled"


def _root(root) -> Path:
    return Path(root) if root else config.project_root()


def run_dir_for(run_id: str, root=None) -> Path:
    d = _root(root) / "runs" / run_id
    if not d.is_dir():
        raise ContentError(f"no such run: {run_id}")
    return d


def _source(run_dir: Path) -> dict:
    return json.loads((run_dir / "ingest" / "source.json").read_text())


def _lib(source_id: str, root) -> Path:
    return _root(root) / source_library.LIB / source_id


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=float))
    os.replace(tmp, p)


# ------------------------------------------------------------------ start
def start(source_id: str, root=None) -> Path:
    """Create runs/<run-id>/ in the /make-content layout for an existing, ingested source."""
    root = _root(root)
    ref = source_library.resolve(source_id, root, require_active=False)
    d = runs.create_content_run(source_id, ref.meta["sha256"], root)
    info = json.loads((root / source_library.LIB / source_id / "source.json").read_text())
    name = (info.get("inputs") or [{}])[0].get("filename") or "master.mp4"
    _write_json(d / "ingest" / "source.json", {"source_id": source_id, "source_name": name, "sha256": info["sha256"], "duration_sec": info["master"]["duration_sec"],
                                                 "master": f"{source_library.LIB}/{source_id}/master.mp4"})
    st = RunStatus(d, source_id, name, info["master"]["duration_sec"])
    st.stage("INGESTING")
    return d


# ------------------------------------------------------------------ plan
def _tokens(c: dict) -> list[str]:
    return (c.get("transcript_excerpt") or c["id"]).split() or [c["id"]]


def _partition(n: int) -> list[int]:
    """Ranking group sizes for n same-theme candidates: ONE ranking holding every honest entry (no padding, no 4-5 quota).

    Fewer than min_ranks cannot make a ranking (returned as a negative remainder -> standalone treatment). Only the countdown header's
    technical limit (layout_max_ranks) splits a very large theme, into balanced parts that each keep at least min_ranks entries."""
    lim = style.load()["limits"]
    lo, hi = lim["min_ranks"], lim.get("layout_max_ranks") or n
    if n < lo:
        return [-n] if n else []
    parts = -(-n // hi)                      # ceil
    base, extra = divmod(n, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def build_plan(run_id: str, root=None, snap_fn=None) -> dict:
    """Allocate every reviewed segment to ONE format and write plan/{rankings,funny-moments,continuous-moments,rejected}.json."""
    root = _root(root)
    d = run_dir_for(run_id, root)
    src = _source(d)
    sid = src["source_id"]
    data = fullanalysis.load_candidates(sid, root)
    sha = src["sha256"]
    usable, rejected = [], []
    for c in data["candidates"]:
        rv = c["review"]
        base = {"id": c["id"], "start": c["start"], "end": c["end"]}
        if rv["status"] != "reviewed":
            rejected.append({**base, "reason": "not visually reviewed / unverifiable"})
            continue
        if rv["suggested_format"] == "rejected":
            rejected.append({**base, "reason": rv["rejection_reason"]})
            continue
        a, b = rv.get("use_start") or c["start"], rv.get("use_end") or c["end"]
        if snap_fn and not registry.conflicts(sha, a, b, root=root, ignore_run=run_id):   # (already-used scenes are rejected below; no need to decode them)
            # never cut through a spoken word (QA would fail it): snap to clean cut points
            a, b = snap_fn(_lib(sid, root) / "master.mp4", a, b,
                           ok=lambda x, y: not registry.conflicts(sha, x, y, root=root, ignore_run=run_id))   # never snap into an already-used scene
        bad = registry.conflicts(sha, a, b, root=root, ignore_run=run_id)
        if bad:
            rejected.append({**base, "reason": "already used or reserved: " + bad[0]})
            continue
        usable.append({"id": c["id"], "format": rv["suggested_format"] if rv["suggested_format"] in FORMAT_DIR else "funny", "title": rv.get("title"),
                       "category": rv.get("category"), "start": a, "end": b, "humor": c["humor_score"], "cand": c, "review": rv})
    used_titles: set[str] = set()

    def title_ok(it, kind):
        t = it["title"] or ""
        try:
            (titles.validate_title if kind == "ranking" else titles.validate_headline)(t)
        except titles.TitleError as exc:
            rejected.append({"id": it["id"], "start": it["start"], "end": it["end"], "reason": f"title invalid: {exc}"})
            return False
        if t.lower() in used_titles:
            rejected.append({"id": it["id"], "start": it["start"], "end": it["end"], "reason": f"duplicate title {t!r}"})
            return False
        return True

    singles = [u for u in usable if u["format"] in ("funny", "continuous")]
    groups: dict[str, list] = {}
    for u in usable:
        if u["format"] == "ranking":
            groups.setdefault((u["category"] or "Wild Moments").strip(), []).append(u)
    ranking_videos, leftovers = [], []
    for cat, items in groups.items():                                  # RANKING PASS: every honest entry of a theme, never padded
        items = [i for i in sorted(items, key=lambda x: x["humor"]) if title_ok(i, "ranking") and not used_titles.add(i["title"].lower())]
        parts = _partition(len(items))
        pos, n_part = 0, 0
        for size in parts:
            if size < 0:
                leftovers += items[pos:]
                break
            chunk = items[pos:pos + size]
            pos += size
            n_part += 1
            ranking_videos.append({"category": cat if len(parts) == 1 else f"{cat} Part {n_part}", "items": chunk})
    for u in leftovers:                         # too few honest same-theme moments for a ranking: its best use is a standalone
        used_titles.discard((u["title"] or "").lower())
        u["format"] = "funny"
        singles.append(u)
    vids, counters = [], {"ranking": 0, "funny": 0, "continuous": 0}

    def moment(u, rank, a=None, b=None):
        toks = _tokens(u["cand"])
        a, b = (u["start"] if a is None else a), (u["end"] if b is None else b)
        return {"rank": rank, "title": u["title"], "start": a, "end": b, "verified": True, "segment": u["id"], "humor": u["humor"],
                "text_sha256": registry.text_fingerprint(toks), "sketch": registry.sketch(toks)}
    for rv in ranking_videos:
        counters["ranking"] += 1
        n = len(rv["items"])
        ms = [moment(u, n - i) for i, u in enumerate(rv["items"])]       # weakest first -> countdown to the strongest at #1
        for u in rv["items"]:
            used_titles.add(u["title"].lower())
        vids.append({"format": "ranking", "id": f"R{counters['ranking']:02d}", "category": rv["category"], "name": slug(rv["category"]), "moments": ms})
    for u in sorted(singles, key=lambda x: x["start"]):                  # STANDALONE PASS: every remaining strong moment, same level of care
        kind = u["format"]
        if not title_ok(u, kind):
            continue
        used_titles.add(u["title"].lower())
        counters[kind] += 1
        vids.append({"format": kind, "id": f"{PREFIX[kind]}{counters[kind]:02d}", "category": None, "name": slug(u["title"]), "moments": [moment(u, 1)]})
    # intentional reuse: a ranking entry may ALSO get a standalone only when the review records a materially different edit (fuller scene + reason)
    for rv, vid in zip(ranking_videos, [v for v in vids if v["format"] == "ranking"]):
        for u in rv["items"]:
            sr = u["review"].get("standalone_reuse")
            if not sr:
                continue
            kind = sr.get("format") if sr.get("format") in ("funny", "continuous") else "funny"
            a, b = sr.get("use_start", u["start"]), sr.get("use_end", u["end"])
            why = (sr.get("reason") or "").strip()
            try:
                (titles.validate_headline)(sr.get("title") or "")
            except titles.TitleError as exc:
                rejected.append({"id": u["id"], "start": a, "end": b, "reason": f"standalone reuse skipped, headline invalid: {exc}"})
                continue
            sim = plan_mod.footage_similarity([{"start": u["start"], "end": u["end"]}], [{"start": a, "end": b}])
            if not why or sim >= plan_mod.NEAR_IDENTICAL or (sr["title"].lower() in used_titles):
                rejected.append({"id": u["id"], "start": a, "end": b, "reason": "standalone reuse skipped: needs a reason, a materially different span and a new headline"})
                continue
            used_titles.add(sr["title"].lower())
            counters[kind] += 1
            nu = {**u, "title": sr["title"], "start": a, "end": b}
            vids.append({"format": kind, "id": f"{PREFIX[kind]}{counters[kind]:02d}", "category": None, "name": slug(sr["title"]), "moments": [moment(nu, 1)],
                         "reuse": {"of": vid["id"], "reason": why, "segment": u["id"], "ranking_span": [u["start"], u["end"]]}})
    conflict = lambda m: registry.conflicts(sha, m["start"], m["end"], m.get("text_sha256"), m.get("sketch"), root=root, ignore_run=run_id)
    res = plan_mod.validate([{"format": v["format"], "id": v["id"], "category": v["category"] or v["name"], "moments": v["moments"], **({"reuse": v["reuse"]} if v.get("reuse") else {})}
                             for v in vids], None, conflict)
    if res.errors:
        raise ContentError("plan rejected: " + "; ".join(res.errors[:5]))
    for fmt, fname in (("ranking", "rankings.json"), ("funny", "funny-moments.json"), ("continuous", "continuous-moments.json")):
        _write_json(d / "plan" / fname, [v for v in vids if v["format"] == fmt])
    _write_json(d / "plan" / "rejected.json", rejected)
    _write_json(d / "plan" / "edit-log.json", [{"segment": u["id"], "format": u["format"], "start": u["start"], "end": u["end"],
                                                 **{k: u["review"].get(k) for k in fullanalysis.EDITORIAL}} for u in usable])
    for v in vids:                                                       # runs/<run>/projects/{rankings,standalone}/<ID>-<name>.json: the edit decision list of each video
        _write_json(d / "projects" / ("rankings" if v["format"] == "ranking" else "standalone") / f"{v['id']}-{v['name']}.json", v)
    ledger = write_ledger(d, data["candidates"], vids, rejected, groups)
    return {"videos": vids, "rejected": rejected, "warnings": res.warnings, "ledger": ledger["summary"],
            "candidates": {"ranking": sum(1 for u in usable if u["format"] == "ranking"), "funny": sum(1 for u in usable if u["format"] == "funny"),
                           "continuous": sum(1 for u in usable if u["format"] == "continuous")}}


def write_ledger(d: Path, candidates: list[dict], vids: list[dict], rejected: list[dict], groups: dict) -> dict:
    """candidates/ledger.json: EVERY candidate with where it went (or why not), plus the two mandatory creative passes and any intentional reuse."""
    used: dict[str, list[str]] = {}
    for v in vids:
        for m in v["moments"]:
            used.setdefault(m["segment"], []).append(v["id"])
    why_not = {r["id"]: r["reason"] for r in rejected if r.get("id")}
    rows = []
    for c in candidates:
        rv = c.get("review") or {}
        rows.append({"id": c["id"], "start": c["start"], "end": c["end"], "duration_sec": c.get("duration"), "kind": c.get("kind"),
                     "review_status": rv.get("status"), "suggested_format": rv.get("suggested_format"), "hook": rv.get("hook"), "payoff": rv.get("payoff"),
                     "used_in": used.get(c["id"], []), "intentional_reuse": len(used.get(c["id"], [])) > 1,
                     "not_used_because": None if used.get(c["id"]) else (why_not.get(c["id"]) or rv.get("rejection_reason") or "not selected")})
    rk = [v for v in vids if v["format"] == "ranking"]
    sa = [v for v in vids if v["format"] != "ranking"]
    themes = {cat: len(items) for cat, items in groups.items()}
    passes = {"ranking_pass": {"videos": [v["id"] for v in rk], "themes_considered": themes,
                               "none_because": None if rk else ("no theme had at least %d honest entries: %s" % (style.load()["limits"]["min_ranks"], themes or "no ranking candidates"))},
              "standalone_pass": {"videos": [v["id"] for v in sa],
                                  "none_because": None if sa else "no remaining moment worked as a complete standalone (see not_used_because per candidate)"}}
    out = {"candidates": rows, "passes": passes,
           "summary": {"candidates": len(rows), "used": sum(1 for r in rows if r["used_in"]), "reused_in_two_videos": sum(1 for r in rows if r["intentional_reuse"]),
                       "ranking_videos": len(rk), "standalone_videos": len(sa)}}
    _write_json(d / "candidates" / "ledger.json", out)
    return out


LEDGER_STATUS = ("used_directly", "in_ranking", "in_standalone", "merged_into_scene", "rejected_after_recovery")


def reconcile_ledger(candidate_ids: list[str], rows: list[dict], out_dir: Path) -> dict:
    """candidates/recovery-ledger.json + .csv: EVERY segment exactly once with status, exact reason, audio verification method, neighbours inspected
    and final output filename. Refuses a ledger that omits, duplicates or leaves a segment unexplained; counts come from the rows themselves."""
    seen: dict[str, dict] = {}
    problems = []
    for r in rows:
        sid = r.get("segment")
        if sid in seen:
            problems.append(f"{sid} appears more than once")
        seen[sid] = r
        if r.get("status") not in LEDGER_STATUS:
            problems.append(f"{sid}: status must be one of {LEDGER_STATUS}")
        if not (r.get("reason") or "").strip():
            problems.append(f"{sid}: exact reason missing")
        if not (r.get("audio_verification") or "").strip() or not r.get("neighbors_inspected"):
            problems.append(f"{sid}: audio verification method and neighbouring segments inspected are required")
        if r.get("status") == "merged_into_scene" and not r.get("merged_into"):
            problems.append(f"{sid}: merged_into is required")
        if r.get("status") in ("used_directly", "in_ranking", "in_standalone") and not r.get("output"):
            problems.append(f"{sid}: a used segment needs its output filename")
    missing = [c for c in candidate_ids if c not in seen]
    extra = [c for c in seen if c not in candidate_ids]
    if missing:
        problems.append(f"missing segments: {missing}")
    if extra:
        problems.append(f"unknown segments: {extra}")
    if problems:
        raise ContentError("ledger rejected: " + "; ".join(problems[:8]))
    ordered = [seen[c] for c in candidate_ids]
    counts = {k: sum(1 for r in ordered if r["status"] == k) for k in LEDGER_STATUS}
    out = {"segments": len(ordered), "counts": counts, "rows": ordered}
    _write_json(out_dir / "recovery-ledger.json", out)
    import csv
    with (out_dir / "recovery-ledger.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["segment", "status", "reason", "audio_verification", "neighbors_inspected", "merged_into", "scene", "output"])
        for r in ordered:
            w.writerow([r["segment"], r["status"], r["reason"], r["audio_verification"], " ".join(r["neighbors_inspected"]), r.get("merged_into", ""), r.get("scene", ""), r.get("output", "")])
    return out


def load_plan(d: Path) -> list[dict]:
    out = []
    for fname in ("rankings.json", "funny-moments.json", "continuous-moments.json"):
        p = d / "plan" / fname
        if p.is_file():
            out += json.loads(p.read_text())
    return out


# ------------------------------------------------------------------ specs (captions from two-decode agreement only)
def _clip_evidence(d: Path, vid: dict, m: dict, media: Path, base_segs: list, root: Path, chat_step: float = 1.5) -> dict:
    cache = d / "working" / FORMAT_DIR[vid["format"]] / "cache" / f"{vid['id']}_{m['segment']}_{m['start']:.2f}_{m['end']:.2f}.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    dec = captions_mod.decode_moment(media, m["start"], m["end"], pad=3.0)
    sc = textsafety.scan_moment(media, m["start"], m["end"], step=chat_step, root=root)
    ev = {"decodes": dec["decodes"], "chat": {k: sc.get(k) for k in ("ocr_available", "samples", "text_boxes", "too_small_in_output", "chat_unverified",
                                                                     "verified_clean", "findings", "blocked_spans", "clean_spans", "note")}}
    _write_json(cache, ev)
    return ev


def _lines(dec: list, a: float, b: float, base_segs: list) -> list[tuple[str, float, float]]:
    ok = {w for s in base_segs if s["end"] >= a - 1.5 and s["start"] <= b + 1.5 for w in captions_mod.tokens(s["text"])}
    words = [w for w in captions_mod.agreed_words(dec[0], dec[1]) if a - 0.1 <= w["start"] and w["end"] <= b + 0.3 and w["w"] in ok]
    L, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > 0.6 or len(cur) >= 8):
            L.append(cur)
            cur = []
        cur.append(w)
    if cur:
        L.append(cur)
    out = []
    for ln in L:
        t = " ".join(x["w"] for x in ln)
        t = t[:1].upper() + t[1:]
        if not HALLUCINATION.search(t) and len(ln) >= 2:
            out.append((t, float(ln[0]["start"]), float(ln[-1]["end"])))
    return out


def build_specs(run_id: str, root=None, progress=print) -> list[dict]:
    """For every planned video: caption spec (+ labels) written to working/<format>/, chat scans and decodes cached. Returns per-video info."""
    root = _root(root)
    d = run_dir_for(run_id, root)
    src = _source(d)
    sid = src["source_id"]
    media = source_library.resolve(sid, root, require_active=False).media
    base = json.loads((_lib(sid, root) / "transcript" / "transcript.json").read_text())["segments"]
    info = []
    for vid in load_plan(d):
        wd = d / "working" / FORMAT_DIR[vid["format"]]
        stem = f"{vid['id']}-{vid['name']}"
        moms, labels, warn, nlines, chat = {}, [], False, 0, []
        for m in sorted(vid["moments"], key=lambda x: -x["rank"]):
            ev = _clip_evidence(d, vid, m, media, base, root)
            if ev["chat"].get("findings"):
                raise ContentError(f"severe on-screen language inside {m['segment']}; trim or reject it before rendering")
            L = _lines(ev["decodes"], m["start"], m["end"], base)
            warn |= any(PROFANITY.search(t) for t, _, _ in L)
            nlines += len(L)
            chat.append({"segment": m["segment"], **{k: ev["chat"].get(k) for k in ("chat_unverified", "verified_clean", "note")}})
            moms[str(m["rank"])] = {"label": m["title"], "start": m["start"], "end": m["end"], "decodes": ev["decodes"], "segment": m["segment"],
                                    "words": [[t, s, e] for t, s, e in L], "lines": [{"text": t, "first": i, "last": i} for i, (t, _, _) in enumerate(L)]}
            labels.append({"rank": m["rank"], "title": m["title"]})
        if vid["format"] == "ranking":
            spec = {"video_id": sid, "title": f"Ranking the {vid['category'].lower()}", "moments": moms}
        else:
            only = moms["1"]
            spec = {"video_id": sid, "title": vid["moments"][0]["title"], "headline": vid["moments"][0]["title"], "moments": {"1": only}}
        _write_json(wd / f"{stem}.spec.json", spec)
        _write_json(wd / f"{stem}.labels.json", sorted(labels, key=lambda x: -x["rank"]))
        info.append({"id": vid["id"], "stem": stem, "format": vid["format"], "warn_language": warn, "caption_lines": nlines, "chat": chat})
        progress(f"  specs ready: {stem} ({nlines} caption lines)")
    _write_json(d / "working" / "specs.json", info)
    return info


# ------------------------------------------------------------------ render
def render_all(run_id: str, root=None, progress=print, only: list[str] | None = None) -> list[dict]:
    root = _root(root)
    d = run_dir_for(run_id, root)
    sid = _source(d)["source_id"]
    out = []
    for sp in json.loads((d / "working" / "specs.json").read_text()):
        if only and sp["id"] not in only:
            continue
        wd = d / "working" / FORMAT_DIR[sp["format"]]
        mp4 = wd / f"{sp['stem']}.mp4"
        try:
            if mp4.exists():
                raise ContentError(f"{mp4.name} already exists; refusing to overwrite")
            if sp["format"] == "ranking":
                render.render_ranking(sid, wd / f"{sp['stem']}.spec.json", mp4, None, wd / f"{sp['stem']}.labels.json")
            else:
                render.render_standalone(sid, wd / f"{sp['stem']}.spec.json", mp4)
            out.append({"id": sp["id"], "stem": sp["stem"], "format": sp["format"], "render": str(mp4.relative_to(d)), "error": None})
        except Exception as exc:                            # one failed render never stops the others
            out.append({"id": sp["id"], "stem": sp["stem"], "format": sp["format"], "render": None, "error": f"{type(exc).__name__}: {exc}"})
        progress(f"  rendered {sp['stem']}: {'ok' if out[-1]['error'] is None else out[-1]['error']}")
    _write_json(d / "working" / "renders.json", out)
    return out


# ------------------------------------------------------------------ finalize / organize
def _style_hash() -> str:
    import hashlib
    S = style.load()
    keys = ("canvas", "framing", "background", "header", "badge", "label", "captions", "audio", "video_encode", "safe_margins")
    return hashlib.sha256(json.dumps({k: S[k] for k in keys}, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _offsets(moments: list[dict]) -> list[float]:
    t, out = 0.0, []
    for m in moments:
        out.append(round(t, 2))
        t += render.frame_exact({"start": m["start"], "end": m["end"]})
    return out


def _edit_field(d: Path, moments: list[dict], key: str) -> str:
    """hook/payoff text recorded in the review for the moments' segments (joined, first non-empty)."""
    p = d / "plan" / "edit-log.json"
    log = {e["segment"]: e for e in json.loads(p.read_text())} if p.is_file() else {}
    return " | ".join(filter(None, (str((log.get(m["segment"]) or {}).get(key) or "") for m in moments)))[:400]


def _edit_decisions(d: Path, moments: list[dict]) -> list[dict]:
    p = d / "plan" / "edit-log.json"
    log = {e["segment"]: e for e in json.loads(p.read_text())} if p.is_file() else {}
    return [{"segment": m["segment"], **{k: (log.get(m["segment"]) or {}).get(k) for k in ("treatment", "trim_reason", "duration_reason", "added_value", "edit_decisions", "runner_up")}}
            for m in moments]


def exports_path(root) -> Path:
    return _root(root) / "registry" / "exports.json"


def exports_load(root) -> list[dict]:
    p = exports_path(root)
    return json.loads(p.read_text()) if p.is_file() else []


def exports_find(root, digest: str, video_id: str, run_id: str) -> str | None:
    """A previously exported final with the same bytes under another name/run is an accidental duplicate export."""
    for e in exports_load(root):
        if e["sha256"] == digest and not (e["run_id"] == run_id and e["video_id"] == video_id) and (_root(root) / e["path"]).exists():
            return e["path"]
    return None


def exports_register(root, digest: str, video_id: str, run_id: str, path: str, timecodes) -> None:
    rows = [e for e in exports_load(root) if not (e["run_id"] == run_id and e["video_id"] == video_id)]
    rows.append({"sha256": digest, "run_id": run_id, "video_id": video_id, "path": path, "source_timecodes": timecodes})
    _write_json(exports_path(root), rows)


exports_mod_find = exports_find


def finalize_output(run_id: str, entry: dict, root=None, words_fn=None) -> dict:
    """Full QA on one render, then file it under approved_clips/{rankings,standalone/funny,standalone/continuous}/<run>/<ID-slug>/ or needs-review/<run>/<ID-slug>/ (with FAILURE.txt)."""
    root = _root(root)
    d = run_dir_for(run_id, root)
    src = _source(d)
    sid, sha = src["source_id"], src["sha256"]
    vid = next(v for v in load_plan(d) if v["id"] == entry["id"])
    stem, fmt = entry["stem"], vid["format"]
    wd = d / "working" / FORMAT_DIR[fmt]
    rec = {"id": entry["id"], "stem": stem, "format": fmt, "name": vid["name"], "category": vid["category"], "status": "render_failed", "error": entry.get("error")}
    if entry.get("error") or not entry.get("render"):
        return rec
    mp4 = d / entry["render"]
    spec = json.loads((wd / f"{stem}.spec.json").read_text())
    moments = sorted(vid["moments"], key=lambda m: -m["rank"])
    media = source_library.resolve(sid, root, require_active=False).media
    side = json.loads(Path(str(mp4) + ".render.json").read_text()) if Path(str(mp4) + ".render.json").is_file() else {}
    ranks = [str(m["rank"]) for m in moments]
    extra, reasons = [], []
    # cut-boundary / sync / edit-point QA
    offs = _offsets(moments)
    extra += cutqa.boundary_checks(media, moments, words_fn)
    extra += cutqa.sync_checks(mp4, media, moments, offs)
    extra += cutqa.edit_point_checks(mp4, offs)
    # caption / label / crop / duplicate / text-safety / template QA
    cap_problems = captions_mod.problems(spec)
    extra.append(qa.check("caption_wording_confirmed", qa.FAIL if cap_problems else qa.PASS, "; ".join(cap_problems[:3])))
    want = [m["title"] for m in moments]
    got = [spec["moments"][str(m["rank"])]["label"] for m in moments]
    head = spec.get("headline")
    label_ok = want == got and (fmt == "ranking" or head == want[0])
    extra.append(qa.check("label_matches_plan", qa.PASS if label_ok else qa.FAIL, f"plan {want}, render {got}" + (f", headline {head!r}" if head else "")))
    fr = framing.analyze(*(side.get("source_dims") or framing.reference()))
    extra.append(qa.check("crop_and_framing", qa.PASS if fr["ok"] and side.get("framing_ok", True) else qa.FAIL, fr["reason"] or f"source {fr['width']}x{fr['height']} mapped by ratios"))
    dup = [x for m in moments for x in registry.conflicts(sha, m["start"], m["end"], m.get("text_sha256"), m.get("sketch"), root=root, ignore_run=run_id)]
    mine = [(m["start"], m["end"]) for m in moments]
    partner = {(vid.get("reuse") or {}).get("of")} | {o["id"] for o in load_plan(d) if (o.get("reuse") or {}).get("of") == entry["id"]}
    for other in load_plan(d):                                  # no overlap with the run's OTHER videos either (except a recorded ranking<->standalone reuse)
        if other["id"] != entry["id"] and other["id"] not in partner:
            dup += [f"overlaps {other['id']}" for o in other["moments"] for a, b in mine if min(b, o["end"]) - max(a, o["start"]) > plan_mod.SHARED_BOUNDARY_SEC]
        elif other["id"] in partner and plan_mod.footage_similarity(vid["moments"], other["moments"]) >= plan_mod.NEAR_IDENTICAL:
            dup.append(f"near-identical timeline to {other['id']}")
    extra.append(qa.check("no_duplicate_moment", qa.FAIL if dup else qa.PASS, dup[0] if dup else "registry and reservations clear"))
    digest0 = cleanup.sha256(mp4)
    same = exports_mod_find(root, digest0, vid["id"], run_id)
    extra.append(qa.check("no_duplicate_export", qa.FAIL if same else qa.PASS, f"byte-identical to {same}" if same else "no identical earlier export"))
    gen = textsafety.scan_fields(textsafety.generated_text(None, spec))
    extra.append(qa.check("text_safety_generated", qa.FAIL if gen else qa.PASS, "; ".join(f"{x['field']}: {x['category']}" for x in gen[:3]) or "no severe language (profanity untouched)"))
    colors_used = side.get("label_colors")
    cprob = colors.validate(colors_used or {}, ranks) if colors_used else ["no accent color recorded by the renderer"]
    extra.append(qa.check("accent_color", qa.FAIL if cprob else qa.PASS, "; ".join(cprob) or f"one accent color {next(iter(colors_used.values()))}"))
    extra.append(qa.check("template_matches_style", qa.PASS if side.get("source_dims") else qa.FAIL, f"style {_style_hash()}; rendered from the approved template"))
    chat = []
    for m in moments:
        c = json.loads(next((wd / "cache").glob(f"{entry['id']}_{m['segment']}_*.json")).read_text())["chat"] if (wd / "cache").is_dir() else {}
        chat.append({"segment": m["segment"], **{k: c.get(k) for k in ("chat_unverified", "verified_clean", "note", "findings")}})
    bad_chat = [x for c in chat for x in (c.get("findings") or [])]
    extra.append(qa.check("text_safety_visible_chat", qa.FAIL if bad_chat else qa.PASS,
                          f"severe on-screen language at {bad_chat[0]['t']}s" if bad_chat else ("some chat too small to read in the output (recorded as unverified)" if any(c.get("chat_unverified") for c in chat) else "no severe on-screen text")))
    report = qa.run_qa(mp4, d / "qa" / "reports", [{"rank": m["rank"], "title": m["title"]} for m in moments] if fmt == "ranking" else None,
                       "ranking" if fmt == "ranking" else "headline", extra)
    ok, why = qa.auto_decision(report)
    reasons += why
    status_name = "ready_for_draft_transfer" if ok else "needs_review"
    rel = (Path(APPROVED_ROOT) / APPROVED_DIR[fmt] / run_id / stem) if ok else (Path("needs-review") / run_id / stem)
    dest = root / rel
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / f"{stem}.mp4"
    digest = cleanup.sha256(mp4)
    if final.exists() and cleanup.sha256(final) != digest:
        raise ContentError(f"{rel}/{final.name} already exists and differs; not overwritten")
    if not final.exists():
        try:
            os.link(mp4, final)
        except OSError:
            shutil.copy2(mp4, final)
    # cover, caption, qa, manifest
    cover = dest / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.2", "-i", str(final), "-frames:v", "1", "-vf", "scale=540:-1", "-q:v", "3", str(cover)], capture_output=True)
    warn = any(PROFANITY.search(l["text"]) for mo in spec["moments"].values() for l in mo["lines"])
    tags = ["#funnymoments", "#irl", "#streamer", f"#{re.sub(r'[^a-z]', '', (vid['category'] or fmt).split()[0].lower()) or 'clips'}", "#ranking" if fmt == "ranking" else "#shorts"]
    headline = (vid["category"] if fmt == "ranking" else want[0])
    caption = (f"Ranking the {vid['category'].lower()}" if fmt == "ranking" else want[0]) + (" - language warning" if warn else "") + " " + " ".join(tags)
    (dest / "caption.txt").write_text(caption + "\n")
    _write_json(dest / "qa.json", {**report, "needs_review_reasons": reasons, "boundary_and_sync": [c for c in extra if c["status"] != "pass"] or "all pass"})
    dur = round(sum(render.frame_exact(m) for m in moments), 2)
    manifest = {"video_file": final.name, "id": entry["id"], "run_id": run_id, "source_id": sid, "source_sha256": sha, "format": fmt, "status": status_name,
                "headline": headline, "category": vid["category"], "cover_title": headline, "caption": caption, "hashtags": tags, "duration_sec": dur,
                "rank_count": len(moments) if fmt == "ranking" else None, "accent_color": next(iter(colors_used.values())) if colors_used else None,
                "text_colors": colors_used, "style_hash": _style_hash(), "sha256": digest, "size_bytes": final.stat().st_size,
                "moments": [{"segment": m["segment"], "rank": m["rank"] if fmt == "ranking" else None, "title": m["title"], "start": m["start"], "end": m["end"],
                             "duration_sec": round(m["end"] - m["start"], 2), "clip_id": f"{sid}:{m['start']:.2f}-{m['end']:.2f}"} for m in moments],
                "qa_verdict": report["verdict"], "needs_review_reasons": reasons, "approval": qa.AUTO_APPROVED if ok else "needs_review", "manually_reviewed": False,
                "text_safety": {"chat": chat}, "public_publish": False,
                "transfer": {"status": status_name if ok else "not_ready", "mode": "UPLOAD_TO_DRAFT" if ok else None, "authorized": False,
                             "note": "no upload happens automatically; TikTok drafts need the user's separate authorization"}}
    reuse = vid.get("reuse")
    manifest.update({"output_filename": final.name, "source_timecodes": [[m["start"], m["end"]] for m in moments], "theme": vid["category"] if fmt == "ranking" else None,
                     "hook_used": _edit_field(d, moments, "hook") or None, "payoff": _edit_field(d, moments, "payoff") or None,
                     "editing_decisions": _edit_decisions(d, moments), "final_runtime_sec": dur, "candidate_ids": [m["segment"] for m in moments],
                     "intentional_reuse": reuse or next(({"reused_by": o["id"], "reason": (o["reuse"] or {}).get("reason")} for o in load_plan(d) if (o.get("reuse") or {}).get("of") == entry["id"]), None),
                     "qa_result": report["verdict"]})
    _write_json(dest / "manifest.json", manifest)
    if not ok:
        (dest / "FAILURE.txt").write_text("Needs review - this output failed automated QA or could not be verified.\n\n" + "\n".join(f"- {r}" for r in reasons or ["unspecified"]) + "\n")
    if ok:
        clips = [{"source_sha256": sha, "source_id": sid, "run_id": run_id, "video": final.name, "video_title": headline, "format": fmt,
                  "rank": m["rank"], "title": m["title"], "start": m["start"], "end": m["end"], "transcript_text_sha256": m.get("text_sha256"),
                  "sketch": m.get("sketch"), "transcript_file": f"{source_library.LIB}/{sid}/transcript/transcript.json", "status": qa.AUTO_APPROVED,
                  "approved_by_user_at": None, "approval": qa.AUTO_APPROVED} for m in moments]
        registry.register({"sha256": sha, "id": sid, "duration_sec": src["duration_sec"], "size_bytes": (root / source_library.LIB / sid / "master.mp4").stat().st_size,
                           "original_names": [src["source_name"]]}, clips, root)
    os.unlink(mp4) if mp4.exists() else None
    exports_register(root, digest, entry["id"], run_id, str(rel / final.name), manifest["source_timecodes"])
    rec.update(status=status_name if ok else "needs_review", dir=str(rel), final=str(rel / final.name), duration_sec=dur, headline=headline, caption=caption,
               accent_color=manifest["accent_color"], qa_verdict=report["verdict"], needs_review_reasons=reasons, error=None,
               clip_ids=[x["clip_id"] for x in manifest["moments"]], source_ranges=[[m["start"], m["end"]] for m in moments], rank_count=manifest["rank_count"])
    return rec


def finalize_all(run_id: str, root=None, progress=print, words_fn=None) -> list[dict]:
    root = _root(root)
    d = run_dir_for(run_id, root)
    recs = []
    for e in json.loads((d / "working" / "renders.json").read_text()):
        try:
            r = finalize_output(run_id, e, root, words_fn)
        except Exception as exc:
            r = {"id": e["id"], "stem": e["stem"], "format": e["format"], "status": "needs_review", "error": f"{type(exc).__name__}: {exc}", "needs_review_reasons": [str(exc)]}
        recs.append(r)
        progress(f"  {e['stem']}: {r['status']}")
    _write_json(d / "outputs.json", recs)
    return recs


# ------------------------------------------------------------------ dashboard
def build_dashboard(run_id: str, root=None) -> Path:
    """Self-contained local dashboard (no server, no external assets): approved_clips/_reports/<source>/<run>/index.html + summary.json."""
    root = _root(root)
    d = run_dir_for(run_id, root)
    src = _source(d)
    sid = src["source_id"]
    out_dir = root / APPROVED_ROOT / "_reports" / sid / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = json.loads((d / "outputs.json").read_text()) if (d / "outputs.json").is_file() else []
    status = json.loads((d / "status.json").read_text())
    ready = [r for r in recs if r["status"] == "ready_for_draft_transfer"]
    review = [r for r in recs if r["status"] != "ready_for_draft_transfer"]
    by_fmt = {k: sum(1 for r in ready if r["format"] == k) for k in FORMAT_DIR}
    cards = []
    for r in ready:
        man = json.loads((root / r["dir"] / "manifest.json").read_text())
        cov = root / r["dir"] / "cover.jpg"
        img = ("data:image/jpeg;base64," + base64.b64encode(cov.read_bytes()).decode()) if cov.is_file() else ""
        rel = os.path.relpath(root / r["final"], out_dir)
        ts = ", ".join(f"{m['start']:.0f}-{m['end']:.0f}s" for m in man["moments"])
        cards.append(f'<div class="card"><img src="{img}" alt=""><div class="b"><span class="tag" style="background:{man["accent_color"] or "#fff"}">{html.escape(FORMAT_DIR[man["format"]][:-1].replace("-", " "))}</span>'
                     f'<h3>{html.escape(man["id"])} - {html.escape(man["headline"])}</h3><p>{man["duration_sec"]} s · QA {html.escape(man["qa_verdict"])}'
                     f'{" · " + str(man["rank_count"]) + " ranks" if man["rank_count"] else ""} · accent {man["accent_color"]}</p>'
                     f'<p class="s">source {html.escape(ts)}</p><p class="c">{html.escape(man["caption"])}</p><p>cover title: {html.escape(man["cover_title"])}</p>'
                     f'<a href="{html.escape(rel)}">open {html.escape(Path(r["final"]).name)}</a></div></div>')
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(src['source_name'])} - {run_id}</title><style>
body{{font:15px system-ui,sans-serif;margin:24px;background:#111;color:#eee}}h1{{margin:0 0 6px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
.card{{background:#1c1c1e;border-radius:12px;overflow:hidden}}.card img{{width:100%;display:block;background:#000}}.b{{padding:10px 12px}}h3{{margin:6px 0}}
.tag{{color:#111;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700;text-transform:uppercase}}p{{margin:4px 0;color:#bbb}}.s{{color:#8ab}}.c{{color:#ddd;font-size:13px}}
a{{color:#7cf}}.k{{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0}}.k div{{background:#1c1c1e;padding:10px 16px;border-radius:10px}}b{{font-size:20px}}</style></head><body>
<h1>{html.escape(src['source_name'])}</h1><p>Source <code>{sid}</code> · {src['duration_sec']:.0f} s · run <code>{run_id}</code> · stage <b>{status['stage']}</b></p>
<div class="k"><div>Segments detected<br><b>{status['segments_detected']}</b></div><div>Rankings<br><b>{by_fmt['ranking']}</b></div><div>Funny moments<br><b>{by_fmt['funny']}</b></div>
<div>Continuous moments<br><b>{by_fmt['continuous']}</b></div><div>Needs review<br><b>{len(review)}</b></div><div>Rejected candidates<br><b>{status['rejected_candidates']}</b></div>
<div>Cleanup<br><b>{html.escape(str(status['cleanup']))}</b></div></div>
<h2>Ready for drafts ({len(ready)})</h2><div class="grid">{''.join(cards) or '<p>nothing ready</p>'}</div>
<h2>Needs review ({len(review)})</h2><ul>{''.join(f"<li>{html.escape(r['stem'])}: {html.escape('; '.join(r.get('needs_review_reasons') or [str(r.get('error'))])[:200])}</li>" for r in review) or '<li>none</li>'}</ul>
<p>Status: ready_for_draft_transfer = passed QA, not uploaded, not published.</p></body></html>"""
    (out_dir / "index.html").write_text(page)
    summary = {"source_id": sid, "source_name": src["source_name"], "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
               "counts": {"ready_for_drafts": len(ready), "by_format": by_fmt, "needs_review": len(review), "segments_detected": status["segments_detected"],
                          "rejected_candidates": status["rejected_candidates"]},
               "outputs": [{"id": r["id"], "format": r["format"], "path": r.get("final"), "headline": r.get("headline"), "duration_sec": r.get("duration_sec"),
                            "status": r["status"], "source_ranges": r.get("source_ranges"), "clip_ids": r.get("clip_ids")} for r in recs]}
    _write_json(out_dir / "summary.json", summary)
    return out_dir / "index.html"


# ------------------------------------------------------------------ cleanup
CLEAN_DIRS = ("working", "segments/shortlisted-previews", "qa/frames", "ingest/tmp")


def clean_run(run_id: str, root=None, trash_dir=None) -> dict:
    """Move ONLY allowlisted temporary files of this run to the macOS Trash. Never touches sources, catalogs, finals, records or other runs."""
    root = _root(root)
    d = run_dir_for(run_id, root)
    res = {"result": "completed", "moved": 0, "bytes": 0, "problems": []}
    try:
        trash = Path(trash_dir) if trash_dir else cleanup.default_trash()
        if not trash.is_dir() or not os.access(trash, os.W_OK):
            raise cleanup.CleanupError(f"Trash is unavailable ({trash})")
        if os.stat(trash).st_dev != os.stat(d).st_dev:
            raise cleanup.CleanupError("Trash is on a different volume; a move would need a copy")
    except cleanup.CleanupError as exc:
        return {"result": "failed", "moved": 0, "bytes": 0, "problems": [f"cleanup failed, files left in place: {exc}"]}
    for sub in CLEAN_DIRS:
        base = d / sub
        if base.is_symlink() or not base.is_dir():
            continue
        for dp, dns, fns in os.walk(base, followlinks=False):
            for n in fns:
                p = Path(dp) / n
                if p.is_symlink():
                    continue
                try:
                    st = p.stat()
                    dst = cleanup.unique_trash_name(trash, f"{run_id}__{p.relative_to(d).as_posix().replace('/', '__')}")
                    os.rename(p, dst)
                    res["moved"] += 1
                    res["bytes"] += st.st_size
                except OSError as exc:
                    res["problems"].append(f"not moved: {p.relative_to(d)}: {exc}")
    # sampled evidence frames from the full analysis are temporary working frames too
    sampled = _lib(_source(d)["source_id"], root) / "analysis" / "thumbnails" / "_sampled"
    if sampled.is_dir() and not sampled.is_symlink():
        for dp, dns, fns in os.walk(sampled, followlinks=False):
            for n in fns:
                p = Path(dp) / n
                try:
                    st = p.stat()
                    dst = cleanup.unique_trash_name(trash, f"{run_id}__sampled__{n}")
                    os.rename(p, dst)
                    res["moved"] += 1
                    res["bytes"] += st.st_size
                except OSError as exc:
                    res["problems"].append(f"not moved: {n}: {exc}")
    for sub in CLEAN_DIRS:                         # only empty directories are removed
        base = d / sub
        if base.is_dir() and not base.is_symlink():
            for dp, _, _ in os.walk(base, topdown=False, followlinks=False):
                if dp != str(base) and not os.listdir(dp):
                    os.rmdir(dp)
    if res["problems"]:
        res["result"] = "partial"
    _write_json(d / "logs" / "cleanup.json", res)
    return res


# ------------------------------------------------------------------ report
def report(run_id: str, root=None, command: str = "") -> str:
    root = _root(root)
    d = run_dir_for(run_id, root)
    st = json.loads((d / "status.json").read_text())
    src = _source(d)
    recs = json.loads((d / "outputs.json").read_text()) if (d / "outputs.json").is_file() else []
    rej = json.loads((d / "plan" / "rejected.json").read_text()) if (d / "plan" / "rejected.json").is_file() else []
    reasons: dict[str, int] = {}
    for r in rej:
        k = re.sub(r"\d+(\.\d+)?", "#", r["reason"])[:60]
        reasons[k] = reasons.get(k, 0) + 1
    ready = [r for r in recs if r["status"] == "ready_for_draft_transfer"]
    L = [f"CONTENT REPORT - run {run_id} - source {src['source_id']} ({src['source_name']})", f"Command: {command}",
         f"Segments detected: {st['segments_detected']} · analyzed {st['segments_analyzed']}",
         f"Candidates: ranking {st['ranking_candidates']}, funny {st['funny_candidates']}, continuous {st['continuous_candidates']}, rejected {st['rejected_candidates']}",
         "Main rejection reasons: " + ("; ".join(f"{k} x{n}" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]) or "none"),
         f"Videos: {len(ready)} approved (rankings {sum(r['format'] == 'ranking' for r in ready)}, funny {sum(r['format'] == 'funny' for r in ready)}, "
         f"continuous {sum(r['format'] == 'continuous' for r in ready)}; runtime is recorded, never a pass/fail factor), {len(recs) - len(ready)} needing review"]
    L += [f"  {r['id']} [{r['format']}] {r.get('headline')} - {r.get('duration_sec')}s - accent {r.get('accent_color')} - QA {r.get('qa_verdict')} - {r.get('final')}" for r in ready]
    L += [f"  NEEDS REVIEW {r['stem']}: {'; '.join(r.get('needs_review_reasons') or [str(r.get('error'))])[:160]}" for r in recs if r["status"] != "ready_for_draft_transfer"]
    L += [f"Approved (ready for your review / TikTok drafts): approved_clips/rankings/{run_id}/ and approved_clips/standalone/{{funny,continuous}}/{run_id}/", f"Dashboard: approved_clips/_reports/{src['source_id']}/{run_id}/index.html",
          f"Cleanup: {st['cleanup']}", f"Source protection: {src['master']} preserved (never purged)"]
    lg = d / "candidates" / "ledger.json"
    if lg.is_file():
        led = json.loads(lg.read_text())
        L.insert(3, f"Candidate ledger: {led['summary']['candidates']} candidates, {led['summary']['used']} used, {led['summary']['reused_in_two_videos']} intentionally reused (runs/{run_id}/candidates/ledger.json)")
        for nm, ps in led["passes"].items():
            L.insert(4, f"{nm.replace('_', ' ').title()}: {len(ps['videos'])} video(s)" + (f" - none because: {ps['none_because']}" if ps["none_because"] else ""))
    L += [f"Warning: {w}" for w in st["warnings"]] + [f"Error: {e}" for e in st["errors"]]
    text = "\n".join(L)
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / f"{run_id}-report.txt").write_text(text + "\n")
    return text
