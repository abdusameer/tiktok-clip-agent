"""Register an authorized local video: copy it in, hash it, record metadata."""
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config


class IngestError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise IngestError("ffprobe not found; run `clipagent doctor`")
    if out.returncode != 0:
        raise IngestError(f"ffprobe could not read {path.name}: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise IngestError(f"{path.name} has no video stream")
    return {
        "duration_sec": float(data.get("format", {}).get("duration", 0) or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_audio": audio is not None,
    }


def probe_audio(path: Path) -> bool:
    """True if the file contains an audio stream (used for a separate --audio file)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise IngestError(f"ffprobe could not read {path.name}: {out.stderr.strip()}")
    return bool(out.stdout.strip())


def ingest(path: Path, rights: str) -> dict:
    """Copy `path` into source_videos/ and write a metadata record. Returns the record."""
    if not rights.strip():
        raise IngestError("a rights/authorization note is required")
    path = path.expanduser().resolve()
    if not path.is_file():
        raise IngestError(f"file not found: {path}")

    digest = sha256_file(path)
    video_id = digest[:12]
    dest_dir = config.source_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    meta_path = dest_dir / f"{video_id}.json"
    if meta_path.exists():
        raise IngestError(f"already ingested as {video_id}")

    info = probe(path)
    dest = dest_dir / f"{video_id}{path.suffix.lower()}"
    shutil.copy2(path, dest)
    record = {
        "id": video_id,
        "original_name": path.name,
        "stored_file": dest.name,
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "rights_note": rights.strip(),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        **info,
    }
    meta_path.write_text(json.dumps(record, indent=2))
    return record
