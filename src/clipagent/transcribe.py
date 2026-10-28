"""Transcribe an ingested video to timestamped JSON, SRT and readable TXT."""
import json
import subprocess
from pathlib import Path
from typing import Callable

from . import config, models, source_library

DEFAULT_MODEL = "small.en"


class TranscribeError(Exception):
    pass


def srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def clock(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02}:{s % 3600 // 60:02}:{s % 60:02}"


def to_srt(segments: list[dict]) -> str:
    blocks = [
        f"{i}\n{srt_time(seg['start'])} --> {srt_time(seg['end'])}\n{seg['text']}\n"
        for i, seg in enumerate(segments, 1)
    ]
    return "\n".join(blocks)


def to_txt(segments: list[dict]) -> str:
    return "\n".join(f"[{clock(seg['start'])}] {seg['text']}" for seg in segments) + "\n"


def decode_audio(media: Path):
    """Decode to 16 kHz mono float32 with ffmpeg (faster-whisper's PyAV decoder is incompatible with av 19)."""
    import numpy as np

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise TranscribeError(f"ffmpeg could not decode audio: {proc.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _whisper_run(media: Path, model_name: str, progress: Callable[[float], None] | None) -> tuple[list[dict], dict]:
    from faster_whisper import WhisperModel

    model = models.whisper(model_name)
    gen, info = model.transcribe(decode_audio(media), vad_filter=True, word_timestamps=True)
    segments = []
    for seg in gen:
        segments.append({
            "id": len(segments),
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": [
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word, "prob": round(w.probability, 3)}
                for w in (seg.words or [])
            ],
        })
        if progress:
            progress(seg.end)
    return segments, {"language": info.language, "language_probability": round(info.language_probability, 3)}


def transcribe(video_id: str, model_name: str = DEFAULT_MODEL, progress=None, runner=_whisper_run) -> dict:
    """Transcribe an ingested video. Refuses to overwrite existing outputs."""
    try:
        ref = source_library.resolve(video_id)
    except FileNotFoundError:
        raise TranscribeError(f"unknown video id {video_id}; run `ingest` first") from None
    except source_library.SourceError as exc:
        raise TranscribeError(str(exc)) from exc
    meta, media = ref.meta, ref.media
    if not media.exists():
        raise TranscribeError(f"missing media file {media}")
    if not meta.get("has_audio"):
        raise TranscribeError("video has no audio stream")

    out_dir = ref.transcripts_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # The default model keeps the original <id>.<ext> names; other models get <id>.<model>.<ext>
    stem = video_id if model_name == DEFAULT_MODEL else f"{video_id}.{model_name}"
    paths = {ext: out_dir / f"{stem}.{ext}" for ext in ("json", "srt", "txt")}
    existing = [p.name for p in paths.values() if p.exists()]
    if existing:
        raise TranscribeError(f"refusing to overwrite existing output: {', '.join(existing)}")

    segments, info = runner(media, model_name, progress)
    record = {
        "video_id": video_id,
        "source_sha256": meta["sha256"],
        "model": model_name,
        "duration_sec": meta["duration_sec"],
        **info,
        "segments": segments,
    }
    # "x" mode: never clobber a file created while we were transcribing
    with paths["json"].open("x") as f:
        json.dump(record, f, indent=2)
    with paths["srt"].open("x") as f:
        f.write(to_srt(segments))
    with paths["txt"].open("x") as f:
        f.write(to_txt(segments))
    return {"record": record, "paths": paths}
