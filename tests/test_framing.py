import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import framing as F
from clipagent import production as PR
from clipagent import render as R
from test_production import RID, SID, good, make_mp4, master, meta, proj  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
SPEC = {"title": "t", "wording_verified_by": "supplied_transcript", "moments": {"1": {"start": 0.0, "end": 2.0, "words": [["hi", 0.2, 0.8]], "lines": [{"text": "hi", "first": 0, "last": 0}]}}}


@pytest.mark.parametrize("w,h", [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160), (1366, 768)])
def test_any_16x9_source_maps_by_ratio(w, h):
    fr = F.analyze(w, h)
    assert fr["ok"] and fr["crop_ratio"] == 0.875
    assert abs(fr["scale"] - w / 1280) < 1e-3
    kept = 2 * int(h * F.crop_ratio() / 2)                       # what the ffmpeg crop expression evaluates to
    assert abs(kept / h - 0.875) < 0.003                          # identical framing at every resolution


@pytest.mark.parametrize("w,h", [(640, 480), (1080, 1920), (2560, 1080), (1000, 1000), (100, 56)])
def test_unusual_aspect_or_size_is_not_cropped_blindly(w, h):
    fr = F.analyze(w, h)
    assert not fr["ok"] and fr["reason"]
    assert F.crop_filter(fr) == "null"


def test_commands_use_ratios_not_720p_pixels_and_share_geometry():
    cmds = {d: " ".join(R.standalone_clip_command(SPEC, Path("/w"), Path("/m.mp4"), Path("/o.mp4"), d)) for d in ((1280, 720), (1920, 1080), (2560, 1440))}
    for c in cmds.values():
        assert "crop=iw:2*trunc(ih*0.875" in c and "1280:630" not in c
        assert f"scale={R.W}:{R.BAND_H}" in c and f"overlay=0:{R.BAND_Y}" in c
    assert len({c.split("-filter_complex")[1].split("-map")[0] for c in cmds.values()}) == 1      # same filter graph at every size
    odd = " ".join(R.standalone_clip_command(SPEC, Path("/w"), Path("/m.mp4"), Path("/o.mp4"), (640, 480)))
    assert "[0:v]null,split" in odd and f"scale={R.W}:-2" in odd
    rk = " ".join(R.clip_command({"moments": {"1": SPEC["moments"]["1"]}}, "1", Path("/w"), Path("/m.mp4"), Path("/o.mp4"), True, src_dims=(1920, 1080)))
    assert "crop=iw:2*trunc(ih*0.875" in rk


def _library(tmp_path, monkeypatch, w, h):
    root = tmp_path / f"p{w}"
    d = root / "source_library" / SID
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"smptebars=s={w}x{h}:r=30:d=3", "-f", "lavfi", "-i", "sine=d=3", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "master.mp4")], check=True)
    (d / "source.json").write_text(json.dumps({"id": SID, "status": "active", "sha256": "a" * 64, "master": {"width": w, "height": h, "duration_sec": 3.0, "size_bytes": 1}}))
    monkeypatch.setenv("CLIPAGENT_ROOT", str(root))
    return root


def test_rendered_1080p_matches_720p_directly(tmp_path, monkeypatch):
    frames = {}
    for w, h in ((1280, 720), (1920, 1080), (2560, 1440)):
        _library(tmp_path, monkeypatch, w, h)
        spec = tmp_path / f"s{w}.json"
        spec.write_text(json.dumps(SPEC))
        out = tmp_path / f"o{w}.mp4"
        R.render_standalone(SID, spec, out)
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-vf", "scale=108:192,format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, check=True)
        frames[w] = r.stdout
        assert subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                              capture_output=True, text=True).stdout.split()[0] == "1080,1920"
    for w in (1920, 2560):
        diff = sum(abs(x - y) for x, y in zip(frames[w], frames[1280])) / len(frames[1280])
        assert diff < 4.0, (w, diff)


def test_unmappable_source_goes_to_needs_review_not_approved(proj, tmp_path):            # noqa: F811
    info = json.loads((proj / "source_library" / SID / "source.json").read_text())
    info["master"].update(width=640, height=480)                                         # 4:3
    (proj / "source_library" / SID / "source.json").write_text(json.dumps(info))
    rec = PR.finalize_video(RID, "renders/a.mp4", "standalone", meta(), proj)
    assert rec["status"] == "needs_review" and any("source framing" in r and "unusual aspect" in r for r in rec["needs_review_reasons"])
    assert (proj / "needs-review" / RID / "a.mp4").is_file()
