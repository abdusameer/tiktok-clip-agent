import json
import shutil
import subprocess

import pytest

from clipagent import ingest as ing

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@pytest.fixture
def video(tmp_path):
    out = tmp_path / "in" / "demo.mp4"
    out.parent.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=duration=1", "-shortest", str(out)],
        check=True,
    )
    return out


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPAGENT_ROOT", str(tmp_path / "proj"))


def test_ingest_records_metadata(video, tmp_path):
    rec = ing.ingest(video, "my own stream")
    assert rec["width"] == 320 and rec["height"] == 240 and rec["has_audio"]
    src = tmp_path / "proj" / "source_videos"
    assert (src / rec["stored_file"]).exists()
    assert json.loads((src / f"{rec['id']}.json").read_text())["rights_note"] == "my own stream"


def test_duplicate_rejected(video):
    ing.ingest(video, "x")
    with pytest.raises(ing.IngestError, match="already"):
        ing.ingest(video, "x")


def test_rights_required(video):
    with pytest.raises(ing.IngestError, match="rights"):
        ing.ingest(video, "  ")


def test_missing_and_non_video(tmp_path):
    with pytest.raises(ing.IngestError, match="not found"):
        ing.ingest(tmp_path / "nope.mp4", "x")
    bad = tmp_path / "bad.mp4"
    bad.write_text("not a video")
    with pytest.raises(ing.IngestError):
        ing.ingest(bad, "x")
