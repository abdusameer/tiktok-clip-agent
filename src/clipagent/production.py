"""Unattended local production: auto-QA gate, approved/ storage, permanent registration, recoverable run cleanup, final report.

Nothing here uploads, publishes, spends credits or purges a source. Approval is `auto_qa_approved` (automated QA only),
never "manually reviewed". Cleanup only MOVES allowlisted run-specific files to the macOS Trash.
"""
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import cleanup, config, framing, qa, registry, runs, source_library, style, textsafety, titles

LOG = "production.json"
HASHTAG = re.compile(r"#\w+")


class ProductionError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cfg() -> dict:
    return style.load()["production"]


def _root(root) -> Path:
    return Path(root) if root else config.project_root()


def _load_log(run_dir: Path) -> dict:
    p = run_dir / LOG
    return json.loads(p.read_text()) if p.is_file() else {"run_id": run_dir.name, "videos": [], "errors": [], "cleanup": None}


def _save_log(run_dir: Path, log: dict) -> None:
    cleanup.write_json_atomic(run_dir / LOG, log)


def clip_id(source_id: str, start: float, end: float) -> str:
    return f"{source_id}:{start:.2f}-{end:.2f}"


def source_record(source_id: str, root: Path) -> dict:
    """The registry's `sources` entry for a library source (built from its source.json)."""
    info = json.loads((root / source_library.LIB / source_id / "source.json").read_text())
    return {"sha256": info["sha256"], "id": source_id, "duration_sec": info["master"]["duration_sec"],
            "size_bytes": info["master"]["size_bytes"], "original_names": [i["filename"] for i in info.get("inputs", [])]}


def _check_meta(fmt: str, meta: dict) -> None:
    if fmt not in ("ranking", "standalone"):
        raise ProductionError(f"unknown format {fmt!r}")
    tags = meta.get("hashtags") or []
    if not 4 <= len(tags) <= 5 or not all(HASHTAG.fullmatch(t if t.startswith("#") else "#" + t) for t in tags):
        raise ProductionError("need 4-5 hashtags")
    for k in ("video_title", "cover_title", "caption", "source_id", "moments"):
        if not meta.get(k):
            raise ProductionError(f"meta is missing {k!r}")
    if len(meta["caption"]) > 150:
        raise ProductionError("caption longer than 150 characters")
    ms = meta["moments"]
    if fmt == "standalone":
        if len(ms) != 1:
            raise ProductionError("a standalone video has exactly one moment")
        titles.validate_title(ms[0]["title"])
    else:
        titles.validate_entries([{"rank": m["rank"], "title": m["title"]} for m in ms])


