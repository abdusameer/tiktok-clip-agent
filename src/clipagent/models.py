"""Whisper model loading: prefer the local cache (no network), fall back to a download only when the model is not cached yet."""
_CACHE: dict = {}


def whisper(name: str = "small.en"):
    """Cached faster-whisper model. `local_files_only` first so a dropped connection never breaks analysis/QA of a cached model."""
    if name not in _CACHE:
        from faster_whisper import WhisperModel
        try:
            _CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8", local_files_only=True)
        except Exception:
            _CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _CACHE[name]
