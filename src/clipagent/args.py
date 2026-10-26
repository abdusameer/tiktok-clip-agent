"""Argument handling for /make-rankings <video> | --video V [--audio A] [--transcript T] | --url U --rights R | --source-id S."""
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import ingest


class ArgError(ValueError):
    pass


@dataclass
class MakeRankingsArgs:
    video: Path | None = None
    audio: Path | None = None
    transcript: Path | None = None
    url: str | None = None
    source_id: str | None = None
    rights: str | None = None


def parse(argv: str | list[str]) -> MakeRankingsArgs:
    toks = shlex.split(argv) if isinstance(argv, str) else list(argv)
    vals: dict[str, str] = {}
    positional: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("--video", "--audio", "--transcript", "--url", "--source-id", "--rights"):
            if i + 1 >= len(toks) or toks[i + 1].startswith("--"):
                raise ArgError(f"{t} needs a value")
            if t[2:] in vals:
                raise ArgError(f"{t} given twice")
            vals[t[2:]] = toks[i + 1]
            i += 2
        elif t.startswith("--"):
            raise ArgError(f"unknown option {t}")
        else:
            positional.append(t)
            i += 1
    if len(positional) > 1:
        raise ArgError(f"unexpected extra arguments: {positional[1:]}")
    if positional and "video" in vals:
        raise ArgError("give the video either positionally or with --video, not both")
    video = positional[0] if positional else vals.get("video")
    modes = [m for m in (video, vals.get("url"), vals.get("source-id")) if m]
    if not modes:
        raise ArgError("usage: /make-rankings <video-file> | --video V [--audio A] [--transcript T] | --url <youtube-url> --rights R | --source-id S")
    if len(modes) > 1:
        raise ArgError("give exactly one of: a video file, --url, --source-id")
    if vals.get("audio") and not video:
        raise ArgError("--audio is only used together with --video")
    return MakeRankingsArgs(Path(video) if video else None, Path(vals["audio"]) if "audio" in vals else None,
                            Path(vals["transcript"]) if "transcript" in vals else None,
                            vals.get("url"), vals.get("source-id"), vals.get("rights"))


def validate(a: MakeRankingsArgs) -> MakeRankingsArgs:
    """Check every supplied file exists and is readable; video must have video (and audio unless --audio is given)."""
    if a.url or a.source_id:                                  # nothing local to check; --url needs --rights before any download
        if a.url and not (a.rights or "").strip():
            raise ArgError("--url needs --rights: say how you are authorized to download and process this video")
        return a
    out = {}
    for name in ("video", "audio", "transcript"):
        p = getattr(a, name)
        if p is None:
            continue
        p = p.expanduser().resolve()
        if not p.is_file():
            raise ArgError(f"--{name} file not found: {p}")
        out[name] = p
    try:
        vinfo = ingest.probe(out["video"])
        ainfo = ingest.probe_audio(out["audio"]) if "audio" in out else None
    except ingest.IngestError as exc:
        raise ArgError(str(exc)) from exc
    if ainfo is not None and not ainfo:
        raise ArgError(f"--audio file has no audio stream: {out['audio'].name}")
    if "audio" not in out and not vinfo["has_audio"]:
        raise ArgError("video has no audio stream; supply --audio")
    return MakeRankingsArgs(out["video"], out.get("audio"), out.get("transcript"), None, None, a.rights)