def finalize_video(run_id: str, render_rel: str, fmt: str, meta: dict, root=None) -> dict:
    """QA a finished render, then file it under approved/<run-id>/ (auto-QA passed) or needs-review/<run-id>/ (anything uncertain).

    Never raises for a single bad video: the failure is recorded in runs/<id>/production.json and the caller continues.
    """
    root = _root(root)
    run_dir = cleanup.run_dir_for(run_id, root)
    log = _load_log(run_dir)
    rec = {"render": render_rel, "format": fmt, "video_title": meta.get("video_title"), "source_id": meta.get("source_id"),
           "status": "render_failed", "at": _now()}
    try:
        _check_meta(fmt, meta)
        src = cleanup.no_symlink_dirs(run_dir, cleanup.rel_parts(render_rel))
        if src.is_symlink() or not src.is_file() or src.stat().st_size == 0:
            raise ProductionError(f"render missing or empty: {render_rel}")
        entries = ([{"rank": m["rank"], "title": m["title"]} for m in meta["moments"]] if fmt == "ranking" else None)
        report = qa.run_qa(src, run_dir / "qa", entries, fmt)
        ok, reasons = qa.auto_decision(report)
        sinfo = json.loads((root / source_library.LIB / meta["source_id"] / "source.json").read_text())["master"]
        w, h = sinfo.get("width"), sinfo.get("height")
        if not (w and h):                                  # legacy record without dimensions: measure the master instead of guessing
            mp = root / source_library.LIB / meta["source_id"] / "master.mp4"
            v = source_library.probe_media(mp)["video"] if mp.is_file() else None
            w, h = (v["width"], v["height"]) if v else (None, None)
        fr = framing.analyze(w, h)
        if not fr["ok"]:                                   # never approve an output whose source could not be mapped safely
            ok = False
            reasons = reasons + [f"source framing: {fr['reason']}"]
        tsafe = {"generated_text": "clean", "chat": []}
        gen = textsafety.scan_fields(textsafety.generated_text(meta))
        if gen:
            ok = False
            tsafe["generated_text"] = gen
            reasons = reasons + [f"text safety: {x['field']} contains {x['category']}" for x in gen]
        master = root / source_library.LIB / meta["source_id"] / "master.mp4"
        if master.is_file() and not meta.get("skip_chat_scan"):
            for m in meta["moments"]:
                sc = textsafety.scan_moment(master, m["start"], m["end"], root=root, src_dims=(w, h))
                tsafe["chat"].append({"start": m["start"], "end": m["end"], **{k: sc.get(k) for k in (
                    "ocr_available", "samples", "text_boxes", "too_small_in_output", "chat_unverified", "verified_clean", "findings", "note")}})
                if sc.get("findings"):
                    ok = False
                    reasons = reasons + [f"text safety: severe on-screen language at {x['t']}s ({x['category']})" for x in sc["findings"][:3]]
        side_src = Path(str(src) + ".render.json")                       # written by the renderer (colors, seed, framing)
        side = json.loads(side_src.read_text()) if side_src.is_file() else {}
        colors_used = side.get("label_colors") if fmt == "ranking" else None
        if fmt == "ranking" and style.load()["label"]["title_accent"]["enabled"]:
            from . import colors as colors_mod
            ranks = [str(m["rank"]) for m in sorted(meta["moments"], key=lambda m: -m["rank"])]
            problems = colors_mod.validate(colors_used or {}, ranks) if colors_used else ["no title colors were recorded by the renderer"]
            if problems:
                ok = False
                reasons = reasons + [f"title colors: {x}" for x in problems]
        rec.update(status="store_failed", qa_verdict=report["verdict"], needs_review_reasons=reasons)
        bucket = _cfg()["approved_dir"] if ok else _cfg()["needs_review_dir"]
        dest_dir = root / bucket / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        digest = cleanup.sha256(src)
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink() or cleanup.sha256(dest) != digest:
                raise ProductionError(f"{bucket}/{run_id}/{dest.name} already exists and differs; not overwritten")
        else:
            try:
                os.link(src, dest)
            except OSError:
                shutil.copy2(src, dest)
        if cleanup.sha256(dest) != digest:
            raise ProductionError("stored copy failed checksum verification; render left in place")
        os.unlink(src)       # the run no longer holds the final (hard link => no extra space)
        qa_json = run_dir / "qa" / f"{src.stem}.qa.json"
        for f in (qa_json, run_dir / "qa" / f"{src.stem}.contact.png", side_src):
            if f.is_file() and not (dest_dir / f.name).exists():
                shutil.copy2(f, dest_dir / f.name)
        tags = [t if t.startswith("#") else f"#{t}" for t in meta["hashtags"]]
        approval = qa.AUTO_APPROVED if ok else "needs_review"
        manifest = {"video_file": dest.name, "run_id": run_id, "format": fmt, "source_id": meta["source_id"],
                    "video_title": meta["video_title"], "cover_title": meta["cover_title"], "caption": meta["caption"], "hashtags": tags,
                    "moments": [{"clip_id": clip_id(meta["source_id"], m["start"], m["end"]), "rank": m["rank"] if fmt == "ranking" else None,
                                 "title": m["title"], "start": m["start"], "end": m["end"],
                                 "duration_sec": round(m["end"] - m["start"], 2)} for m in meta["moments"]],
                    "duration_sec": round(sum(m["end"] - m["start"] for m in meta["moments"]), 2),
                    "size_bytes": dest.stat().st_size, "sha256": digest, "qa_verdict": report["verdict"], "qa_report": qa_json.name,
                    "category": meta.get("category") or meta["video_title"], "text_colors": colors_used, "color_seed": side.get("color_seed"),
                    "status": "ready_for_draft_transfer" if ok else "needs_review",
                    "approval": approval, "manually_reviewed": False, "approved_by_user": False, "text_safety": tsafe,
                    "needs_review_reasons": reasons, "approved_at": _now() if ok else None,
                    "public_publish": False,
                    "transfer": {"status": "ready_for_draft_transfer" if ok else "not_ready", "mode": "UPLOAD_TO_DRAFT" if ok else None,
                                 "authorized": False, "note": "no upload happens automatically; TikTok drafts need the user's separate authorization"}}
        runs.write_new(dest_dir / f"{src.stem}.manifest.json", json.dumps(manifest, indent=2))
        rec.update(final=str(dest.relative_to(root)), sha256=digest, approval=approval, format=fmt, category=manifest["category"],
                   duration_sec=manifest["duration_sec"], text_colors=colors_used, qa_status=report["verdict"],
                   clip_ids=[m["clip_id"] for m in manifest["moments"]], qa_checks=[f"{c['status']}:{c['check']}" for c in report["checks"]])
        if ok:
            clips = [{"source_sha256": source_record(meta["source_id"], root)["sha256"], "source_id": meta["source_id"], "run_id": run_id,
                      "video": dest.name, "video_title": meta["video_title"], "format": fmt,
                      "rank": m["rank"] if fmt == "ranking" else 1, "title": m["title"], "start": m["start"], "end": m["end"],
                      "transcript_text_sha256": m.get("text_sha256"), "sketch": m.get("sketch"),
                      "transcript_file": f"source_library/{meta['source_id']}/transcripts/{meta['source_id']}.json",
                      "status": qa.AUTO_APPROVED, "approved_by_user_at": None, "approval": qa.AUTO_APPROVED}
                     for m in meta["moments"]]
            registry.register(source_record(meta["source_id"], root), clips, root)
        rec["status"] = "approved" if ok else "needs_review"
    except Exception as exc:    # one bad output never stops the run
        rec["error"] = f"{type(exc).__name__}: {exc}"
        log["errors"].append(f"{render_rel}: {rec['error']}")
    log["videos"].append(rec)
    _save_log(run_dir, log)
    return rec


