import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipagent import colors as C
from clipagent import production as PR
from clipagent import render as R
from clipagent import style
from test_production import RID, SID, good, make_mp4, master, meta, proj  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
APPROVED = {"#FFD84D", "#58D9FF", "#8BE36B", "#FF9F43", "#FF7EB6", "#C9A7FF", "#FFFFFF"}


def test_palette_and_rules_live_in_the_style_file():
    cfg = style.load()["label"]["title_accent"]
    assert {p["hex"] for p in cfg["palette"]} == APPROVED and cfg["fallback"] == "#FFFFFF" and cfg["no_consecutive_repeat"] is True
    assert not any(k in cfg for k in ("gradient", "rainbow", "flash", "animate"))
    lb = style.load()["label"]
    assert lb["font"] == "bold" and lb["size"] == 50 and lb["background"]["fill"] == [0, 0, 0, 140]      # approved look untouched
    assert lb["color"] == [255, 255, 255, 255]


def test_every_palette_color_is_readable_and_bad_colors_are_not():
    assert all(C.readable(c) for c in C.palette())
    assert not C.readable("#000080") and not C.readable("#333333") and not C.readable("#8B0000")
    with pytest.raises(C.ColorError):
        C.to_rgb("red")


def test_one_seeded_color_per_video_deterministic_and_varied_across_videos():
    ranks = ["5", "4", "3", "2", "1"]
    a = C.assign("run1|vid|Ranking", ranks)
    assert a == C.assign("run1|vid|Ranking", ranks)                              # re-render reproduces the color
    assert len(set(a.values())) == 1 and set(a.values()) <= APPROVED             # ONE consistent accent color inside a video
    assert C.video_color("run1|vid|Ranking") == a["5"]
    seen = {C.video_color(f"run{i}|vid|t") for i in range(300)}
    assert len(seen) >= 5 and seen <= APPROVED                                    # different videos get different colors
    assert C.validate(a, ranks) == []
    assert C.validate({**a, "3": "#58D9FF" if a["3"] != "#58D9FF" else "#FFD84D"}, ranks)    # mixed colors are a violation


def test_unreadable_or_off_palette_choices_are_replaced_and_white_is_the_last_resort(monkeypatch):
    ranks = ["4", "3", "2", "1"]
    assert C.resolve({r: "#000080" for r in ranks}, "seed", ranks) == C.assign("seed", ranks)           # unreadable -> seeded palette color
    assert C.resolve({r: "#123456" for r in ranks}, "seed", ranks) == C.assign("seed", ranks)           # off-palette
    assert C.resolve({"4": "#FFD84D", "3": "#58D9FF", "2": "#FFD84D", "1": "#FFD84D"}, "seed", ranks) == C.assign("seed", ranks)   # mixed
    assert C.resolve({r: "#FFD84D" for r in ranks}, "seed", ranks) == {r: "#FFD84D" for r in ranks}     # good explicit choice kept
    cfg = dict(style.load()["label"]["title_accent"], min_contrast=99.0)           # nothing passes -> fall back to white
    monkeypatch.setattr(C, "_cfg", lambda: cfg)
    assert set(C.assign("s", ranks).values()) == {"#FFFFFF"}


def test_only_the_title_fill_changes_in_the_label_graphics(tmp_path):
    spec = {"title": "T", "moments": {"1": {"start": 0, "end": 5, "words": [["a", 0.1, 0.5]], "lines": [{"text": "a", "first": 0, "last": 0}]}}}
    plain = R.png_spec(spec, ["1"], tmp_path, {"1": "Mic Falls Over"})["items"]
    tinted = R.png_spec(spec, ["1"], tmp_path, {"1": "Mic Falls Over"}, {"1": "#FF7EB6"})["items"]
    lp = next(i for i in plain if "label1" in i["out"])
    lt = next(i for i in tinted if "label1" in i["out"])
    assert lt["blocks"][0]["fill"] == [255, 126, 182, 255]
    for k in ("w", "h", "pill"):
        assert lp[k] == lt[k]
    for k in ("text", "font", "size", "cx", "y", "stroke"):
        assert lp["blocks"][0][k] == lt["blocks"][0][k]
    assert [i for i in plain if "label" not in i["out"]] == [i for i in tinted if "label" not in i["out"]]      # header/badge/captions identical


