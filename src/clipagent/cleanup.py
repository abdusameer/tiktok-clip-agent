"""Deterministic, recoverable cleanup of one completed ranking run.

Every decision comes from runs/<id>/completion.json (written by `record-completion`), never from free-form path choices.
Files are only ever MOVED to the macOS Trash (os.rename; no copy, no rm, no globbing, symlinks never followed).
`preview()` is read-only. `execute()` re-runs every safety check and needs the exact phrase 'DELETE RUN <run-id>'.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, registry

RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}(-\d+)?$")
FINALS_DIR = "finals"                       # finals/<run-id>/<name>.mp4 + manifest.json (no earlier convention existed)
# Normal cleanup NEVER touches canonical sources, transcripts or analysis (in-run source/ and transcripts/ are not cleanable at all,
# candidates/ keeps its analysis text/json, project-level source_videos/ source_library/ transcripts/ clip_candidates/ are not in scope).
# Removing a canonical source needs the separate `PURGE SOURCE <source-id>` flow below.
ELIGIBLE_DIRS = ("candidates", "renders", "qa", "tmp", "cache", "scratch", "proxies", "frames")
ANALYSIS_SUFFIXES = (".json", ".md", ".txt", ".srt", ".csv")
PROTECTED_TOP = ("usage.json", "completion.json")
UP_OK = ("succeeded", "uploaded")
EXTERNAL_DIRS = ("rendered_clips",)           # drafts/aliases only
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class CleanupError(Exception):
    """A safety check failed; nothing was moved."""


def confirm_phrase(run_id: str) -> str:
    return f"DELETE RUN {run_id}"


# --- paths -------------------------------------------------------------------------
def run_dir_for(run_id: str, root: Path) -> Path:
    if not RUN_ID.fullmatch(run_id or ""):
        raise CleanupError(f"invalid run id {run_id!r} (expected YYYYMMDD-HHMMSS-<8 hex>)")
    d = Path(root) / "runs" / run_id
    if d.is_symlink() or not d.is_dir():
        raise CleanupError(f"run directory not found (or is a symlink): runs/{run_id}")
    return d


def rel_parts(rel: str) -> tuple[str, ...]:
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts or any(c in rel for c in "*?[]{}$~\0"):
        raise CleanupError(f"unsafe path in record: {rel!r}")
    return p.parts


def no_symlink_dirs(run_dir: Path, parts: tuple[str, ...]) -> Path:
    """Walk parent components with lstat; refuse if any parent directory is a symlink. Returns the (unresolved) target."""
    cur = run_dir
    for part in parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise CleanupError(f"path passes through a symlinked directory: {cur.relative_to(run_dir)}")
    return cur / parts[-1]


def is_protected(parts: tuple[str, ...]) -> bool:
    if len(parts) == 1 and parts[0] in PROTECTED_TOP:
        return True
    if parts[0] in ("source", "transcripts") or (parts[0] == "candidates" and parts[-1].lower().endswith(ANALYSIS_SUFFIXES)):
        return True
    if parts[0] == "upload-manifests" or parts == ("source", "originals.json") or parts[-1] == "manifest.json":
        return True
    return parts[0] == "qa" and parts[-1].endswith(".qa.json")


def build_eligible(run_dir: Path, final_rel: str) -> list[str]:
    """Explicit per-run list of cleanable files: everything in the eligible dirs except protected records and the final."""
    out = []
    for d in ELIGIBLE_DIRS:
        base = run_dir / d
        if base.is_symlink() or not base.is_dir():
            continue
        for dp, dns, fns in os.walk(base, followlinks=False):
            for n in fns + [x for x in dns if (Path(dp) / x).is_symlink()]:
                rel = (Path(dp) / n).relative_to(run_dir)
                if str(rel) != final_rel and not is_protected(rel.parts):
                    out.append(str(rel))
    return sorted(out)


# --- probing -----------------------------------------------------------------------
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def probe_final(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise CleanupError(f"final MP4 missing or not a regular file: {path}")
    if path.stat().st_size <= 0:
        raise CleanupError(f"final MP4 is empty: {path}")
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        raise CleanupError(f"final MP4 is not readable by ffprobe: {r.stderr.strip()[-200:]}")
    info = json.loads(r.stdout)
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    if not {"video", "audio"} <= kinds:
        raise CleanupError(f"final MP4 must contain video and audio streams, found {sorted(kinds)}")
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return {"size_bytes": path.stat().st_size, "duration_sec": round(float(info["format"]["duration"]), 3),
            "width": v["width"], "height": v["height"]}


# --- plan --------------------------------------------------------------------------
def load_record(run_dir: Path) -> dict:
    p = run_dir / "completion.json"
    if p.is_symlink() or not p.is_file():
        raise CleanupError("no completion.json for this run - run `record-completion` first (nothing to base cleanup on)")
    try:
        return json.loads(p.read_text())
    except ValueError as exc:
        raise CleanupError(f"completion.json is not valid JSON: {exc}") from exc


def make_plan(run_id: str, root: Path | None = None) -> dict:
    """All safety checks. Raises CleanupError on the first failure. Read-only."""
    root = Path(root) if root else config.project_root()
    run_dir = run_dir_for(run_id, root)
    rec = load_record(run_dir)
    if rec.get("run_id") != run_id:
        raise CleanupError("completion.json belongs to a different run id")
    notes = []
    cleaned_before = (rec.get("cleanup") or {}).get("result") == "completed"

    # final MP4: still in the run, or already moved to finals/<id>/
    final_rel = rec.get("final_artifact")
    in_run = final_path = None
    if final_rel:
        in_run = no_symlink_dirs(run_dir, rel_parts(final_rel))
    moved = rec.get("final_location")
    if moved:
        final_path = Path(root) / moved
    if in_run is not None and (in_run.exists() or in_run.is_symlink()):
        final_path, where = in_run, "run"
    elif final_path is not None:
        where = "finals"
    elif in_run is not None:
        final_path, where = in_run, "run"
    else:
        raise CleanupError("record names no final MP4")
    if final_path.suffix.lower() != ".mp4":
        raise CleanupError(f"final artifact is not an .mp4: {final_path.name}")
    info = probe_final(final_path)

    if rec.get("qa_result") != "passed":
        raise CleanupError(f"QA has not passed (recorded: {rec.get('qa_result')!r})")
    up = rec.get("higgsfield_upload") or {}
    asset = str(up.get("asset_id") or "").strip()
    ev = up.get("evidence") or {}
    if up.get("status") not in UP_OK or not asset:
        raise CleanupError(f"no confirmed Higgsfield upload (status={up.get('status')!r}, asset_id={up.get('asset_id')!r})")
    if up["status"] == "uploaded" and not (UUID.fullmatch(asset) and ev.get("media_confirm_status") == "uploaded" and ev.get("confirmed_at")):
        raise CleanupError("status 'uploaded' needs a valid Higgsfield asset id (UUID) and an upload record (evidence.media_confirm_status/confirmed_at)")
    if rec.get("registry_required"):
        reg = registry.load(root)
        mine = [c for c in reg["clips"] if c["run_id"] == run_id]
        if rec.get("registry_source_sha256") not in reg["sources"] or len(mine) < int(rec.get("registry_clip_count", 1)):
            raise CleanupError("used-clips registry does not yet hold this run's ranked moments; refusing to clean this run")
    digest = sha256(final_path)
    if rec.get("uploaded_sha256") and rec["uploaded_sha256"] != digest:
        raise CleanupError("final MP4 differs from the file that was uploaded to Higgsfield (checksum mismatch)")
    if not rec.get("uploaded_sha256"):
        notes.append("record has no uploaded_sha256; cannot prove the kept file is the exact uploaded file")
    notes.append("Higgsfield upload is taken from the local record; it is not re-verified against Higgsfield here")

    # targets
    final_resolved = final_path.resolve()
    targets, seen = [], set()
    for rel in rec.get("cleanup_eligible", []):
        parts = rel_parts(rel)
        if parts[0] not in ELIGIBLE_DIRS:
            raise CleanupError(f"target outside the cleanable directories: {rel}")
        if is_protected(parts) or rel in (rec.get("protected") or []):
            raise CleanupError(f"target is protected: {rel}")
        p = no_symlink_dirs(run_dir, parts)
        if rel in seen:
            raise CleanupError(f"duplicate target: {rel}")
        seen.add(rel)
        if not p.is_symlink() and p.exists() and (p.resolve() == final_resolved or os.path.samefile(p, final_path)):
            raise CleanupError(f"final MP4 is in the cleanup list: {rel}")
        if p.is_symlink():
            targets.append({"rel": rel, "path": p, "bytes": 0, "kind": "symlink (link only; target untouched)"})
        elif p.is_file():
            targets.append({"rel": rel, "path": p, "bytes": p.stat().st_size, "kind": "file"})
        elif p.exists():
            raise CleanupError(f"target is not a plain file (directories are never moved): {rel}")
        else:
            targets.append({"rel": rel, "path": p, "bytes": 0, "kind": "already gone"})
    keep_ext = set(rec.get("external_keep") or [])
    for rel in rec.get("external_cleanup", []):
        parts = rel_parts(rel)
        if len(parts) != 2 or parts[0] not in EXTERNAL_DIRS or parts[1].startswith("."):
            raise CleanupError(f"external target not allowed (must be a file directly inside {', '.join(EXTERNAL_DIRS)}): {rel}")
        d = Path(root) / parts[0]
        if d.is_symlink() or not d.is_dir():
            raise CleanupError(f"external directory missing or a symlink: {parts[0]}")
        if rel in keep_ext or rel in seen:
            raise CleanupError(f"external target is protected or duplicated: {rel}")
        seen.add(rel)
        p = d / parts[1]
        alias = rel == rec.get("final_alias")
        if p.is_symlink():
            targets.append({"rel": rel, "path": p, "bytes": 0, "kind": "symlink (link only; target untouched)", "ino": None})
        elif p.is_file():
            same = os.path.samefile(p, final_path)
            if same and not alias:
                raise CleanupError(f"final MP4 is in the cleanup list: {rel}")
            if alias and not (same and sha256(p) == digest):
                raise CleanupError(f"declared alias is not a hard link of the final MP4: {rel}")
            st = p.stat()
            targets.append({"rel": rel, "path": p, "bytes": st.st_size, "ino": (st.st_dev, st.st_ino),
                            "kind": "alias: hard link of the final (0 bytes reclaimed)" if alias else "file (outside run)"})
        elif p.exists():
            raise CleanupError(f"target is not a plain file: {rel}")
        else:
            targets.append({"rel": rel, "path": p, "bytes": 0, "kind": "already gone", "ino": None})
    if rec.get("metadata_updates"):
        raise CleanupError("source metadata changes are not part of normal cleanup (use the PURGE SOURCE flow)")
    present = [t for t in targets if t["kind"] != "already gone"]
    seen_ino = set()
    for t in present:                                   # a hard-linked inode only frees its space once
        key = t.get("ino") or (t["path"].lstat().st_dev, t["path"].lstat().st_ino)
        if key in seen_ino or "alias" in t["kind"]:
            t["bytes"] = 0
        seen_ino.add(key)
    dest_dir = Path(root) / FINALS_DIR / run_id
    retain = rec.get("retain_in_run") is True
    if retain and where != "run":
        raise CleanupError("record says the final is retained in the run, but it is not there")
    dest = final_path if retain else dest_dir / final_path.name
    move_needed = where == "run" and not retain
    if move_needed and (dest.exists() or dest.is_symlink()):
        if dest.is_symlink() or sha256(dest) != digest:
            raise CleanupError(f"destination already exists and differs: {dest.relative_to(root)} - stop and ask the user")
        notes.append(f"destination {dest.relative_to(root)} already exists and is byte-identical; it will be reused")
    return {"run_id": run_id, "root": Path(root), "run_dir": run_dir, "record": rec, "final": final_path, "final_where": where,
            "final_info": info, "final_sha256": digest, "dest": dest, "move_needed": move_needed, "targets": targets,
            "present": present, "bytes": sum(t["bytes"] for t in present), "notes": notes,
            "already_clean": cleaned_before and not present and not move_needed}


def preview(run_id: str, root: Path | None = None) -> str:
    p = make_plan(run_id, root)
    root = p["root"]
    i, up = p["final_info"], p["record"]["higgsfield_upload"]
    L = [f"CLEANUP PREVIEW (no changes made)  run: {p['run_id']}  ({p['run_dir'].relative_to(root)})",
         f"Final MP4 kept: {p['final'].relative_to(root)}  {i['size_bytes']:,} B  {i['duration_sec']}s  {i['width']}x{i['height']}",
         f"Final location after cleanup: {p['dest'].relative_to(root) if p['move_needed'] else p['final'].relative_to(root)}"
         f"  (+ manifest.json beside it)",
         f"Higgsfield upload: id {up['asset_id']}  status {up['status']}", f"QA: {p['record']['qa_result']}"]
    if p["already_clean"]:
        L.append("Nothing to do: this run was already cleaned.")
    L.append(f"Proposed for Trash ({len(p['present'])} items):")
    L += [f"  {t['bytes']:>14,} B  {t['rel']}  [{t['kind']}]" for t in p["present"]]
    gone = [t for t in p["targets"] if t["kind"] == "already gone"]
    if gone:
        L.append(f"Already gone (skipped): {len(gone)}")
    L.append(f"Total recoverable: {p['bytes']:,} B (moved to Trash, not deleted)")
    L.append("Protected, stays: final MP4, usage.json, completion.json, source/originals.json, upload-manifests/, qa/*.qa.json, "
             "project code, tests, config/, .claude/skills/, CLAUDE.md, fonts/templates/models, git, other runs, everything outside this run")
    if p["record"].get("protected"):
        L.append("Also protected by record: " + ", ".join(p["record"]["protected"]))
    L += [f"Note: {n}" for n in p["notes"]]
    L.append(f"To proceed, type exactly: {confirm_phrase(run_id)}")
    return "\n".join(L)


# --- execute -----------------------------------------------------------------------
def default_trash() -> Path:
    if sys.platform != "darwin":
        raise CleanupError("moving to Trash is only supported on macOS here; stopping (nothing deleted)")
    return Path.home() / ".Trash"


def unique_trash_name(trash: Path, name: str) -> Path:
    d = trash / name
    n = 1
    while d.exists() or d.is_symlink():
        d = trash / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return d


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def execute(run_id: str, confirmation: str, root: Path | None = None, trash_dir: Path | None = None) -> dict:
    if confirmation != confirm_phrase(run_id):
        raise CleanupError(f"confirmation must be exactly: {confirm_phrase(run_id)}")
    p = make_plan(run_id, root)
    root, run_dir, rec = p["root"], p["run_dir"], p["record"]
    if p["already_clean"]:
        return {"result": "already_clean", "moved": 0, "bytes": 0}
    trash = Path(trash_dir) if trash_dir else default_trash()
    if not trash.is_dir() or trash.is_symlink() or not os.access(trash, os.W_OK):
        raise CleanupError(f"Trash is unavailable ({trash}); stopping without changes")
    if p["present"] and os.stat(trash).st_dev != os.stat(run_dir).st_dev:
        raise CleanupError("Trash is on a different volume; a move would need a copy. Stopping without changes")

    final_dir = p["dest"].parent
    final_now = p["final"]
    if p["move_needed"]:                       # keep the final: hard-link into finals/ (fails if present), then drop the old name
        final_dir.mkdir(parents=True, exist_ok=True)
        if not p["dest"].exists():
            os.link(final_now, p["dest"])
        if sha256(p["dest"]) != p["final_sha256"]:
            raise CleanupError("final copy verification failed; original left in place")
        os.unlink(final_now)
        final_now = p["dest"]
    moved, bytes_moved, error = [], 0, None
    for t in p["present"]:
        try:
            dst = unique_trash_name(trash, f"{run_id}__{t['rel'].replace(os.sep, '__')}")
            os.rename(t["path"], dst)          # moves a symlink itself; never follows it
            moved.append({"former_path": str(t["path"]), "trash_name": dst.name, "bytes": t["bytes"]})
            bytes_moved += t["bytes"]
        except OSError as exc:
            error = f"{t['rel']}: {exc}"
            break
    for x in ELIGIBLE_DIRS:                    # remove only directories left empty (children first)
        d = run_dir / x
        if d.is_dir() and not d.is_symlink():
            for dp, dns, fns in os.walk(d, topdown=False, followlinks=False):
                if not Path(dp).is_symlink() and not os.listdir(dp):
                    os.rmdir(dp)
    info = probe_final(final_now)
    now_iso = datetime.now(timezone.utc).isoformat()
    manifest = {"run_id": run_id, "final_filename": final_now.name, "final_path": str(final_now.relative_to(root)),
                "size_bytes": info["size_bytes"], "duration_sec": info["duration_sec"],
                "resolution": f"{info['width']}x{info['height']}", "sha256": p["final_sha256"],
                "completed_at": rec.get("completed_at"),
                "higgsfield_asset_id": rec["higgsfield_upload"]["asset_id"], "higgsfield_upload_status": rec["higgsfield_upload"]["status"],
                "tiktok_transfer_status": rec.get("tiktok_transfer", "unknown"),
                "cleanup_timestamp": now_iso, "total_bytes_trashed": bytes_moved,
                "cleaned_paths": [m["former_path"] for m in moved], "trash_names": [m["trash_name"] for m in moved],
                "cleanup_result": "completed" if error is None else f"partial: {error}"}
    write_json_atomic(final_dir / "manifest.json", manifest)
    rec["final_location"] = manifest["final_path"]
    rec["cleanup"] = {"result": "completed" if error is None else "partial", "at": manifest["cleanup_timestamp"], "bytes": bytes_moved}
    write_json_atomic(run_dir / "completion.json", rec)
    if error:
        raise CleanupError(f"stopped part-way ({error}); {len(moved)} items were moved to Trash; see {final_dir.name}/manifest.json")
    return {"result": "completed", "moved": len(moved), "bytes": bytes_moved, "manifest": str(final_dir / "manifest.json")}


# --- completion record -------------------------------------------------------------
def record_completion(run_id: str, final_rel: str, qa_result: str, asset_id: str, upload_status: str, root: Path | None = None,
                      tiktok_transfer: str = "not_started", user_reviewed: bool = False, uploaded_sha256: str | None = None,
                      extra: dict | None = None) -> Path:
    """Write runs/<id>/completion.json: final artifact, QA, Higgsfield upload and the explicit eligible/protected path lists."""
    root = Path(root) if root else config.project_root()
    run_dir = run_dir_for(run_id, root)
    out = run_dir / "completion.json"
    if out.exists() and (load_record(run_dir).get("cleanup") or {}).get("result"):
        raise CleanupError("run already cleaned; completion.json is final")
    if qa_result not in ("passed", "manual_review", "failed"):
        raise CleanupError("qa_result must be passed, manual_review or failed")
    if qa_result == "passed":                   # automated verdict + the user's own review of manual items
        qa_dir = run_dir / "qa"
        reps = sorted(f for f in qa_dir.iterdir() if f.name.endswith(".qa.json")) if qa_dir.is_dir() else []
        if not reps or any(json.loads(r.read_text())["verdict"] == "fail" for r in reps):
            raise CleanupError("cannot record QA as passed: no QA report, or a report has failures")
        if not user_reviewed and any(json.loads(r.read_text())["verdict"] != "pass" for r in reps):
            raise CleanupError("QA has manual_review items; pass user_reviewed=True only after the user reviewed them")
    final = no_symlink_dirs(run_dir, rel_parts(final_rel))
    info = probe_final(final)
    rec = {"run_id": run_id, "completed_at": datetime.now(timezone.utc).isoformat(), "final_artifact": final_rel,
           "final_size_bytes": info["size_bytes"], "final_sha256": sha256(final), "qa_result": qa_result,
           "higgsfield_upload": {"status": upload_status, "asset_id": asset_id},
           "uploaded_sha256": uploaded_sha256, "tiktok_transfer": tiktok_transfer,
           "cleanup_eligible": build_eligible(run_dir, final_rel),
           "protected": [final_rel, "usage.json", "completion.json", "source/originals.json", "upload-manifests/", "qa/*.qa.json"]}
    allowed = {"retain_in_run", "external_cleanup", "external_keep", "final_alias", "registry_required",
               "registry_source_sha256", "registry_clip_count", "backfilled", "backfill_notes", "upload_evidence"}
    if set(extra or {}) - allowed:
        raise CleanupError(f"unknown record fields: {sorted(set(extra) - allowed)}")
    extra = dict(extra or {})
    rec["higgsfield_upload"]["evidence"] = extra.pop("upload_evidence", None)
    rec.update(extra)
    write_json_atomic(out, rec)
    return out


# ======================= canonical source purge (separate, explicitly confirmed) =======================
SOURCE_ID = re.compile(r"^[0-9a-f]{12}$")
LIBRARY = "source_library"


def purge_phrase(source_id: str) -> str:
    return f"PURGE SOURCE {source_id}"


def purge_plan(source_id: str, root: Path | None = None) -> dict:
    """Read-only. Only media files (master.mp4 and the legacy ingested copy) can be purged; transcripts and analysis stay."""
    root = Path(root) if root else config.project_root()
    if not SOURCE_ID.fullmatch(source_id or ""):
        raise CleanupError(f"invalid source id {source_id!r} (expected 12 hex characters)")
    lib = root / LIBRARY / source_id
    if lib.is_symlink() or not lib.is_dir() or (root / LIBRARY).is_symlink():
        raise CleanupError(f"no source library entry (or it is a symlink): {LIBRARY}/{source_id}")
    metap = lib / "source.json"
    if metap.is_symlink() or not metap.is_file():
        raise CleanupError("source.json missing - cannot purge without the source record")
    meta = json.loads(metap.read_text())
    if meta.get("status") == "active":
        raise CleanupError("source is 'active' and can never be purged; archive it first (set-source-status <id> archived)")
    reg = registry.load(root)
    if meta.get("sha256") not in reg["sources"]:
        raise CleanupError("source fingerprint is not in registry/used_clips.json; refusing to purge")
    targets = []
    master = lib / "master.mp4"
    legacy = root / "source_videos" / f"{source_id}.mp4"
    for p in (master, legacy):
        if p.is_symlink():
            raise CleanupError(f"refusing to purge a symlink: {p.relative_to(root)}")
        if p.is_file():
            targets.append({"path": p, "rel": str(p.relative_to(root)), "bytes": p.stat().st_size})
    if master.is_file() and not (meta.get("master") or {}).get("sha256"):
        raise CleanupError("master.mp4 has no recorded sha256 in source.json; its fingerprint must be preserved before purging")
    return {"source_id": source_id, "root": root, "lib": lib, "meta": meta, "targets": targets,
            "bytes": sum(t["bytes"] for t in targets), "clips": [c for c in reg["clips"] if c["source_sha256"] == meta["sha256"]]}


def purge_preview(source_id: str, root: Path | None = None) -> str:
    p = purge_plan(source_id, root)
    L = [f"PURGE PREVIEW (no changes made)  source: {source_id}  status: {p['meta'].get('status')}",
         "Only the source MEDIA is purgeable; transcripts, analysis, source.json, source fingerprints and the used-clips registry always stay."]
    L += [f"  {t['bytes']:>14,} B  {t['rel']}" for t in p["targets"]] or ["  (no source media present - nothing to purge)"]
    L += [f"Total recoverable: {p['bytes']:,} B (moved to Trash, not deleted)",
          f"Used-clip records kept: {len(p['clips'])} (these moments stay excluded from future videos)",
          f"After purge the source is marked local_media_cleaned and must be re-supplied before it can be used again.",
          f"To proceed, type exactly: {purge_phrase(source_id)}"]
    return "\n".join(L)


def purge_execute(source_id: str, confirmation: str, root: Path | None = None, trash_dir: Path | None = None) -> dict:
    if confirmation != purge_phrase(source_id):
        raise CleanupError(f"confirmation must be exactly: {purge_phrase(source_id)}")
    p = purge_plan(source_id, root)
    root = p["root"]
    if not p["targets"]:
        return {"result": "nothing_to_purge", "moved": 0, "bytes": 0}
    trash = Path(trash_dir) if trash_dir else default_trash()
    if not trash.is_dir() or trash.is_symlink() or not os.access(trash, os.W_OK):
        raise CleanupError(f"Trash is unavailable ({trash}); stopping without changes")
    if any(os.stat(trash).st_dev != os.stat(t["path"]).st_dev for t in p["targets"]):
        raise CleanupError("Trash is on a different volume; a move would need a copy. Stopping without changes")
    moved, error = [], None
    for t in p["targets"]:
        try:
            dst = unique_trash_name(trash, f"source-{source_id}__{t['rel'].replace(os.sep, '__')}")
            os.rename(t["path"], dst)
            moved.append({"former_path": t["rel"], "trash_name": dst.name, "bytes": t["bytes"]})
        except OSError as exc:
            error = f"{t['rel']}: {exc}"
            break
    now = datetime.now(timezone.utc).isoformat()
    meta = p["meta"]
    meta.update({"status": "purged" if error is None else meta.get("status"), "local_media_cleaned": True, "local_media_cleaned_at": now,
                 "purged_paths": (meta.get("purged_paths") or []) + [m["former_path"] for m in moved]})
    write_json_atomic(p["lib"] / "source.json", meta)
    registry.mark_source_cleaned(meta["sha256"], now, root)
    if error:
        raise CleanupError(f"stopped part-way ({error}); {len(moved)} items were moved to Trash")
    return {"result": "purged", "moved": len(moved), "bytes": sum(m["bytes"] for m in moved), "trash_names": [m["trash_name"] for m in moved]}
