"""Cut-boundary, audio-sync and edit-point QA for finished content videos. Sloppy cuts must fail.

* cut_start/cut_end: no word is being spoken across a cut point (checked against word timestamps decoded around the edge).
* edit_point_frames: the frames right after each edit are not black.
* audio_source_sync: the rendered audio lines up with the source audio of the same moment (envelope cross-correlation).
"""
import subprocess
from pathlib import Path

import numpy as np

from . import qa

MARGIN = 0.05            # a cut within this distance of a word edge is clean
SYNC_TOL = 0.12          # seconds
_MODEL = {}


def _words_near(media: Path, t: float, half: float = 1.3) -> list[tuple[float, float, str]]:
    from faster_whisper import WhisperModel
    a = max(t - half, 0.0)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a}", "-to", f"{t + half}", "-i", str(media), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    if len(audio) < 8000 or float(np.abs(audio).max()) < 0.003:        # silence: nothing can be cut in half
        return []
    model = _MODEL.setdefault("m", WhisperModel("small.en", device="cpu", compute_type="int8"))
    segs, _ = model.transcribe(audio, beam_size=1, word_timestamps=True, language="en", condition_on_previous_text=False)
    return [(round(w.start + a, 2), round(w.end + a, 2), w.word.strip()) for s in segs for w in s.words]


def inside_word(t: float, words: list[tuple[float, float, str]], margin: float = MARGIN) -> tuple[float, float, str] | None:
    for a, b, w in words:
        if a + margin < t < b - margin:
            return (a, b, w)
    return None


def boundary_checks(media: Path, moments: list[dict], words_fn=None) -> list[dict]:
    """One check per cut point of every moment (start and end). `words_fn(media, t)` is injectable for tests."""
    words_fn = words_fn or _words_near
    out = []
    for i, m in enumerate(moments, 1):
        for kind, t in (("start", m["start"]), ("end", m["end"])):
            hit = inside_word(t, words_fn(media, t))
            out.append(qa.check(f"cut_{kind}_not_mid_word[{i}]", qa.FAIL if hit else qa.PASS,
                                f"{kind} {t:.2f}s falls inside the word {hit[2]!r} ({hit[0]:.2f}-{hit[1]:.2f}s)" if hit else f"{kind} {t:.2f}s is between words"))
    return out


def _env(x: np.ndarray, sr: int, hz: int = 100) -> np.ndarray:
    n = sr // hz
    x = np.abs(x[: len(x) // n * n]).reshape(-1, n).mean(axis=1)
    return (x - x.mean()) / (x.std() + 1e-9)


def _pcm(path: Path, ss: float, t: float, sr: int = 16000) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{ss}", "-t", f"{t}", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def sync_lag(out_path: Path, out_ss: float, src_path: Path, src_ss: float, dur: float = 3.0) -> float | None:
    """Lag (s) between the rendered audio and the source audio for the same moment; None when there is nothing to correlate (silence)."""
    a, b = _pcm(out_path, out_ss, dur), _pcm(src_path, src_ss, dur)
    n = min(len(a), len(b))
    if n < 16000 or float(np.abs(a[:n]).max()) < 0.003:
        return None
    if float(np.abs(a[:n]).reshape(-1, 160).mean(axis=1).std()) < 1e-4 or float(np.abs(b[:n]).reshape(-1, 160).mean(axis=1).std()) < 1e-4:
        return None                                    # a flat tone/silence has no envelope to align
    ea, eb = _env(a[:n], 16000), _env(b[:n], 16000)
    corr = np.correlate(ea, eb, mode="full")
    lag = (int(np.argmax(corr)) - (len(eb) - 1)) / 100.0
    return lag


def sync_checks(out_path: Path, src_path: Path, moments: list[dict], offsets: list[float]) -> list[dict]:
    """offsets[i] = where moment i starts inside the rendered video."""
    res = []
    for i, (m, off) in enumerate(zip(moments, offsets), 1):
        d = min(3.0, m["end"] - m["start"] - 0.2)
        lag = sync_lag(out_path, off + 0.1, src_path, m["start"] + 0.1, d) if d > 1.0 else None
        ok = lag is None or abs(lag) <= SYNC_TOL
        res.append(qa.check(f"audio_source_sync[{i}]", qa.PASS if ok else qa.FAIL,
                            "silent moment (nothing to correlate)" if lag is None else f"rendered audio is {lag * 1000:+.0f} ms from the source"))
    return res


def edit_point_checks(out_path: Path, offsets: list[float]) -> list[dict]:
    """The frame just after every edit must not be black."""
    res = []
    for i, off in enumerate(offsets, 1):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{off + 0.15:.2f}", "-i", str(out_path), "-frames:v", "1", "-vf", "scale=32:56,format=gray",
                              "-f", "rawvideo", "-"], capture_output=True).stdout
        mean = float(np.frombuffer(raw, dtype=np.uint8).mean()) if raw else 0.0
        res.append(qa.check(f"edit_point_frames[{i}]", qa.PASS if mean > 12 else qa.FAIL, f"mean luminance {mean:.0f} right after edit {i}"))
    return res
