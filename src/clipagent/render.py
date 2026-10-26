"""Render a ranking TikTok draft: 1080x1920, blurred backdrop, cropped video band, new captions.

The original audio is untouched (only 30 ms edge fades at each cut to avoid pops). Profanity is
captioned exactly as spoken. Nothing is uploaded or published.
"""
import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import captions as captions_mod
from . import colors as colors_mod
from . import config, framing, source_library, style, textsafety, titles

_S = style.load()
W, H, FPS = _S["canvas"]["width"], _S["canvas"]["height"], _S["canvas"]["fps"]
SRC_W, SRC_H = _S["source"]["width"], _S["source"]["height"]
KEEP_H = _S["framing"]["keep_height"]          # drops the burned-in caption strip (bottom ~12.5%)
BAND_Y = _S["framing"]["band_y"]
BAND_H = round(KEEP_H * W / SRC_W / 2) * 2
CAP_Y, CAP_H = _S["captions"]["y"], _S["captions"]["height"]   # below the band, above TikTok's bottom UI zone
HDR_DY = _S["header"]["dy"]                    # header pushed down so the title clears TikTok's top 200 px
CAP_MAX_W = _S["captions"]["max_width"]
BADGE_Y, BADGE_H = _S["badge"]["y"] + HDR_DY, _S["badge"]["height"]
LABEL_Y, LABEL_H = _S["label"]["y"], _S["label"]["height"]
LABEL_SHOW, LABEL_FADE, LABEL_RISE = _S["label"]["show_seconds"], _S["label"]["fade_seconds"], _S["label"]["rise_px"]
HELPER = Path(__file__).with_name("_pngtext.py")


class RenderError(Exception):
    pass


def caption_timing(moment: dict) -> list[tuple[str, float, float]]:
    """(text, start, end) in seconds relative to the cut start, from the audio word timings."""
    words, t0 = moment["words"], moment["start"]
    dur = moment["end"] - moment["start"]
    lines = moment["lines"]
    out = []
    for i, ln in enumerate(lines):
        a = words[ln["first"]][1] - t0 - 0.04
        b = words[ln["last"]][2] - t0 + 0.30
        if i + 1 < len(lines):
            b = min(b, words[lines[i + 1]["first"]][1] - t0 - 0.03)
        out.append((ln["text"], max(0.0, a), min(dur, max(b, a + 0.6))))
    return out


def png_spec(spec: dict, order: list[str], workdir: Path, labels: dict | None = None, label_colors: dict | None = None) -> dict:
    """labels: {rank (str): title}. The pill shows the title only - the badge is the single visible rank number."""
    h, bd, lb, cp, hk = _S["header"], _S["badge"], _S["label"], _S["captions"], _S["hook"]
    items = []
    n = len(order)
    for k, rank in enumerate(order):
        # header: title + countdown chips (current rank highlighted)
        rects, blocks = [], [{"text": spec["title"], "font": "bold", "size": h["title_size"], "cx": W // 2, "y": h["title_y"] + HDR_DY,
                              "stroke": h["title_stroke"], "shadow": h["title_shadow"], "max_width": h["title_max_width"]}]
        cw, gap = h["chip_width"], h["chip_gap"]
        x0 = (W - (n * cw + (n - 1) * gap)) // 2
        for j, r in enumerate(order):
            x = x0 + j * (cw + gap)
            cur = r == rank
            rects.append({"box": [x, h["chip_y"] + HDR_DY, x + cw, h["chip_y"] + h["chip_height"] + HDR_DY], "radius": h["chip_radius"],
                          "fill": h["chip_current_fill"] if cur else h["chip_other_fill"]})
            blocks.append({"text": f"#{r}", "font": "bold", "size": h["chip_text_size"], "cx": x + cw // 2,
                           "y": h["chip_y"] + h["chip_text_dy"] + HDR_DY,
                           "fill": h["chip_current_text"] if cur else h["chip_other_text"], "stroke": 0})
        items.append({"out": str(workdir / f"header{rank}.png"), "w": W, "h": 340 + HDR_DY, "rects": rects, "blocks": blocks})
        items.append({"out": str(workdir / f"badge{rank}.png"), "w": W, "h": BADGE_H,
                      "blocks": [{"text": f"#{rank}", "font": bd["font"], "size": bd["size"], "cx": W // 2, "y": 8,
                                  "stroke": bd["stroke"], "shadow": bd["shadow"]}]})
        for i, (text, _, _) in enumerate(caption_timing(spec["moments"][rank])):
            items.append({"out": str(workdir / f"cap{rank}_{i:02d}.png"), "w": W, "h": CAP_H,
                          "blocks": [{"text": text, "font": cp["font"], "size": cp["size"], "cx": W // 2, "y": 20, "stroke": cp["stroke"],
                                      "shadow": cp["shadow"], "max_width": CAP_MAX_W, "line_spacing": cp["line_spacing"]}]})
    for rank, text in (labels or {}).items():
        items.append({"out": str(workdir / f"label{rank}.png"), "w": W, "h": LABEL_H, "pill": dict(lb["background"]),
                      "blocks": [{"text": text, "font": lb["font"], "size": lb["size"], "cx": W // 2, "y": lb["text_y"],
                                  "fill": _fill(label_colors, rank, lb["color"]), "stroke": 0}]})
    items.append({"out": str(workdir / "hook.png"), "w": W, "h": hk["height"],
                  "blocks": [{"text": hk["text"], "font": "bold", "size": hk["size"], "cx": W // 2, "y": 8,
                              "fill": hk["fill"], "stroke": hk["stroke"], "shadow": hk["shadow"]}]})
    return {"items": items}