def fix_hints(report: dict) -> dict:
    """Automatic corrections for fixable QA failures (frozen/black frames at the very start or end -> trim). Used by the unattended
    retry loop: re-render under a NEW name with the trimmed boundary (max 2 retries), then finalize again. Anything else is not fixable."""
    import ast
    dur = None
    for c in report["checks"]:
        if c["check"] == "duration_within_platform_limit":
            dur = float(c["detail"].split("s")[0])
    hints, fixable = [], True
    for c in report["checks"]:
        if c["status"] != "fail":
            continue
        if c["check"] in ("no_frozen_frames", "no_black_frames") and dur:
            for a, d in ast.literal_eval(c["detail"] or "[]"):
                if a <= 2.0:
                    hints.append({"action": "trim_start", "seconds": round(a + d + 0.1, 2), "because": c["check"]})
                elif a + d >= dur - 2.0:
                    hints.append({"action": "trim_end", "seconds": round(dur - a + 0.1, 2), "because": c["check"]})
                else:
                    fixable = False
                    hints.append({"action": "none", "because": f"{c['check']} in the middle of the clip at {a}s"})
        else:
            fixable = False
            hints.append({"action": "none", "because": f"{c['check']} is not automatically fixable"})
    return {"fixable": bool(hints) and fixable, "hints": hints, "max_retries": 2}


# ------------------------------------------------------------------ cleanup (recoverable, allowlisted, run-specific)
def _verify_master(source_id: str, root: Path) -> None:
    """Separate input copies may only go once the canonical master is fully verified."""
    info = json.loads((root / source_library.LIB / source_id / "source.json").read_text())
    master = root / source_library.LIB / source_id / "master.mp4"
    if master.is_symlink() or not master.is_file():
        raise ProductionError("canonical master.mp4 missing")
    if source_library.sha256_file(master) != info["sha256"]:
        raise ProductionError("master.mp4 does not match its recorded sha256")
    source_library.validate_master(master, info["master"]["duration_sec"])
    if info["sha256"] not in registry.load(root)["sources"]:
        raise ProductionError("source is not in the permanent registry")


