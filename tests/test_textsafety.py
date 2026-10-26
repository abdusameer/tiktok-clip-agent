import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import fullanalysis as FA
from clipagent import production as PR
from clipagent import render as R
from clipagent import textsafety as T
from test_production import RID, SID, good, make_mp4, master, meta, proj  # noqa: F401  (fixtures)

FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
needs_ocr = pytest.mark.skipif(not (shutil.which("swiftc") or Path("/usr/bin/swiftc").exists()), reason="swiftc required for the OCR helper")


@pytest.mark.parametrize("text", ["n!gga", "n.i.g.g.a", "N 1 G G 3 R", "ni99er", "NIGGGGA", "n​igga", "ｎｉｇｇａ", "nіgga", "n i g g a s",
                                   "f4gg0t", "F.A.G", "k1ke", "ch1nk", "sp1c", "r3tard", "retarded", "tr@nny", "w3tback", "g00k"])
def test_obfuscated_slurs_are_caught(text):
    assert T.scan_text(text), text


@pytest.mark.parametrize("text", ["kill all the jews", "gas the muslims", "go back to Africa", "Women are animals", "white power", "Heil Hitler", "gays are vermin"])
def test_hateful_phrases_are_caught(text):
    assert T.scan_text(text), text


@pytest.mark.parametrize("text", ["fuck", "what the fuck", "holy shit", "bitch", "ass", "dumbass", "damn it", "that is so gay lol", "dick", "pussy cat",
                                   "Scunthorpe", "snigger", "raccoon", "spicy", "classic", "assassin", "analysis", "chinking", "peacock", "a fagot of sticks",
                                   "I killed it", "kill the lights", "women are great", "the jews and the christians shared a meal"])
def test_ordinary_profanity_and_innocent_words_are_not_blocked(text):
    assert T.scan_text(text) == [], text


def test_every_generated_field_is_scanned():
    m = meta()
    assert T.scan_fields(T.generated_text(m)) == []
    for key, bad in (("video_title", "N1gga Moments"), ("cover_title", "k.i.k.e"), ("caption", "go back to africa lol")):
        assert any(f["field"] == key for f in T.scan_fields(T.generated_text({**m, key: bad}))), key
    assert any(f["field"].startswith("hashtag") for f in T.scan_fields(T.generated_text({**m, "hashtags": ["#a", "#b", "#c", "#n1gga"]})))
    assert any(f["field"].startswith("moment_title") for f in T.scan_fields(T.generated_text({**m, "moments": [{**m["moments"][0], "title": "Fa66ot Moment"}]})))
    spec = {"title": "ok", "moments": {"1": {"label": "ok", "lines": [{"text": "he said n i g g a"}]}}}
    assert any(f["field"].startswith("caption") for f in T.scan_fields(T.generated_text(None, spec)))
    assert any(f["field"].startswith("label") for f in T.scan_fields(T.generated_text(None, None, [{"rank": 1, "title": "Retarded Plan"}])))


def test_renders_refuse_severe_captions_but_not_profanity(tmp_path, monkeypatch):
    spec = {"title": "t", "moments": {"1": {"start": 0.0, "end": 2.0, "words": [["x", 0.1, 0.5]], "lines": [{"text": "what the fuck", "first": 0, "last": 0}]}}}
    T.assert_clean(T.generated_text(None, spec))                                      # profanity passes
    bad = json.loads(json.dumps(spec))
    bad["moments"]["1"]["lines"][0]["text"] = "you n!gga"
    p = tmp_path / "s.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(R.RenderError, match="severe language"):
        R.render_standalone(SID, p, tmp_path / "o.mp4")


def test_finalize_sends_severe_generated_text_to_needs_review_and_does_not_register(proj):    # noqa: F811
    rec = PR.finalize_video(RID, "renders/a.mp4", "standalone", {**meta(), "cover_title": "N1gga Cover"}, proj)
    assert rec["status"] == "needs_review" and any("text safety" in r for r in rec["needs_review_reasons"])
    assert (proj / "needs-review" / RID / "a.mp4").is_file()
    m = json.loads((proj / "needs-review" / RID / "a.manifest.json").read_text())
    assert m["text_safety"]["generated_text"] != "clean" and m["approval"] == "needs_review"