def _fill(label_colors: dict | None, rank: str, default):
    """RGBA fill for a title: the assigned solid accent color (only the text fill varies; everything else is the approved label)."""
    if not label_colors or rank not in label_colors:
        return default
    r, g, b = colors_mod.to_rgb(label_colors[rank])
    return [r, g, b, 255]


def clip_command(spec, rank, workdir, media, out, first, label=False, src_dims=None):
    m = spec["moments"][rank]
    fr = framing.analyze(*(src_dims or framing.reference()))
    band = f"{W}:{BAND_H}" if fr["ok"] else f"{W}:-2"      # unmappable aspect: uncropped fit (output goes to needs-review)
    dur = frame_exact(m)
    caps = caption_timing(m)
    bg, bd, hk, ve = _S["background"], _S["badge"], _S["hook"], _S["video_encode"]
    inputs = ["-ss", f"{m['start']}", "-t", f"{dur}", "-i", str(media),
              "-i", str(workdir / f"header{rank}.png"),
              "-loop", "1", "-framerate", str(FPS), "-t", f"{dur}", "-i", str(workdir / f"badge{rank}.png")]
    # crop first (by ratio) so the blurred backdrop never contains a trace of the old caption strip
    f = [f"[0:v]{framing.crop_filter(fr)},split=2[a][b]",
         f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma={bg['blur_sigma']},eq=brightness={bg['brightness']}:saturation={bg['saturation']},setsar=1[bg]",
         f"[b]scale={band},setsar=1[fg]",
         f"[bg][fg]overlay=0:{BAND_Y}[v0]", f"[v0][1:v]overlay=0:0[v1]",
         f"[2:v]format=rgba,fade=t=in:st=0:d={bd['fade_in']}:alpha=1[bd]",
         f"[v1][bd]overlay=x=0:y='{BADGE_Y}+{bd['slide_px']}*pow(max(0,1-t/{bd['slide_seconds']}),2)'[v2]"]
    last, idx = "v2", 3
    if first:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur}", "-i", str(workdir / "hook.png")]
        f.append("[%d:v]format=rgba,fade=t=in:st=%s:d=%s:alpha=1,fade=t=out:st=%s:d=%s:alpha=1[hk]"
                 % (idx, hk["fade_in_start"], hk["fade_in"], hk["fade_out_start"], hk["fade_out"]))
        f.append(f"[{last}][hk]overlay=0:{hk['y'] + HDR_DY}[vh]")
        last, idx = "vh", idx + 1
    if label:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{LABEL_SHOW}", "-i", str(workdir / f"label{rank}.png")]
        f.append(f"[{idx}:v]format=rgba,fade=t=in:st=0:d={LABEL_FADE}:alpha=1,"
                 f"fade=t=out:st={LABEL_SHOW - LABEL_FADE}:d={LABEL_FADE}:alpha=1[lb]")
        f.append(f"[{last}][lb]overlay=x=0:y='{LABEL_Y}+{LABEL_RISE}*pow(max(0,1-t/{LABEL_FADE}),2)':eof_action=pass[vl]")
        last, idx = "vl", idx + 1
    for i, (_, a, b) in enumerate(caps):
        inputs += ["-i", str(workdir / f"cap{rank}_{i:02d}.png")]
        f.append(f"[{last}][{idx}:v]overlay=0:{CAP_Y}:enable='between(t,{a:.3f},{b:.3f})'[c{i}]")
        last, idx = f"c{i}", idx + 1
    f.append(f"[{last}]fps={FPS},format={ve['pix_fmt']}[vout]")
    return (["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex", ";".join(f), "-map", "[vout]", "-an",
            "-c:v", ve["codec"], "-preset", ve["preset"], "-crf", str(ve["crf"]), "-profile:v", ve["profile"], "-pix_fmt", ve["pix_fmt"], str(out)])


