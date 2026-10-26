import os
from pathlib import Path


def project_root() -> Path:
    """Project root; override with CLIPAGENT_ROOT (used by tests)."""
    env = os.environ.get("CLIPAGENT_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def source_dir() -> Path:
    return project_root() / "source_videos"


def transcripts_dir() -> Path:
    return project_root() / "transcripts"
