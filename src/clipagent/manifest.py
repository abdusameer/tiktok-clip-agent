"""Upload manifests and the approval gate. Nothing here talks to TikTok; it only prepares and guards."""
import json
import re
from pathlib import Path

from . import runs, titles

SAFE_MODES = ("inbox", "draft")


class ApprovalError(Exception):
    pass


def build(video_file: str, entries: list[dict], category: str, caption: str, cover_title: str, hashtags: list[str],
          qa_verdict: str, duration_sec: float | None = None) -> dict:
    tags = [t if t.startswith("#") else f"#{t}" for t in hashtags]
    if not 4 <= len(tags) <= 5 or any(not re.fullmatch(r"#\w+", t) for t in tags):
        raise ValueError("need 4-5 hashtags")
    ordered = titles.validate_entries(entries)
    return {"video_file": video_file, "category": category, "cover_title": cover_title, "caption": caption, "hashtags": tags,
            "ranks": [{"rank": e["rank"], "title": e["title"], "label": titles.label_text(e)} for e in ordered],
            "qa_verdict": qa_verdict, "duration_sec": duration_sec,
            "approved_by_user": False, "transfer": {"status": "not_started", "mode": None},
            "public_publish": False}


def write(manifest_dir: Path, manifest: dict) -> Path:
    return runs.write_new(Path(manifest_dir) / f"{Path(manifest['video_file']).stem}.upload.json", json.dumps(manifest, indent=2))


def approve(manifest: dict) -> dict:
    """Record the user's approval of the finished video. Only call after the user has said so."""
    return {**manifest, "approved_by_user": True}


def authorize_transfer(manifest: dict, mode: str, user_authorized: bool) -> dict:
    """Gate for any TikTok transfer. Returns the manifest marked ready; raises unless every condition holds."""
    if manifest.get("public_publish"):
        raise ApprovalError("public publishing is never automatic")
    if not manifest.get("approved_by_user"):
        raise ApprovalError("video has not been approved by the user")
    if not user_authorized:
        raise ApprovalError("TikTok transfer needs explicit user authorization")
    if mode not in SAFE_MODES:
        raise ApprovalError(f"mode {mode!r} is not inbox/draft; stop and ask the user before direct posting")
    return {**manifest, "transfer": {"status": "ready", "mode": mode}}