def frame_exact(moment: dict) -> float:
    """Cut length rounded to whole frames so video and audio stay the same length (no drift across cuts)."""
    return round((moment["end"] - moment["start"]) * FPS) / FPS


def audio_command(spec, order, media, out):
    """One continuous audio track from the original audio (30 ms edge fades only), encoded once."""
    au = _S["audio"]
    ef = au["edge_fade_seconds"]
    inputs, chains = [], []
    for i, rank in enumerate(order):
        m = spec["moments"][rank]
        d = frame_exact(m)
        inputs += ["-ss", f"{m['start']}", "-t", f"{d}", "-i", str(media)]
        chains.append(f"[{i}:a]afade=t=in:st=0:d={ef},afade=t=out:st={d - ef:.3f}:d={ef},aresample={au['sample_rate']}[a{i}]")
    join = "".join(f"[a{i}]" for i in range(len(order))) + f"concat=n={len(order)}:v=0:a=1[aout]"
    return (["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex", ";".join(chains + [join]), "-map", "[aout]",
            "-c:a", au["codec"], "-b:a", au["bitrate"], str(out)])


def load_labels(path: Path, clip_ranks: list[str]) -> dict:
    """labels.json is a list of {"rank": int, "title": str}. Returns {rank_str: title} for the label pill.

    The rank must match the clip's own rank (its badge, sequence position and countdown); '#N' never lives in the title.
    """
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise RenderError("labels file must be a list of {rank, title}; the legacy {rank: '#N text'} form is no longer accepted")
    try:
        entries = titles.validate_entries(data, [int(r) for r in clip_ranks])
    except titles.TitleError as exc:
        raise RenderError(f"labels: {exc}") from exc
    return {str(e["rank"]): e["title"] for e in entries}


def _text_gate(spec: dict, labels_path) -> None:
    """Severe slurs/hateful language never reach a render (ordinary profanity is fine)."""
    labels = json.loads(Path(labels_path).read_text()) if labels_path else None
    try:
        textsafety.assert_clean(textsafety.generated_text(None, spec, labels if isinstance(labels, list) else None))
        captions_mod.assert_agreed(spec)                 # wording must be confirmed by two independent decodes (or a supplied transcript)
    except (textsafety.TextSafetyError, captions_mod.CaptionError) as exc:
        raise RenderError(str(exc)) from exc


def source_dims(media: Path) -> tuple[int, int]:
    v = source_library.probe_media(media)["video"]
    if not v:
        raise RenderError(f"{media.name} has no video stream")
    return v["width"], v["height"]


def framing_note(dims: tuple[int, int]) -> None:
    fr = framing.analyze(*dims)
    if not fr["ok"]:
        print(f"WARNING: {fr['reason']}; rendering an uncropped fit - this output must go to needs-review")


