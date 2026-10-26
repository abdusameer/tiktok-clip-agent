"""Production runs: runs/<run-id>/{source,transcripts,candidates,renders,qa,upload-manifests}. Never overwrites."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config, ingest

SUBDIRS = ("source", "transcripts", "candidates", "renders", "qa", "upload-manifests")


class RunError(Exception):
    pass


def runs_root() -> Path:
    return config.project_root() / "runs"


def create_run(video: Path, audio: Path | None = None, transcript: Path | None = None) -> Path:
    """Create a unique run directory and a source manifest recording the originals' hashes. Originals are not touched."""
    sha = ingest.sha256_file(video)[:8]
    base = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{sha}"
    root = runs_root()
    root.mkdir(exist_ok=True)
    for n in range(100):
        run_id = base if n == 0 else f"{base}-{n}"
        run_dir = root / run_id
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise RunError("could not allocate a unique run id")
    for s in SUBDIRS:
        (run_dir / s).mkdir()
    rec = {"run_id": run_id, "created": datetime.now(timezone.utc).isoformat(),
           "originals": {k: {"path": str(p), "sha256": ingest.sha256_file(p), "size_bytes": p.stat().st_size}
                         for k, p in (("video", video), ("audio", audio), ("transcript", transcript)) if p}}
    write_new(run_dir / "source" / "originals.json", json.dumps(rec, indent=2))
    return run_dir


def write_new(path: Path, text: str) -> Path:
    """Write a file, refusing to overwrite."""
    try:
        with Path(path).open("x") as f:
            f.write(text)
    except FileExistsError:
        raise RunError(f"refusing to overwrite {Path(path).name}") from None
    return Path(path)


def mux_command(video: Path, audio: Path, out: Path) -> list[str]:
    """Combine separate video+audio. Video is stream-copied (never re-encoded); AAC audio is copied as well."""
    return ["ffmpeg", "-v", "error", "-n", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy", "-movflags", "+faststart", str(out)]


def combine(video: Path, audio: Path, out: Path, usage=None) -> Path:
    if out.exists():
        raise RunError(f"refusing to overwrite {out.name}")
    if usage:
        usage.ffmpeg()
    r = subprocess.run(mux_command(video, audio, out), capture_output=True, text=True)
    if r.returncode:
        raise RunError(f"mux failed: {r.stderr.strip()[-400:]}")
    return out


CONTENT_SUBDIRS = ("ingest", "segments/shortlisted-previews", "plan", "working/rankings", "working/funny-moments", "working/continuous-moments",
                   "qa/reports", "qa/frames", "logs")


def create_content_run(source_id: str, source_sha256: str, root: Path | None = None) -> Path:
    """runs/<run-id>/ in the /make-content layout (STATUS.md/status.json are written by status.RunStatus)."""
    root = (Path(root) if root else config.project_root()) / "runs"
    root.mkdir(exist_ok=True)
    base = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{source_sha256[:8]}"
    for n in range(100):
        run_id = base if n == 0 else f"{base}-{n}"
        d = root / run_id
        try:
            d.mkdir()
            break
        except FileExistsError:
            continue
    else:
        raise RunError("could not allocate a unique run id")
    for sub in CONTENT_SUBDIRS:
        (d / sub).mkdir(parents=True)
    (d / "usage.json").write_text(json.dumps({"run_id": d.name, "source_id": source_id, "credits_spent": 0, "uploads": 0}, indent=2))
    return d
