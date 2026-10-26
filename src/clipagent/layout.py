"""Clean on-disk layout of a source library entry. Legacy (root-level) files are MOVED (never copied or deleted) into it on first touch.

source_library/<source-id>/
  master.mp4  source.json  fingerprint/
  transcripts/            (raw transcribe output, kept)
  transcript/             transcript.json  transcript.srt
  analysis/               signals.json  timeline.json  segments.csv  candidates.json  rejected.json
                          contact-sheets/  thumbnails/ (segment strips; _sampled/ = sampled evidence frames)
"""
import shutil
from pathlib import Path

MOVES = [("transcript.json", "transcript/transcript.json"), ("transcript.srt", "transcript/transcript.srt"),
         ("analysis.json", "analysis/signals.json"), ("candidates.json", "analysis/legacy-candidates.json"),
         ("contact-sheets", "analysis/contact-sheets"), ("frames", "analysis/thumbnails/_sampled")]


def migrate(lib: Path) -> list[str]:
    """Move legacy root-level analysis files into the clean layout. Never overwrites; returns what moved."""
    done = []
    for old, new in MOVES:
        src, dst = Path(lib) / old, Path(lib) / new
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done.append(f"{old} -> {new}")
    return done


def ensure(lib: Path) -> None:
    for d in ("transcript", "analysis", "analysis/contact-sheets", "analysis/thumbnails"):
        (Path(lib) / d).mkdir(parents=True, exist_ok=True)
