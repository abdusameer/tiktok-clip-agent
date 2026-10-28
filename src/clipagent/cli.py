from pathlib import Path

import typer

import json

from . import analyze as analyze_mod
from . import args as args_mod
from . import captions as captions_mod
from . import cleanup as cleanup_mod
from . import contentflow as cf_mod
from . import segments as seg_mod
from .status import RunStatus
from . import fullanalysis as fa_mod
from . import textsafety as ts_mod
from . import manifest as manifest_mod
from . import plan as plan_mod
from . import production as production_mod
from . import qa as qa_mod
from . import runs as runs_mod
from . import source_library as lib_mod
from . import titles as titles_mod
from . import usage as usage_mod
from . import doctor as doctor_mod
from . import ingest as ingest_mod
from . import merge as merge_mod
from . import render as render_mod
from . import transcribe as transcribe_mod

app = typer.Typer(help="Local TikTok clip agent for authorized videos.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Local TikTok clip agent for authorized videos."""


@app.command()
def doctor() -> None:
    """Check required software."""
    checks = doctor_mod.run_checks()
    for c in checks:
        mark = "OK  " if c.ok else ("FAIL" if c.required else "WARN")
        typer.echo(f"[{mark}] {c.name}: {c.detail}")
    if not doctor_mod.all_required_ok(checks):
        raise typer.Exit(code=1)


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="Local video file"),
    rights: str = typer.Option(..., "--rights", help="How you are authorized to use this video"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Register an authorized local video."""
    if not yes:
        typer.confirm(f"Confirm you are authorized to clip '{path.name}' ({rights})?", abort=True)
    try:
        rec = ingest_mod.ingest(path, rights)
    except ingest_mod.IngestError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Ingested {rec['id']}: {rec['duration_sec']:.1f}s {rec['width']}x{rec['height']}, audio={rec['has_audio']}")


@app.command()
def transcribe(
    video_id: str = typer.Argument(..., help="Id of an ingested video"),
    model: str = typer.Option(transcribe_mod.DEFAULT_MODEL, "--model", help="faster-whisper model name"),
) -> None:
    """Produce timestamped speech (JSON, SRT, TXT) for an ingested video."""
    last = {"t": -60.0}

    def progress(t: float) -> None:
        if t - last["t"] >= 60:
            last["t"] = t
            typer.echo(f"  ...transcribed up to {transcribe_mod.clock(t)}", err=True)

    try:
        res = transcribe_mod.transcribe(video_id, model, progress)
    except transcribe_mod.TranscribeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{len(res['record']['segments'])} segments written:")
    for p in res["paths"].values():
        typer.echo(f"  {p}")


@app.command("merge-transcripts")
def merge_transcripts(
    video_id: str = typer.Argument(..., help="Id of an ingested video that already has a small.en transcript"),
    core1: Path = typer.Option(..., "--core1", help="Core1-style transcript (text + speaker labels)"),
) -> None:
    """Merge Core1 text/speakers with small.en timing into <id>.merged.* (no model download)."""
    try:
        res = merge_mod.merge(video_id, core1, progress=lambda m: typer.echo(m, err=True))
    except (merge_mod.MergeError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{res['record']['stats']}")
    for p in res["paths"].values():
        typer.echo(f"  {p}")


@app.command()
def analyze(
    video_id: str = typer.Argument(..., help="Id of an ingested video with a merged transcript"),
    limit: int = typer.Option(60, "--limit", help="How many ranked, non-overlapping moments to verify"),
) -> None:
    """Rank possible moments and verify each against the video's audio and frames."""
    try:
        res = analyze_mod.analyze(video_id, limit=limit, progress=lambda m: typer.echo(m, err=True))
    except (FileExistsError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    c = res["record"]["candidates"]
    typer.echo(f"{len(c)} candidates, {sum(x['verified'] for x in c)} verified -> {res['path']}")


@app.command()
def rankings(
    video_id: str = typer.Argument(..., help="Id of an ingested video with a merged transcript"),
    curation: Path = typer.Option(..., "--curation", help="JSON of hand-picked moments (segment ids)"),
) -> None:
    """Verify hand-picked ranking moments against the video and write exact timestamps."""
    try:
        res = analyze_mod.build_rankings(video_id, curation, progress=lambda m: typer.echo(m, err=True))
    except (FileExistsError, FileNotFoundError, KeyError) as exc:
        typer.echo(f"Error: {exc!r}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{len(res['record']['tiktoks'])} TikTok rankings -> {res['path']}")
    typer.echo(f"report -> {analyze_mod.write_report(video_id)}")


@app.command()
def render(
    video_id: str = typer.Argument(..., help="Id of an ingested video"),
    captions: Path = typer.Option(..., "--captions", help="Caption/timing spec for one ranking"),
    out: Path = typer.Option(..., "--out", help="Output .mp4 (never overwritten)"),
    only_rank: str = typer.Option(None, "--only-rank", help="Render just one moment (for quick checks)"),
    labels: Path = typer.Option(None, "--labels", help="JSON {rank: label text} shown ~2 s at the start of each moment"),
) -> None:
    """Render a vertical 1080x1920 ranking draft locally. Does not upload anything."""
    try:
        path = render_mod.render_ranking(video_id, captions, out, only_rank, labels)
    except (render_mod.RenderError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rendered -> {path}")


@app.command("render-standalone")
def render_standalone(
    video_id: str = typer.Argument(..., help="Id of a library source"),
    captions: Path = typer.Option(..., "--captions", help="Spec: {title, moments: {\"1\": {start,end,words,lines}}}"),
    out: Path = typer.Option(..., "--out", help="Output .mp4 (never overwritten)"),
) -> None:
    """Render a single-moment vertical video (same crop/blur/captions/audio; no ranking graphics). Uploads nothing."""
    try:
        path = render_mod.render_standalone(video_id, captions, out)
    except (render_mod.RenderError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rendered -> {path}")


@app.command("finalize-video")
def finalize_video(run_id: str = typer.Argument(...),
                   render: str = typer.Option(..., "--render", help="Render path relative to runs/<id>/"),
                   fmt: str = typer.Option("ranking", "--format", help="ranking | standalone"),
                   meta: Path = typer.Option(..., "--meta", help="JSON: source_id, video_title, cover_title, caption, hashtags, moments")) -> None:
    """QA a render, then file it in approved/<run-id>/ (auto-QA pass) or needs-review/<run-id>/. Never stops the run on one failure."""
    rec = production_mod.finalize_video(run_id, render, fmt, json.loads(meta.read_text()))
    typer.echo(json.dumps({k: rec.get(k) for k in ("status", "final", "approval", "qa_verdict", "needs_review_reasons", "error")}, indent=2))


@app.command("auto-cleanup")
def auto_cleanup(run_id: str = typer.Argument(...),
                 source_id: str = typer.Option(None, "--source-id", help="Needed only with --inputs"),
                 inputs: bool = typer.Option(False, "--inputs", help="Also move separate input copies once the master is verified")) -> None:
    """Release reservations and move allowlisted run-specific files to the Trash (recoverable). Never touches finals or sources."""
    res = production_mod.auto_cleanup(run_id, source_id=source_id, include_inputs=inputs)
    typer.echo(json.dumps({k: res.get(k) for k in ("result", "reason", "bytes", "problems")} | {"moved": len(res["moved"])}, indent=2))


@app.command("production-report")
def production_report(run_id: str = typer.Argument(...)) -> None:
    """One final report for an unattended production run."""
    typer.echo(production_mod.format_report(production_mod.report(run_id)))


@app.command("prepare-run")
def prepare_run(
    video: Path = typer.Option(None, "--video", help="Source video (legacy mode)"),
    audio: Path = typer.Option(None, "--audio", help="Separate audio file to combine (video is not re-encoded)"),
    transcript: Path = typer.Option(None, "--transcript", help="Existing transcript to normalize/merge"),
    source_id: str = typer.Option(None, "--source-id", help="Reuse the canonical master of a source in source_library/"),
) -> None:
    """Validate inputs and create a unique runs/<run-id>/ production run. Originals are never modified."""
    try:
        if source_id:
            if video or audio:
                raise args_mod.ArgError("--source-id cannot be combined with --video/--audio")
            ref = lib_mod.resolve(source_id)            # must exist and be active
            if not ref.library:
                raise args_mod.ArgError(f"{source_id} is a legacy source, not in source_library/")
            a = args_mod.MakeRankingsArgs(ref.media, None, transcript)
            run_dir = runs_mod.create_run(ref.media, None, transcript)
        else:
            if not video:
                raise args_mod.ArgError("give --video (optionally --audio) or --source-id")
            a = args_mod.validate(args_mod.MakeRankingsArgs(video, audio, transcript))
            run_dir = runs_mod.create_run(a.video, a.audio, a.transcript)
    except (args_mod.ArgError, runs_mod.RunError, lib_mod.SourceError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    log = usage_mod.UsageLog(run_dir)
    log.d["source"] = {"duration_sec": ingest_mod.probe(a.video)["duration_sec"], "size_bytes": a.video.stat().st_size}
    source = a.video
    if a.audio:
        with log.stage("combine_av"):
            source = runs_mod.combine(a.video, a.audio, run_dir / "source" / "combined.mp4", log)
    log.save()
    typer.echo(f"run {run_dir.name}\nsource {source}\ntranscript {a.transcript or 'none (reuse library transcript or transcribe with small.en)'}\ndir {run_dir}")


@app.command("ingest-source")
def ingest_source_cmd(
    video: Path = typer.Option(..., "--video"),
    audio: Path = typer.Option(None, "--audio"),
    rights: str = typer.Option(..., "--rights", help="How you are authorized to use this video"),
    new_source: bool = typer.Option(False, "--new-source", help="User decided this is a NEW source even if probes look similar"),
    note: str = typer.Option(None, "--identity-note", help="Recorded evidence/decision about identity"),
) -> None:
    """Combine video(+audio) into source_library/<id>/master.mp4 after validation; never overwrites a different master."""
    try:
        res = lib_mod.ingest_source(video, audio, rights=rights, skip_identity=new_source,
                                    identity_evidence={"decision": "new_source", "decided_by": "user", "note": note} if new_source else None)
    except lib_mod.IdentityUncertain as exc:
        typer.echo(f"STOP - ask the user: {exc}\n{json.dumps(exc.details, indent=1)}", err=True)
        raise typer.Exit(code=2)
    except lib_mod.SourceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps({k: v for k, v in res.items() if k != "meta"}, indent=2))


@app.command("deps")
def deps_cmd() -> None:
    """Report which tools are installed (yt-dlp, ffmpeg, ffprobe, swiftc, Pillow, faster-whisper). Never installs anything."""
    d = fa_mod.dependencies()
    for k, v in d.items():
        typer.echo(f"[{'OK  ' if v else 'MISSING'}] {k}: {v if isinstance(v, str) else ('yes' if v else 'not found')}")


@app.command("youtube-info")
def youtube_info_cmd(url: str = typer.Argument(...)) -> None:
    """Metadata only (id, title, duration, chapters, available captions). Downloads nothing."""
    try:
        typer.echo(json.dumps(fa_mod.youtube_info(url), indent=2))
    except fa_mod.AnalysisError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("prepare-source")
def prepare_source_cmd(
    url: str = typer.Option(None, "--url", help="YouTube URL (needs --rights; downloads only when authorized)"),
    video: Path = typer.Option(None, "--video"),
    audio: Path = typer.Option(None, "--audio"),
    source_id: str = typer.Option(None, "--source-id", help="Reuse a stored source (nothing is downloaded again)"),
    rights: str = typer.Option(None, "--rights", help="How you are authorized to download/use this video"),
    new_source: bool = typer.Option(False, "--new-source"),
    note: str = typer.Option(None, "--identity-note"),
) -> None:
    """Resolve the /make-rankings input into ONE canonical source in the library (download -> combine -> verify -> master.mp4)."""
    import shutil as _sh
    try:
        if sum(bool(x) for x in (url, video, source_id)) != 1:
            raise fa_mod.AnalysisError("give exactly one of --url, --video, --source-id")
        if source_id:
            lib_mod.resolve(source_id, require_active=False)
            typer.echo(json.dumps({"source_id": source_id, "reused": True}))
            return
        yt = None
        if url:
            fa_mod.require("yt-dlp", "ffmpeg", "ffprobe")
            got = fa_mod.youtube_download(url, rights or "")
            video, yt = got["video"], got
            rights = f"{rights} (YouTube {got['info']['video_id']}: {got['info']['title']!r})"
        res = lib_mod.ingest_source(video, audio, rights=rights or "", skip_identity=new_source,
                                    identity_evidence={"decision": "new_source", "decided_by": "user", "note": note} if new_source else None)
        sid = res["source_id"]
        if yt:
            d = config_root() / "source_library" / sid
            (d / "youtube.json").write_text(json.dumps(yt["info"], indent=2))
            (d / "incoming").mkdir(exist_ok=True)
            for c in yt["captions"]:
                _sh.copy2(c, d / "incoming" / c.name)
        typer.echo(json.dumps({"source_id": sid, "reused": not res.get("created", True), "youtube": yt["info"] if yt else None}, indent=2))
    except lib_mod.IdentityUncertain as exc:
        typer.echo(f"STOP - ask the user: {exc}\n{json.dumps(exc.details, indent=1)}", err=True)
        raise typer.Exit(code=2)
    except (lib_mod.SourceError, fa_mod.AnalysisError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


def config_root() -> Path:
    from . import config as _c
    return _c.project_root()


@app.command("analyze-source")
def analyze_source_cmd(source_id: str = typer.Argument(...), force: bool = typer.Option(False, "--force", help="Redo even if analysis exists"),
                       candidates_only: bool = typer.Option(False, "--candidates-only", help="Recompute candidates from the stored analysis")) -> None:
    """Full-video analysis (transcript, scene cuts, frames, contact sheets, OCR, motion/audio signals, candidates with evidence). Reused when it exists."""
    if candidates_only:
        try:
            typer.echo(json.dumps(fa_mod.rebuild_candidates(source_id)["counts"]))
        except fa_mod.AnalysisError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)
        return
    try:
        d = config_root() / "source_library" / source_id
        caps = sorted((d / "incoming").glob("*.vtt")) if (d / "incoming").is_dir() else []
        res = fa_mod.analyze_source(source_id, force=force, captions=caps)
    except (fa_mod.AnalysisError, lib_mod.SourceError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    a = res["analysis"]
    typer.echo(json.dumps({"reused": res["reused"], "duration_sec": a["probe"]["duration_sec"], "detections": a.get("detections"),
                           "frames": a["frames"]["count"], "contact_sheets": len(a["contact_sheets"]),
                           "ocr_available": a["ocr"]["available"], "transcript": a["transcript"]}, indent=2))


@app.command("apply-reviews")
def apply_reviews_cmd(source_id: str = typer.Argument(...), reviews: Path = typer.Argument(..., help="JSON list of candidate reviews")) -> None:
    """Record visual reviews (what is visibly happening, context, confidence, format) for candidates; cites the frames viewed."""
    try:
        typer.echo(json.dumps(fa_mod.apply_reviews(source_id, json.loads(reviews.read_text())), indent=2))
    except fa_mod.AnalysisError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("evidence-report")
def evidence_report_cmd(source_id: str = typer.Argument(...), sample: int = typer.Option(5, "--sample"),
                        seed: int = typer.Option(None, "--seed")) -> None:
    """Random sample of detected moments with timestamps, transcript evidence and frame paths (spot-check against the real video)."""
    try:
        typer.echo(fa_mod.sample_report(source_id, sample, seed))
    except fa_mod.AnalysisError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("decode-moment")
def decode_moment_cmd(source_id: str = typer.Argument(...), start: float = typer.Option(..., "--start"), end: float = typer.Option(..., "--end"),
                      out: Path = typer.Option(..., "--out", help="JSON with both decodes + caption lines built only from agreed words")) -> None:
    """Two independent decodes (beam 1 and 5) of a moment; suggested caption lines contain ONLY words both decodes confirm."""
    try:
        ref = lib_mod.resolve(source_id, require_active=False)
        res = captions_mod.decode_moment(ref.media, start, end)
    except (lib_mod.SourceError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    out.write_text(json.dumps({"start": start, "end": end, **res}, indent=2))
    typer.echo(f"{len(res['suggested_lines'])} caption lines from words both decodes agree on -> {out}")


@app.command("scan-chat")
def scan_chat_cmd(source_id: str = typer.Argument(...), start: float = typer.Option(..., "--start"), end: float = typer.Option(..., "--end"),
                  step: float = typer.Option(1.0, "--step")) -> None:
    """OCR the whole visible frame across a moment; report severe language, unreadable text and clean spans to trim to."""
    try:
        ref = lib_mod.resolve(source_id, require_active=False)
        sc = ts_mod.scan_moment(ref.media, start, end, step=step)
    except (lib_mod.SourceError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    sc["trim_to"] = ts_mod.trim_suggestion(sc)
    typer.echo(json.dumps(sc, indent=2))
    if sc["findings"]:
        raise typer.Exit(code=1)


@app.command("content-intake")
def content_intake_cmd(rights: str = typer.Option(None, "--rights", help="Optional; defaults to the user-supplied-file note")) -> None:
    """Scan sourcevideo/ (and loose media in source_library/), pair each video with its audio, ingest every one and create a run. Prints one JSON line per source."""
    from . import intake
    found = intake.scan()
    if not found:
        typer.echo("Nothing to process: put the video (and its audio, if separate) in sourcevideo/", err=True)
        raise typer.Exit(code=1)
    code = 0
    for f in found:
        try:
            res = lib_mod.ingest_source(f["video"], f["audio"], rights=rights or f"user-supplied local file {f['video'].name} placed in {f['where']}/ by the user for /make-content; no separate rights detail")
            d = cf_mod.start(res["source_id"])
            typer.echo(json.dumps({"run_id": d.name, "source_id": res["source_id"], "video": f["video"].name, "audio": f["audio"].name if f["audio"] else None}))
        except lib_mod.IdentityUncertain as exc:
            typer.echo(f"STOP - identity needs a decision for {f['video'].name}: {exc}", err=True)
            code = 2
        except (cf_mod.ContentError, lib_mod.SourceError, FileNotFoundError) as exc:
            typer.echo(f"Error for {f['video'].name}: {exc}", err=True)
            code = code or 1
    raise typer.Exit(code=code)


@app.command("content-start")
def content_start_cmd(source_id: str = typer.Option(None, "--source-id"), video: Path = typer.Option(None, "--video"), audio: Path = typer.Option(None, "--audio"),
                      rights: str = typer.Option(None, "--rights", help="Optional; defaults to the user-supplied-file note")) -> None:
    """/make-content step 1: ingest (or reuse) the source and create runs/<run-id>/ with STATUS.md. Prints the run id."""
    try:
        if bool(source_id) == bool(video):
            raise cf_mod.ContentError("give exactly one of --source-id or --video")
        if video:
            res = lib_mod.ingest_source(video, audio, rights=rights or f"user-supplied local file {video.name} given to /make-content; no separate rights detail")
            source_id = res["source_id"]
        else:
            ref = lib_mod.resolve(source_id, require_active=False)
            if ref.meta.get("status") == "purged":
                raise cf_mod.ContentError("source is purged; supply the media again")
        d = cf_mod.start(source_id)
    except lib_mod.IdentityUncertain as exc:
        typer.echo(f"STOP - identity needs a decision: {exc}", err=True)
        raise typer.Exit(code=2)
    except (cf_mod.ContentError, lib_mod.SourceError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps({"run_id": d.name, "source_id": source_id}))


@app.command("content-segment")
def content_segment_cmd(run_id: str = typer.Argument(...)) -> None:
    """Analyze the whole source (reused when present), detect every segment, account for the full timeline and write the catalog."""
    d = cf_mod.run_dir_for(run_id)
    st = RunStatus(d)
    sid = json.loads((d / "ingest" / "source.json").read_text())["source_id"]
    try:
        st.stage("ANALYZING")
        caps = sorted((cf_mod._lib(sid, None) / "incoming").glob("*.vtt")) if (cf_mod._lib(sid, None) / "incoming").is_dir() else []
        fa_mod.analyze_source(sid, captions=caps, progress=lambda *_: None)
        r = seg_mod.build(sid, progress=lambda *_: None)
        from . import audioevents
        lib = cf_mod._lib(sid, None)
        ev = audioevents.analyze(lib / "master.mp4", lib / "analysis" / "audio_events.json")        # heard payoffs (yells, impacts, pauses) for the review
        cands = fa_mod.load_candidates(sid)["candidates"]
        (lib / "analysis" / "review_evidence.json").write_text(json.dumps(audioevents.evidence_rows(lib, cands, ev["events"]), indent=1))
        sug = {k: sum(1 for c in cands if c["suggested_format"] == k) for k in ("ranking", "funny", "continuous", "rejected")}
        st.stage("CLASSIFYING", segments_detected=r["segments"], segments_analyzed=r["segments"], ranking_candidates=sug["ranking"], funny_candidates=sug["funny"],
                 continuous_candidates=sug["continuous"], rejected_candidates=sug["rejected"] + r["rejected_sections"])
        if r["second_coverage_pass"]:
            st.warn("few segments detected: a second coverage pass split long segments")
    except Exception as exc:
        st.fail(f"{type(exc).__name__}: {exc}")
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(r))


@app.command("content-plan")
def content_plan_cmd(run_id: str = typer.Argument(...), snap: bool = typer.Option(True, "--snap/--no-snap", help="Snap cut points out of spoken words (re-decodes each edge)")) -> None:
    """Allocate every reviewed segment to ONE format and write plan/*.json (never reuses or overlaps a moment)."""
    d = cf_mod.run_dir_for(run_id)
    st = RunStatus(d)
    try:
        st.stage("PLANNING")
        from . import cutqa
        pl = cf_mod.build_plan(run_id, snap_fn=cutqa.snap_boundaries if snap else None)
    except cf_mod.ContentError as exc:
        st.fail(str(exc))
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    c = pl["candidates"]
    st.update(ranking_candidates=c["ranking"], funny_candidates=c["funny"], continuous_candidates=c["continuous"], rejected_candidates=len(pl["rejected"]),
              videos_planned=len(pl["videos"]))
    typer.echo(json.dumps({"videos": [{"id": v["id"], "format": v["format"], "name": v["name"], "moments": len(v["moments"])} for v in pl["videos"]],
                           "rejected": len(pl["rejected"]), "warnings": pl["warnings"]}, indent=1))


@app.command("content-render")
def content_render_cmd(run_id: str = typer.Argument(...), only: str = typer.Option(None, "--only", help="comma-separated video ids (e.g. R01,F02)")) -> None:
    """Caption specs (two-decode agreement), chat scans, then render every planned video into runs/<id>/working/."""
    d = cf_mod.run_dir_for(run_id)
    st = RunStatus(d)
    st.stage("RENDERING")
    try:
        cf_mod.build_specs(run_id, progress=lambda *_: None)
        recs = cf_mod.render_all(run_id, only=only.split(",") if only else None, progress=lambda *_: None)
    except cf_mod.ContentError as exc:
        st.fail(str(exc))
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    for r in recs:
        if r["error"]:
            st.warn(f"{r['stem']} render failed: {r['error']}")
    st.update(videos_rendering=0, videos_in_qa=sum(1 for r in recs if not r["error"]))
    typer.echo(json.dumps({"rendered": sum(1 for r in recs if not r["error"]), "failed": [r["stem"] for r in recs if r["error"]]}))


@app.command("content-finalize")
def content_finalize_cmd(run_id: str = typer.Argument(...)) -> None:
    """Full QA on every render, file passed videos in ready-for-drafts/<format>/, failures in needs-review/, register moments, build the dashboard."""
    d = cf_mod.run_dir_for(run_id)
    st = RunStatus(d)
    st.stage("QA")
    recs = cf_mod.finalize_all(run_id)
    ready = sum(1 for r in recs if r["status"] == "ready_for_draft_transfer")
    st.stage("ORGANIZING", videos_in_qa=0, videos_passed=ready, videos_needing_review=len(recs) - ready, videos_ready_for_drafts=ready)
    cf_mod.build_dashboard(run_id)
    typer.echo(json.dumps({"ready_for_drafts": ready, "needs_review": len(recs) - ready}))


@app.command("content-clean")
def content_clean_cmd(run_id: str = typer.Argument(...)) -> None:
    """Move ONLY allowlisted temporary files to the Trash, rebuild the dashboard, mark the run COMPLETE."""
    d = cf_mod.run_dir_for(run_id)
    st = RunStatus(d)
    st.stage("CLEANING")
    res = cf_mod.clean_run(run_id)
    st.update(cleanup=f"{res['result']}: {res['moved']} files, {res['bytes']:,} bytes moved to Trash")
    for p in res["problems"]:
        st.warn(p)
    st.stage("COMPLETE")
    cf_mod.build_dashboard(run_id)
    typer.echo(json.dumps(res))


@app.command("content-report")
def content_report_cmd(run_id: str = typer.Argument(...), command: str = typer.Option("", "--command")) -> None:
    """The ONE final report for a /make-content run."""
    typer.echo(cf_mod.report(run_id, command=command))


@app.command("reject-run")
def reject_run_cmd(run_id: str = typer.Argument(...), reason: str = typer.Option(..., "--reason"),
                   archive: str = typer.Option(None, "--archive", help="Move approved/<run-id>/ into this labeled legacy folder (moves only)")) -> None:
    """Mark a finished run rejected_by_user: clips stay in the registry for audit but stop blocking reuse; its outputs move to a labeled legacy folder."""
    from . import registry as registry_mod
    n = registry_mod.reject_run(run_id, reason)
    moved = None
    src = config_root() / "approved" / run_id
    if archive and src.is_dir():
        dst = Path(archive)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise typer.Exit(code=1)
        src.rename(dst)
        moved = str(dst)
    typer.echo(json.dumps({"clips_marked_rejected": n, "outputs_moved_to": moved}))


@app.command("source-status")
def source_status(source_id: str = typer.Argument(None)) -> None:
    """List library sources (state, master present, used clips) or show what a source still needs generated."""
    if source_id:
        try:
            typer.echo(json.dumps({"id": source_id, "missing": lib_mod.missing_items(source_id)}, indent=2))
        except (lib_mod.SourceError, FileNotFoundError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1)
        return
    for s in lib_mod.list_sources():
        typer.echo(f"{s['id']}  {s['status']:20}  master={'yes' if s['master'] else 'no ':3}  used_clips={s['used_clips']}  duration={s['duration_sec']}")


@app.command("set-source-status")
def set_source_status(source_id: str = typer.Argument(...), state: str = typer.Argument(..., help="active | exhausted | archived")) -> None:
    """Change a source's lifecycle state ('purged' is only set by the PURGE SOURCE flow)."""
    try:
        typer.echo(f"{source_id} -> {lib_mod.set_status(source_id, state)['status']}")
    except lib_mod.SourceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("check-plan")
def check_plan(plan: Path = typer.Argument(..., help="JSON list of videos: {category, moments:[{rank,title,start,end,verified}]}"),
               source_sha256: str = typer.Option(None, "--source-sha256", help="Full sha256 of the source; rejects moments in registry/used_clips.json"),
               source_id: str = typer.Option(None, "--source-id", help="Library source id (looks up its sha256)"),
               run_id: str = typer.Option(None, "--run-id", help="This run's id (its own reservations are not conflicts)"),
               scan_chat: bool = typer.Option(False, "--scan-chat", help="Also OCR the whole frame of every moment for severe on-screen language")) -> None:
    """Enforce rank-count, title, duration, category, duplicate and verification rules before rendering."""
    from . import registry as registry_mod
    if source_id:
        source_sha256 = lib_mod.resolve(source_id, require_active=False).meta["sha256"]
    conflict_fn = None
    if source_sha256:
        conflict_fn = lambda m: registry_mod.conflicts(source_sha256, m["start"], m["end"], m.get("text_sha256"), m.get("sketch"),
                                                       ignore_run=run_id)
    if source_id:
        base = conflict_fn

        def conflict_fn(m):                      # registry conflicts + the evidence gate (no transcript-only selections)
            out = base(m) if base else []
            ok, why = fa_mod.evidence_for(source_id, m["start"], m["end"])
            bad = fa_mod.unrelated_cuts_in(source_id, m["start"], m["end"])
            return out + ([] if ok else [why]) + [
                f"unrelated scene change at {c['t']}s inside the moment ({c['continuity']['why']}); a camera-angle change within one event is fine, "
                "an accidentally joined scene is not - trim to one event" for c in bad]
    res = plan_mod.validate(json.loads(plan.read_text()), None, conflict_fn)
    if scan_chat and source_id:
        media = lib_mod.resolve(source_id, require_active=False).media
        for vi, v in enumerate(json.loads(plan.read_text()), 1):
            for m in v["moments"]:
                sc = ts_mod.scan_moment(media, m["start"], m["end"])
                for f in sc["findings"]:
                    res.errors.append(f"video {vi} #{m['rank']}: severe on-screen language at {f['t']}s ({f['category']}); trim to {ts_mod.trim_suggestion(sc)} or drop")
                if sc.get("chat_unverified"):
                    res.warnings.append(f"video {vi} #{m['rank']}: {sc['too_small_in_output']} text item(s) too small to read in the final output (recorded as unverified)")
    for w in res.warnings:
        typer.echo(f"warn: {w}")
    for e in res.errors:
        typer.echo(f"ERROR: {e}", err=True)
    typer.echo("plan OK" if res.ok else "plan REJECTED")
    if not res.ok:
        raise typer.Exit(code=1)


@app.command()
def qa(
    video: Path = typer.Argument(..., help="Finished render"),
    out_dir: Path = typer.Option(..., "--out-dir", help="runs/<id>/qa"),
    labels: Path = typer.Option(None, "--labels", help="labels.json ([{rank,title}]) for the manual-review notes"),
) -> None:
    """Technical QA; visual items that cannot be proven automatically are flagged for manual review."""
    entries = titles_mod.validate_entries(json.loads(labels.read_text())) if labels else None
    rep = qa_mod.run_qa(video, out_dir, entries)
    for c in rep["checks"]:
        typer.echo(f"[{c['status']:13}] {c['check']} {c['detail']}")
    typer.echo(f"verdict: {rep['verdict']}")
    if rep["verdict"] == "fail":
        raise typer.Exit(code=1)


@app.command()
def usage(run: str = typer.Option(None, "--run", help="Run id; omit for every run plus totals")) -> None:
    """Summarize usage by run and across runs."""
    allruns = usage_mod.load_all(runs_mod.runs_root())
    if run:
        allruns = [r for r in allruns if r["run_id"] == run]
        if not allruns:
            typer.echo(f"Error: no such run {run}", err=True)
            raise typer.Exit(code=1)
    for r in allruns:
        typer.echo(usage_mod.format_run(r))
    typer.echo("TOTAL " + json.dumps(usage_mod.summarize(allruns)))


@app.command("record-completion")
def record_completion(
    run_id: str = typer.Argument(...),
    final: str = typer.Option(..., "--final", help="Final MP4, relative to runs/<id>/"),
    qa_result: str = typer.Option(..., "--qa-result", help="passed | manual_review | failed"),
    asset_id: str = typer.Option(..., "--higgsfield-asset-id"),
    upload_status: str = typer.Option(..., "--higgsfield-status", help="succeeded when the upload is confirmed"),
    tiktok: str = typer.Option("not_started", "--tiktok-status"),
    user_reviewed: bool = typer.Option(False, "--user-reviewed", help="User reviewed the manual-review QA items"),
    uploaded_sha256: str = typer.Option(None, "--uploaded-sha256"),
) -> None:
    """Record a completed run (final path, QA, upload, explicit cleanup-eligible and protected lists)."""
    try:
        p = cleanup_mod.record_completion(run_id, final, qa_result, asset_id, upload_status, None, tiktok, user_reviewed, uploaded_sha256)
    except cleanup_mod.CleanupError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"recorded -> {p}")


@app.command("cleanup-preview")
def cleanup_preview(run_id: str = typer.Argument(...)) -> None:
    """Read-only preview of what cleanup would do for a completed run."""
    try:
        typer.echo(cleanup_mod.preview(run_id))
    except cleanup_mod.CleanupError as exc:
        typer.echo(f"BLOCKED: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("cleanup-run")
def cleanup_run(run_id: str = typer.Argument(...),
                confirm: str = typer.Option(..., "--confirm", help="Exactly: DELETE RUN <run-id>")) -> None:
    """Move a completed run's working files to the Trash (recoverable) and file the final MP4 under finals/."""
    try:
        res = cleanup_mod.execute(run_id, confirm)
    except cleanup_mod.CleanupError as exc:
        typer.echo(f"BLOCKED: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(res, indent=2))


@app.command("purge-preview")
def purge_preview(source_id: str = typer.Argument(...)) -> None:
    """Read-only preview of purging a canonical source's media (separate from normal run cleanup)."""
    try:
        typer.echo(cleanup_mod.purge_preview(source_id))
    except cleanup_mod.CleanupError as exc:
        typer.echo(f"BLOCKED: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("purge-source")
def purge_source(source_id: str = typer.Argument(...),
                 confirm: str = typer.Option(..., "--confirm", help="Exactly: PURGE SOURCE <source-id>")) -> None:
    """Move a canonical source's media to the Trash. Transcripts, analysis, fingerprints and the registry are kept."""
    try:
        typer.echo(json.dumps(cleanup_mod.purge_execute(source_id, confirm), indent=2))
    except cleanup_mod.CleanupError as exc:
        typer.echo(f"BLOCKED: {exc}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