def _input_copies(source_id: str, root: Path) -> list[Path]:
    """Separate download copies recorded at ingest: a plain file directly inside source_videos/ or the source_library/ root,
    byte-identical (sha256) to what was ingested. Never anything inside a source's own folder."""
    info = json.loads((root / source_library.LIB / source_id / "source.json").read_text())
    out = []
    for i in info.get("inputs", []):
        for base in (root / "source_videos", root / source_library.LIB):
            p = base / i["filename"]
            if p.is_file() and not p.is_symlink() and p.parent == base and cleanup.sha256(p) == i["sha256"]:
                out.append(p)
                break
    return out


def auto_cleanup(run_id: str, root=None, trash_dir=None, source_id: str | None = None, include_inputs: bool = False) -> dict:
    """After production: release reservations and move allowlisted run files (and, if verified, input copies) to the Trash.

    Does nothing (and says why) unless every produced video is safely stored. If the Trash is unavailable the files are left in
    place and the failure is recorded. Never touches finals, the master, transcripts/analysis, metadata, registry, code or config.
    """
    root = _root(root)
    run_dir = cleanup.run_dir_for(run_id, root)
    log = _load_log(run_dir)
    result = {"run_id": run_id, "result": "skipped", "moved": [], "bytes": 0, "problems": []}
    registry.release(run_id, root)

    def finish(**kw):
        result.update(kw)
        log["cleanup"] = {**{k: result[k] for k in ("result", "bytes", "problems")}, "moved": [m["rel"] for m in result["moved"]], "at": _now()}
        _save_log(run_dir, log)
        return result

    if not log["videos"]:
        return finish(reason="no videos were produced")
    reg = registry.load(root)
    for v in log["videos"]:
        if v["status"] == "store_failed":
            return finish(reason=f"a video could not be stored safely ({v['render']}); cleanup skipped")
        if v["status"] in ("approved", "needs_review"):
            f = root / v["final"]
            if f.is_symlink() or not f.is_file() or cleanup.sha256(f) != v["sha256"]:
                return finish(reason=f"stored final failed verification: {v['final']}")
        if v["status"] == "approved":
            have = {(c["start"], c["end"]) for c in reg["clips"] if c["run_id"] == run_id and c.get("video") == Path(v["final"]).name}
            if len(have) < len(v["clip_ids"]):
                return finish(reason=f"moments of {v['final']} are not yet in the permanent registry; cleanup skipped")
    try:
        trash = Path(trash_dir) if trash_dir else cleanup.default_trash()
        if not trash.is_dir() or trash.is_symlink() or not os.access(trash, os.W_OK):
            raise cleanup.CleanupError(f"Trash is unavailable ({trash})")
        if os.stat(trash).st_dev != os.stat(run_dir).st_dev:
            raise cleanup.CleanupError("Trash is on a different volume; a move would need a copy")
    except cleanup.CleanupError as exc:
        result["problems"].append(f"cleanup failed, files left in place: {exc}")
        return finish(result="failed", reason=str(exc))

    targets = []
    for rel in cleanup.build_eligible(run_dir, ""):
        parts = cleanup.rel_parts(rel)
        p = cleanup.no_symlink_dirs(run_dir, parts)
        if parts[0] in cleanup.ELIGIBLE_DIRS and not cleanup.is_protected(parts) and (p.is_symlink() or p.is_file()):
            targets.append({"rel": rel, "path": p, "bytes": 0 if p.is_symlink() else p.stat().st_size, "name": f"{run_id}__{rel.replace(os.sep, '__')}"})
    if include_inputs and source_id:
        try:
            _verify_master(source_id, root)
            for p in _input_copies(source_id, root):
                targets.append({"rel": str(p.relative_to(root)), "path": p, "bytes": p.stat().st_size, "name": f"{run_id}__input__{p.name}"})
        except Exception as exc:
            result["problems"].append(f"input copies kept (master not verified): {exc}")
    seen = set()
    for t in targets:
        try:
            st = t["path"].lstat()
            key = (st.st_dev, st.st_ino)
            if key in seen:
                t["bytes"] = 0
            seen.add(key)
            dst = cleanup.unique_trash_name(trash, t["name"])
            os.rename(t["path"], dst)
            result["moved"].append({"rel": t["rel"], "bytes": t["bytes"], "trash_name": dst.name})
            result["bytes"] += t["bytes"]
        except OSError as exc:
            result["problems"].append(f"not moved: {t['rel']}: {exc}")
    for x in cleanup.ELIGIBLE_DIRS:         # remove only directories left empty
        d = run_dir / x
        if d.is_dir() and not d.is_symlink():
            for dp, _dns, _fns in os.walk(d, topdown=False, followlinks=False):
                if not Path(dp).is_symlink() and not os.listdir(dp):
                    os.rmdir(dp)
    return finish(result="completed" if not result["problems"] else "partial")


