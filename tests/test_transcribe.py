import json

import pytest

from clipagent import transcribe as tr

SEGS = [
    {"id": 0, "start": 0.0, "end": 2.5, "text": "Hello there.", "words": []},
    {"id": 1, "start": 3661.234, "end": 3665.0, "text": "Later on.", "words": []},
]


def fake_runner(media, model, progress):
    return SEGS, {"language": "en", "language_probability": 0.99}


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_ROOT", str(tmp_path))
    src = tmp_path / "source_videos"
    src.mkdir()
    (src / "abc.mp4").write_bytes(b"x")
    (src / "abc.json").write_text(json.dumps(
        {"id": "abc", "stored_file": "abc.mp4", "sha256": "h", "duration_sec": 10, "has_audio": True}))
    return tmp_path


def test_srt_time():
    assert tr.srt_time(3661.234) == "01:01:01,234"
    assert tr.srt_time(0) == "00:00:00,000"


def test_formats():
    assert tr.to_srt(SEGS).startswith("1\n00:00:00,000 --> 00:00:02,500\nHello there.\n")
    assert "[01:01:01] Later on." in tr.to_txt(SEGS)


def test_transcribe_writes_outputs(proj):
    res = tr.transcribe("abc", runner=fake_runner)
    assert json.loads(res["paths"]["json"].read_text())["segments"][0]["text"] == "Hello there."
    assert res["paths"]["srt"].exists() and res["paths"]["txt"].exists()


def test_no_overwrite(proj):
    tr.transcribe("abc", runner=fake_runner)
    with pytest.raises(tr.TranscribeError, match="overwrite"):
        tr.transcribe("abc", runner=fake_runner)


def test_unknown_id(proj):
    with pytest.raises(tr.TranscribeError, match="unknown"):
        tr.transcribe("nope", runner=fake_runner)


def test_decode_audio_16k_mono(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    wav = tmp_path / "t.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(wav)], check=True)
    audio = tr.decode_audio(wav)
    assert audio.dtype.name == "float32" and abs(len(audio) - 32000) < 400


def test_other_model_gets_separate_files(proj):
    a = tr.transcribe("abc", runner=fake_runner)
    b = tr.transcribe("abc", model_name="medium.en", runner=fake_runner)
    assert a["paths"]["json"].name == "abc.json"
    assert b["paths"]["json"].name == "abc.medium.en.json"
    assert a["paths"]["json"].exists()