def _chat_video(path, text, size, x=30, y=420, dur=6):
    """A 1280x720 clip with a burned-in 'chat' line at (x, y); font size controls how readable it is in the final output."""
    img = path.with_suffix(".png")
    subprocess.run(["/usr/bin/python3", "-c", (
        "from PIL import Image, ImageDraw, ImageFont\n"
        f"im = Image.new('RGBA', (1280, 720), (0, 0, 0, 0)); d = ImageDraw.Draw(im); f = ImageFont.truetype('{FONT}', {size})\n"
        f"d.text(({x}, {y}), {text!r}, font=f, fill=(255, 255, 255, 255))\n"
        f"im.save('{img}')")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=0x303a52:s=1280x720:r=30:d={dur}", "-i", str(img), "-f", "lavfi", "-i", f"sine=d={dur}",
                    "-filter_complex", "[0:v][1:v]overlay=0:0[v]", "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(path)], check=True)
    return path


@needs_ocr
def test_whole_frame_ocr_finds_severe_chat_anywhere_and_suggests_a_trim(tmp_path):
    clip = _chat_video(tmp_path / "bl.mp4", "viewer42: you are a n1gga lol", 34, x=30, y=600)      # BOTTOM-left, not the top half
    sc = T.scan_moment(clip, 0.0, 6.0, root=tmp_path, src_dims=(1280, 720))
    assert sc["ocr_available"] and sc["findings"] and sc["findings"][0]["category"] == "racial_slur"
    assert not sc["verified_clean"] and sc["blocked_spans"] and "severe" in sc["note"]
    assert T.trim_suggestion(sc) is None                                                         # present for the whole moment -> reject, nothing to trim to


@needs_ocr
def test_chat_that_appears_late_can_be_trimmed_around(tmp_path):
    img = tmp_path / "late.png"
    subprocess.run(["/usr/bin/python3", "-c", (
        "from PIL import Image, ImageDraw, ImageFont\n"
        f"im = Image.new('RGBA', (1280, 720), (0, 0, 0, 0)); ImageDraw.Draw(im).text((900, 80), 'someone: n i g g a', font=ImageFont.truetype('{FONT}', 36), fill=(255,255,255,255))\n"
        f"im.save('{img}')")], check=True)
    clip = tmp_path / "late.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x303a52:s=1280x720:r=30:d=20", "-i", str(img), "-f", "lavfi", "-i", "sine=d=20",
                    "-filter_complex", "[0:v][1:v]overlay=0:0:enable='gte(t,13)'[v]", "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)
    sc = T.scan_moment(clip, 0.0, 20.0, root=tmp_path, src_dims=(1280, 720))
    assert sc["findings"] and min(f["t"] for f in sc["findings"]) >= 13
    span = T.trim_suggestion(sc)
    assert span and span[0] == 0.0 and 8 <= span[1] <= 12.5                                      # trimmed to end before the chat line appears


@needs_ocr
def test_tiny_chat_is_recorded_as_unverified_not_clean(tmp_path):
    clip = _chat_video(tmp_path / "tiny.mp4", "user: hello everyone how are you doing today", 11, x=30, y=300)     # ~11 px at 720p -> ~9 px in the output
    sc = T.scan_moment(clip, 0.0, 6.0, root=tmp_path, src_dims=(1280, 720))
    assert sc["ocr_available"] and not sc["findings"]
    if sc["text_boxes"]:
        assert sc["too_small_in_output"] > 0 and sc["chat_unverified"] and not sc["verified_clean"] and "UNVERIFIED" in sc["note"]


@needs_ocr
def test_readable_clean_text_is_verified_clean(tmp_path):
    clip = _chat_video(tmp_path / "ok.mp4", "WELCOME TO THE STREAM", 60, x=60, y=300)
    sc = T.scan_moment(clip, 0.0, 6.0, root=tmp_path, src_dims=(1280, 720))
    assert sc["ocr_available"] and sc["text_boxes"] >= 1 and sc["verified_clean"] and not sc["findings"]


def test_missing_ocr_is_reported_not_treated_as_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "ocr_tool", lambda root=None: None)
    sc = T.scan_moment(tmp_path / "x.mp4", 0, 5, root=tmp_path, src_dims=(1280, 720))
    assert sc["ocr_available"] is False and sc["verified_clean"] is False