def render_ranking(video_id: str, spec_path: Path, out_path: Path, only_rank: str | None = None,
                   labels_path: Path | None = None) -> Path:
    if out_path.exists():
        raise RenderError(f"refusing to overwrite {out_path.name}")
    spec = json.loads(Path(spec_path).read_text())
    _text_gate(spec, labels_path)
    media = source_library.resolve(video_id).media
    dims = source_dims(media)
    framing_note(dims)
    labels = load_labels(labels_path, list(spec["moments"])) if labels_path else None
    order = sorted(spec["moments"], key=int, reverse=True)  # countdown: #4 first, #1 last
    label_colors = None
    if labels and style.load()["label"]["title_accent"]["enabled"]:
        run_id = next((p for p in Path(out_path).resolve().parts if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}(-\d+)?", p)), "no-run")
        seed = spec.get("color_seed") or colors_mod.seed_for(run_id, str(spec.get("video_id", video_id)), spec.get("title", ""))
        label_colors = colors_mod.resolve(spec.get("label_colors"), seed, order)          # palette + contrast checked BEFORE rendering
        problems = colors_mod.validate(label_colors, order)
        if problems:
            raise RenderError("title colors: " + "; ".join(problems))
        if spec.get("label_colors") != label_colors:                                       # record the chosen colors in the ranking specification
            spec_file = Path(spec_path)
            spec_file.write_text(json.dumps({**spec, "label_colors": label_colors, "color_seed": seed}, indent=2))
    with tempfile.TemporaryDirectory(prefix="clipagent-render-") as tmp:
        work = Path(tmp)
        (work / "spec.json").write_text(json.dumps(png_spec(spec, order, work, labels, label_colors)))
        p = subprocess.run([_S["fonts"]["renderer_python"], str(HELPER), str(work / "spec.json")], capture_output=True, text=True)
        if p.returncode:
            raise RenderError(f"text rendering failed: {p.stderr.strip()}")
        parts = []
        for k, rank in enumerate(order):
            if only_rank and rank != only_rank:
                continue
            part = work / f"part{rank}.mp4"
            r = subprocess.run(clip_command(spec, rank, work, media, part, first=(k == 0), label=bool(labels), src_dims=dims), capture_output=True, text=True)
            if r.returncode:
                raise RenderError(f"ffmpeg failed on #{rank}: {r.stderr.strip()[-600:]}")
            parts.append(part)
        listing = work / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        video = work / "video.mp4"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(video)],
                           capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"concat failed: {r.stderr.strip()[-400:]}")
        ranks = [x for x in order if not only_rank or x == only_rank]
        audio = work / "audio.m4a"
        r = subprocess.run(audio_command(spec, ranks, media, audio), capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"audio failed: {r.stderr.strip()[-400:]}")
        r = subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a",
                            "-c", "copy", *(["-movflags", "+faststart"] if _S["container"]["faststart"] else []), str(out_path)], capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"mux failed: {r.stderr.strip()[-400:]}")
    side = Path(str(out_path) + ".render.json")                       # sidecar read by finalize-video (colors, seed, framing)
    side.write_text(json.dumps({"video_id": video_id, "label_colors": label_colors, "color_seed": (spec.get("color_seed") or None),
                                "source_dims": list(dims), "framing_ok": framing.analyze(*dims)["ok"]}, indent=2))
    return out_path


# ---- standalone format: one moment, same crop/blur/safe margins/audio/captions, NO ranking graphics -------------------
STANDALONE_RANK = "1"


def standalone_png_spec(spec: dict, workdir: Path, headline: str | None = None, color: str | None = None) -> dict:
    cp, lb = _S["captions"], _S["label"]
    items = [{"out": str(workdir / f"cap{STANDALONE_RANK}_{i:02d}.png"), "w": W, "h": CAP_H,
              "blocks": [{"text": text, "font": cp["font"], "size": cp["size"], "cx": W // 2, "y": 20, "stroke": cp["stroke"],
                          "shadow": cp["shadow"], "max_width": CAP_MAX_W, "line_spacing": cp["line_spacing"]}]}
             for i, (text, _, _) in enumerate(caption_timing(spec["moments"][STANDALONE_RANK]))]
    if headline:                                     # funny / continuous videos: the approved label pill with the headline (no rank badge)
        items.append({"out": str(workdir / "label1.png"), "w": W, "h": LABEL_H, "pill": dict(lb["background"]),
                      "blocks": [{"text": headline, "font": lb["font"], "size": lb["size"], "cx": W // 2, "y": lb["text_y"],
                                  "fill": _fill({"1": color} if color else None, "1", lb["color"]), "stroke": 0}]})
    return {"items": items}