def _ranking_library(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    d = root / "source_library" / SID
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=30", "-f", "lavfi", "-i", "sine=d=30", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "master.mp4")], check=True)
    (d / "source.json").write_text(json.dumps({"id": SID, "status": "active", "sha256": "a" * 64, "master": {"width": 1280, "height": 720, "duration_sec": 30.0, "size_bytes": 1}}))
    monkeypatch.setenv("CLIPAGENT_ROOT", str(root))
    return root


def test_ranking_render_assigns_records_and_reproduces_colors(tmp_path, monkeypatch):
    root = _ranking_library(tmp_path, monkeypatch)
    run = root / "runs" / RID
    (run / "renders").mkdir(parents=True)
    moments = {str(r): {"label": f"Clip {n}", "start": 2.0 + 6 * i, "end": 6.0 + 6 * i, "words": [["hi", 2.2 + 6 * i, 2.8 + 6 * i]],
                        "lines": [{"text": "hi", "first": 0, "last": 0}]} for i, (r, n) in enumerate((("4", "Four"), ("3", "Three"), ("2", "Two"), ("1", "One")))}
    spec = {"video_id": SID, "title": "Ranking the tests", "wording_verified_by": "supplied_transcript", "moments": moments}
    sp = run / "spec.json"
    sp.write_text(json.dumps(spec))
    labels = run / "labels.json"
    labels.write_text(json.dumps([{"rank": 4, "title": "Clip Four"}, {"rank": 3, "title": "Clip Three"}, {"rank": 2, "title": "Clip Two"}, {"rank": 1, "title": "Clip One"}]))
    out1 = run / "renders" / "v1.mp4"
    R.render_ranking(SID, sp, out1, None, labels)
    rec = json.loads(sp.read_text())
    cols = rec["label_colors"]                                                    # recorded in the ranking specification
    assert set(cols) == {"4", "3", "2", "1"} and len(set(cols.values())) == 1 and set(cols.values()) <= APPROVED and rec["color_seed"].startswith(RID)
    side = json.loads(Path(str(out1) + ".render.json").read_text())
    assert side["label_colors"] == cols
    out2 = run / "renders" / "v2.mp4"
    (run / "spec2.json").write_text(json.dumps({k: v for k, v in rec.items() if k not in ("label_colors", "color_seed")}))      # same video, colors not pinned
    R.render_ranking(SID, run / "spec2.json", out2, None, labels)
    assert json.loads(Path(str(out2) + ".render.json").read_text())["label_colors"] == cols            # seeded by run + video -> identical


def test_finalize_records_colors_and_sends_unapproved_colors_to_needs_review(proj, tmp_path):          # noqa: F811
    good_cols = C.assign("s", ["4", "3", "2", "1"])
    shutil.copy(proj / "runs" / RID / "renders" / "a.mp4", proj / "runs" / RID / "renders" / "b.mp4")
    (proj / "runs" / RID / "renders" / "b.mp4.render.json").write_text(json.dumps({"label_colors": {**good_cols, "3": "#000080"}}))
    # the 16 s fixture is fine for QA; the off-palette color alone must stop auto-approval
    rec = PR.finalize_video(RID, "renders/b.mp4", "ranking", meta("ranking"), proj)
    assert rec["status"] == "needs_review" and any("title colors" in r for r in rec["needs_review_reasons"])


def test_report_lists_durations_categories_colors_and_cleanup(proj):                                     # noqa: F811
    from clipagent import registry
    (proj / "runs" / RID / "renders" / "a.mp4.render.json").write_text(json.dumps({"label_colors": C.assign("s", ["4", "3", "2", "1"])}))
    PR.finalize_video(RID, "renders/a.mp4", "ranking", {**meta("ranking"), "category": "Wild Reactions"}, proj)
    PR.auto_cleanup(RID, root=proj, trash_dir=proj / "trash")
    text = PR.format_report(PR.report(RID, proj))
    for needle in ("READY FOR DRAFT TRANSFER", "category: Wild Reactions", "duration:", "title colors: #4", "clips: abcdef123456:", "QA:", "Cleanup: completed"):
        assert needle in text, needle
    assert registry.load(proj)["clips"]


def test_fixable_qa_failures_get_automatic_corrections():
    rep = {"checks": [{"check": "duration_within_platform_limit", "status": "pass", "detail": "20.00s (limit 600s)"},
                      {"check": "no_frozen_frames", "status": "fail", "detail": "[(0.9, 1.0)]"}]}
    h = PR.fix_hints(rep)
    assert h["fixable"] and h["hints"][0]["action"] == "trim_start" and h["hints"][0]["seconds"] == 2.0 and h["max_retries"] == 2
    tail = {"checks": [rep["checks"][0], {"check": "no_black_frames", "status": "fail", "detail": "[(19.0, 1.0)]"}]}
    assert PR.fix_hints(tail)["hints"][0]["action"] == "trim_end"
    mid = {"checks": [rep["checks"][0], {"check": "no_frozen_frames", "status": "fail", "detail": "[(9.0, 2.0)]"}]}
    assert not PR.fix_hints(mid)["fixable"]
    other = {"checks": [rep["checks"][0], {"check": "no_clipped_audio", "status": "fail", "detail": "max_volume 0.0 dB"}]}
    assert not PR.fix_hints(other)["fixable"]