# ------------------------------------------------------------------ final report
def remaining_candidates(source_id: str, root: Path) -> list[dict]:
    p = root / source_library.LIB / source_id / "analysis" / f"{source_id}.candidates.json"
    if not p.is_file():
        return []
    sha = json.loads((root / source_library.LIB / source_id / "source.json").read_text())["sha256"]
    out = []
    for c in json.loads(p.read_text()).get("candidates", []):
        if not registry.conflicts(sha, c["start"], c["end"], root=root):
            out.append({"clip_id": clip_id(source_id, c["start"], c["end"]), "score": c.get("score"), "risk": c.get("policy_risk"),
                        "text": (c.get("text") or "")[:70]})
    return sorted(out, key=lambda c: -(c["score"] or 0))


def report(run_id: str, root=None) -> dict:
    root = _root(root)
    run_dir = cleanup.run_dir_for(run_id, root)
    log = _load_log(run_dir)
    vids = log["videos"]
    sid = next((v["source_id"] for v in vids if v.get("source_id")), None)
    cl = log.get("cleanup") or {}
    return {"run_id": run_id, "source_id": sid, "videos": vids, "errors": log["errors"], "cleanup": cl,
            "approved": [v for v in vids if v["status"] == "approved"], "needs_review": [v for v in vids if v["status"] == "needs_review"],
            "failed": [v for v in vids if v["status"] in ("render_failed", "store_failed")],
            "remaining": remaining_candidates(sid, root) if sid else [],
            "master": f"source_library/{sid}/master.mp4" if sid else None}


def format_report(r: dict) -> str:
    L = [f"PRODUCTION REPORT  run {r['run_id']}  source {r['source_id']}",
         f"Videos created: {len(r['approved']) + len(r['needs_review'])}  (auto-QA approved {len(r['approved'])}, needs review {len(r['needs_review'])}, failed {len(r['failed'])})"]
    fm = {}
    for v in r["approved"] + r["needs_review"]:
        fm[v["format"]] = fm.get(v["format"], 0) + 1
    L.append("Formats: " + (", ".join(f"{k} x{n}" for k, n in fm.items()) or "none"))
    for v in r["approved"]:
        col = ", ".join(f"#{k} {c}" for k, c in sorted((v.get("text_colors") or {}).items(), key=lambda kv: -int(kv[0]))) or "n/a"
        L.append(f"  READY FOR DRAFT TRANSFER (auto_qa_approved, not manually reviewed): {v['final']}  [{v['format']}]  "
                 f"category: {v.get('category')}  duration: {v.get('duration_sec')}s  QA: {v.get('qa_status')}\n"
                 f"      title colors: {col}\n      clips: {', '.join(v['clip_ids'])}")
    for v in r["needs_review"]:
        L.append(f"  NEEDS REVIEW: {v['final']}  [{v['format']}]  why: {'; '.join(v['needs_review_reasons'])}")
    for v in r["failed"]:
        L.append(f"  FAILED: {v['render']}  {v.get('error', '')}")
    L.append("QA: " + (", ".join(f"{Path(v['final']).name}={v['qa_verdict']}" for v in r["approved"] + r["needs_review"]) or "n/a")
             + "  (visual items remain manual_review by design)")
    rem = r["remaining"]
    L.append(f"Remaining unused candidates: {len(rem)}" + "".join(f"\n  {c['clip_id']} score {c['score']} risk {c['risk']} - {c['text']}" for c in rem[:10]))
    cl = r["cleanup"] or {}
    L.append(f"Cleanup: {cl.get('result', 'not run')}; {len(cl.get('moved', []))} files, {cl.get('bytes', 0):,} B moved to Trash (recoverable)")
    L.append(f"Protected source: {r['master']}")
    L += [f"Warning: {e}" for e in r["errors"]] + [f"Warning: {p}" for p in cl.get("problems", [])]
    return "\n".join(L)