def standalone_clip_command(spec: dict, workdir: Path, media: Path, out: Path, src_dims=None, headline: bool = False) -> list[str]:
    m = spec["moments"][STANDALONE_RANK]
    fr = framing.analyze(*(src_dims or framing.reference()))
    band = f"{W}:{BAND_H}" if fr["ok"] else f"{W}:-2"
    dur = frame_exact(m)
    caps = caption_timing(m)
    bg, ve = _S["background"], _S["video_encode"]
    inputs = ["-ss", f"{m['start']}", "-t", f"{dur}", "-i", str(media)]
    f = [f"[0:v]{framing.crop_filter(fr)},split=2[a][b]",
         f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma={bg['blur_sigma']},eq=brightness={bg['brightness']}:saturation={bg['saturation']},setsar=1[bg]",
         f"[b]scale={band},setsar=1[fg]",
         f"[bg][fg]overlay=0:{BAND_Y}[v0]"]
    last, idx = "v0", 1
    if headline:
        show = _S["label"]["single_show_seconds"]
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{show}", "-i", str(workdir / "label1.png")]
        f.append(f"[{idx}:v]format=rgba,fade=t=in:st=0:d={LABEL_FADE}:alpha=1,fade=t=out:st={show - LABEL_FADE}:d={LABEL_FADE}:alpha=1[lb]")
        f.append(f"[{last}][lb]overlay=x=0:y='{LABEL_Y}+{LABEL_RISE}*pow(max(0,1-t/{LABEL_FADE}),2)':eof_action=pass[vl]")
        last, idx = "vl", idx + 1
    for i, (_, a, b) in enumerate(caps):
        inputs += ["-i", str(workdir / f"cap{STANDALONE_RANK}_{i:02d}.png")]
        f.append(f"[{last}][{idx}:v]overlay=0:{CAP_Y}:enable='between(t,{a:.3f},{b:.3f})'[c{i}]")
        last, idx = f"c{i}", idx + 1
    f.append(f"[{last}]fps={FPS},format={ve['pix_fmt']}[vout]")
    return (["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex", ";".join(f), "-map", "[vout]", "-an",
            "-c:v", ve["codec"], "-preset", ve["preset"], "-crf", str(ve["crf"]), "-profile:v", ve["profile"], "-pix_fmt", ve["pix_fmt"], str(out)])


def render_standalone(video_id: str, spec_path: Path, out_path: Path) -> Path:
    """Spec: {"title": str, "moments": {"1": {start, end, words, lines}}}. Never overwrites; nothing is uploaded."""
    if out_path.exists():
        raise RenderError(f"refusing to overwrite {out_path.name}")
    spec = json.loads(Path(spec_path).read_text())
    if list(spec.get("moments", {})) != [STANDALONE_RANK]:
        raise RenderError("a standalone spec has exactly one moment, keyed \"1\"")
    _text_gate(spec, None)
    media = source_library.resolve(video_id, require_active=False).media
    dims = source_dims(media)
    framing_note(dims)
    headline, accent = spec.get("headline"), None
    if headline:
        titles.validate_headline(headline)
        run_id = next((p for p in Path(out_path).resolve().parts if re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}(-\d+)?", p)), "no-run")
        seed = spec.get("color_seed") or colors_mod.seed_for(run_id, str(spec.get("video_id", video_id)), headline)
        accent = colors_mod.resolve({"1": spec["accent_color"]} if spec.get("accent_color") else None, seed, ["1"])["1"]
        if spec.get("accent_color") != accent:
            Path(spec_path).write_text(json.dumps({**spec, "accent_color": accent, "color_seed": seed}, indent=2))
    with tempfile.TemporaryDirectory(prefix="clipagent-standalone-") as tmp:
        work = Path(tmp)
        (work / "spec.json").write_text(json.dumps(standalone_png_spec(spec, work, headline, accent)))
        p = subprocess.run([_S["fonts"]["renderer_python"], str(HELPER), str(work / "spec.json")], capture_output=True, text=True)
        if p.returncode:
            raise RenderError(f"text rendering failed: {p.stderr.strip()}")
        video, audio = work / "video.mp4", work / "audio.m4a"
        r = subprocess.run(standalone_clip_command(spec, work, media, video, dims, bool(headline)), capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"ffmpeg failed: {r.stderr.strip()[-600:]}")
        r = subprocess.run(audio_command(spec, [STANDALONE_RANK], media, audio), capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"audio failed: {r.stderr.strip()[-400:]}")
        r = subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a",
                            "-c", "copy", *(["-movflags", "+faststart"] if _S["container"]["faststart"] else []), str(out_path)],
                           capture_output=True, text=True)
        if r.returncode:
            raise RenderError(f"mux failed: {r.stderr.strip()[-400:]}")
    Path(str(out_path) + ".render.json").write_text(json.dumps({"video_id": video_id, "label_colors": {"1": accent} if accent else None,
                                                                 "color_seed": spec.get("color_seed"), "source_dims": list(dims),
                                                                 "framing_ok": framing.analyze(*dims)["ok"], "headline": headline}, indent=2))
    return out_path
