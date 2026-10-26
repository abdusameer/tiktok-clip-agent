---
name: deleteandcleanup
description: Safely clean a completed ranking-video run - preview first, then (only on the exact phrase "DELETE RUN <run-id>") move the run's working files to the macOS Trash and file the final MP4 under finals/. Manual use only.
disable-model-invocation: true
argument-hint: "<run-id>"
allowed-tools: Bash, Read
---

# /deleteandcleanup

Arguments: `$ARGUMENTS` - exactly one run id like `20261012-004041-6a6f3783`. Anything else: stop and ask.

All decisions are made by the deterministic script (`src/clipagent/cleanup.py`) from `runs/<id>/completion.json`. **You never choose, list, glob or delete paths yourself.** Do not use `rm`, `find -delete`, `mv` into Trash by hand, or any other file command on run contents. Do not install anything, upload anything, or call Higgsfield generation tools.

Shell setup: `export PATH=/opt/homebrew/bin:$PATH`; CLI: `PYTHONPATH=src .venv/bin/python -m clipagent.cli <cmd>`.

## What normal cleanup never touches
Canonical sources, transcripts and analysis are **never** cleaned by `/deleteandcleanup <run-id>`: not `source_library/`, not `source_videos/`, `transcripts/`, `clip_candidates/`, and not a run's `source/`, `transcripts/` or analysis files under `candidates/`. It only handles non-final renders, candidate clip media, QA/preview media, tmp/cache/scratch/proxy/frame files and `rendered_clips/` drafts or the final's alias. The used-clips registry and source metadata are never changed by it.

## Step 1 - preview only (what `/deleteandcleanup <run-id>` does)
Run `cleanup-preview <run-id>` and show its output verbatim. It makes no filesystem changes. It shows the run, the final MP4 (size, duration, resolution), the Higgsfield upload id/status, every file proposed for Trash with sizes, the total recoverable, protected files, the final location (`finals/<run-id>/`) and any uncertainty.

If it prints `BLOCKED: ...`, report the reason and stop. Do not work around it (for example by editing `completion.json`). If the run has no `completion.json`, say the run was not recorded as complete.

Then ask the user to type exactly `DELETE RUN <run-id>` to proceed, and stop.

## Step 2 - execute (only after the exact phrase)
Proceed only if the user's next message is exactly `DELETE RUN <run-id>` for the same run id. "yes", "ok", "go ahead", a different id, extra words or different case are NOT confirmation - repeat the phrase request instead.

Run `cleanup-run <run-id> --confirm "DELETE RUN <run-id>"` using the user's literal text. The script re-checks everything, files the final MP4 at `finals/<run-id>/<name>.mp4` (never overwriting; stops if a different file is already there), moves cleanup targets to `~/.Trash` (recoverable), removes only directories left empty, and writes `finals/<run-id>/manifest.json`.

Report: final path, bytes moved to Trash, the manifest path, and that files can be restored from Trash. If the script stops (Trash unavailable, destination conflict, partial failure), report it as-is and do not retry with other tools.

Running it again is safe: an already-cleaned run reports `already_clean` and changes nothing.

## Purging a canonical source (separate mode)
`/deleteandcleanup --purge-source <source-id>` (12 hex chars) is the ONLY way to remove source media. Run `purge-preview <source-id>` and show it verbatim (read-only), then ask for exactly `PURGE SOURCE <source-id>`. The run-cleanup phrase `DELETE RUN ...` does not authorize a purge, and the purge phrase does not authorize run cleanup. Only after that exact text: `purge-source <source-id> --confirm "PURGE SOURCE <source-id>"`. It moves only `source_library/<id>/master.mp4` and the legacy `source_videos/<id>.mp4` to the Trash; transcripts, analysis, `source.json`, fingerprints and the used-clips registry stay, and the source is marked `purged`/`local_media_cleaned`. It refuses if the registry lacks the source or `master.mp4` has no recorded fingerprint.
