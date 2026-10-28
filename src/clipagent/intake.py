"""Find source media the user dropped into sourcevideo/ (or loose in source_library/) and pair each video with its audio file, if any."""
from pathlib import Path

from . import config, source_library

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus"}
INBOX = "sourcevideo"


def _files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".") and not p.is_symlink())


def scan(root: Path | None = None) -> list[dict]:
    """[{'video': Path, 'audio': Path | None, 'where': 'sourcevideo'|'source_library'}]. Master files inside source_library/<id>/ are never returned."""
    root = Path(root) if root else config.project_root()
    out = []
    for where, folder in ((INBOX, root / INBOX), (source_library.LIB, root / source_library.LIB)):
        files = _files(folder)
        videos = [p for p in files if p.suffix.lower() in VIDEO_EXT]
        audios = [p for p in files if p.suffix.lower() in AUDIO_EXT]
        for v in videos:
            mate = next((a for a in audios if a.stem == v.stem or a.stem.startswith(v.stem) or v.stem.startswith(a.stem)), None)
            if mate is None and len(videos) == 1 and len(audios) == 1:
                mate = audios[0]
            out.append({"video": v, "audio": mate, "where": where})
    return out
