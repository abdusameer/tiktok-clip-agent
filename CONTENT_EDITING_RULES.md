# Content editing rules (canonical)

This is the single source of truth for the editorial policy of every clip-making workflow (`/make-content`, `/make-rankings`). Skills, `CLAUDE.md`, prompts and code **reference this file; they do not restate or override it**. Visual styling lives in `config/ranking_style.json`; benchmark research (heuristics only) lives in `.claude/skills/make-content/research.md`.

## 1. Primary objective
Make the strongest, most watchable, most shareable videos possible from every source. Virality cannot be guaranteed; optimize for viral potential, retention, shares, rewatches and follows with the footage that actually exists.

**Runtime is irrelevant to editorial quality. The moment decides the duration.** A finished video may be 7 s, 25 s, 75 s, 2 min or longer. Length is metadata (recorded in manifests and reports) - never a target, warning, score, or reason to reject, shorten or pad.

Never:
- pad a moment to reach a duration, or stretch/add weak entries to reach a count;
- cut necessary setup, escalation, payoff or reaction to stay under a duration;
- reject a strong moment for exceeding 17, 30, 59, 60 or 90 s (or any other figure);
- prefer a weaker short clip over a stronger long one because of completion-rate benchmarks;
- treat "under one minute" as a quality requirement.

The **only** permitted duration check is the destination's genuine technical upload limit (`limits.video_platform_max_seconds`). If a video would exceed it, split only at a natural story/scene boundary (`plan.split_at_boundaries`); if no natural boundary exists, report it as an unsolved technical limit instead of cutting blindly.

## 2. What is a strong, publishable moment
Publish only when the finished edit has a real reason to watch. Judge on:
- immediate curiosity or emotional interest;
- a clear setup, escalation, reveal, reaction or payoff;
- understandable with only the necessary context;
- strong visual or spoken content (surprise, humor, tension, insight, absurdity, relatability, rewatch value);
- a natural, satisfying ending;
- an honest hook that does not mislead;
- audio and visuals clear enough to publish.

**Funny is the bar.** Sexually explicit, innuendo, crude or weird content is never a reason to reject or skip a moment (user decision); if it is funny, use it. Genuine hateful slurs/attacks still fail the severe-language scan, and public publishing still needs the user's explicit approval.

A technically clean clip without a worthwhile payoff is not strong. Start at the latest point that still gives enough context; remove dead air, repetition, loading, confusion and irrelevant setup; end when the payoff and any valuable reaction are complete - not at an arbitrary timestamp.

In the engine this is enforced by evidence, not by length: a candidate must be **visually reviewed** (viewed frames, visible action, hook, setup, payoff, works-without-context, audio/visual confidence). Anything the frames cannot confirm is uncertain and skipped; `check-plan`/`content-plan` refuse unreviewed or rejected candidates. Heuristic scores only prioritize; they never delete an understandable moment.

## 3. Analyze the whole source
Analyze the entire source from beginning to end and build a **candidate ledger** (`runs/<run>/candidates/ledger.json`) of every potentially publishable moment: funny scenes, unexpected reactions, arguments/tension, escalations, embarrassing moments, surprising statements, stories with a payoff, continuous scenes, strong conversations, visual comedy, impressive or relatable moments, moments that improve on replay, and groups that share one narrow ranking theme. A scene change, topic change, completed joke/reaction or new interaction can signal a new candidate; do not create meaningless fragments just because a shot changed (a camera-angle change inside one continuing event is not a boundary).

There is no minimum or maximum number of outputs, and no filler. Do not stop after one ranking and one standalone.

## 4. Two mandatory creative passes
Every source gets both passes, with equal care:
1. **Ranking pass** - find themes with enough honest entries.
2. **Standalone pass** - find moments that work better alone. Standalones are first-class, not leftovers.

Produce both formats whenever the source supports them. Never manufacture a weak ranking, fake theme or filler standalone for a quota. If a format is genuinely impossible, the run report states the evidence and reason (`candidates/ledger.json -> passes.*.none_because`).

### Ranking rules
- Valid only when every entry fits one clear, **narrow** theme (e.g. Funniest Crashes, Wildest Reactions, Most Unexpected Answers, Worst Decisions, Best Comebacks).
- Entry count is whatever honestly belongs to the theme: a Top 3, 4, 5 or any honest count. **Minimum 3** (`limits.min_ranks`; fewer is a standalone). No maximum except the countdown header's technical layout limit (`limits.layout_max_ranks`, chips shrink to fit; a larger group is split into balanced parts of at least 3). Never add a weak entry to reach five.
- Theme immediately understandable; strongest available opening beat; anticipation toward the final reveal; the strongest/most satisfying entry at #1 only when that ordering is honest; every entry gets enough setup and payoff regardless of length; end right after the final payoff or valuable reaction.
- A brief preview of #1 may be used as a cold open when it strengthens curiosity without spoiling the payoff.
- Rank and title are stored separately; the rank always equals badge, position and countdown (first clip plays the highest rank, last plays #1).

### Standalone rules
One complete, worthwhile viewing experience: a short joke, a long funny scene, a continuous conversation, a story, an argument, a reaction with its full context, a setup with escalating beats, or any scene that works better alone. It may run longer than a minute whenever the scene earns it. Never cut a good scene into meaningless pieces to get more files.

## 5. Full creative freedom, truthfully
You may use cold opens, payoff teasers, reordering for clarity, replays, freeze frames, punch-ins/zooms, reframing/tracking, jump cuts, speed changes, strategic pauses, headlines, burned-in captions, sound effects, music when legally available, visual emphasis, highlighted words, selective color. Every choice must improve clarity, emotion, pacing, curiosity or payoff; do not overload a video with effects.

Reordering and teasing must stay truthful: never fabricate a reaction, quote, causal link or event. A payoff may be previewed then rewound, but the finished edit must not misrepresent the source. Captions stay readable and accurate (confirmed wording only); accent colors come from the controlled high-contrast palette (`label.title_accent`), varied across videos, never random. Profanity stays uncensored unless the user says otherwise. Each output needs real added value (framing, structure, event selection or substantive editing), not just a re-upload.

## 6. Reuse and duplicates
- Do not export identical or near-identical videos to inflate the count. Two videos that use >= 85 % of the same source footage are a near-identical duplicate (`plan.NEAR_IDENTICAL`) and are refused; renamed duplicates and simple recrops do not count as different edits; byte-identical finals are caught by the export index (`registry/exports.json`).
- A source moment may appear in both a ranking and a standalone **only** when the two videos give materially different experiences (e.g. tight ranking beat vs fuller-scene standalone with its own headline). The review records `standalone_reuse {title, reason, use_start, use_end}`; the plan stores `reuse {of, reason}` and the candidate ledger/manifest record every intentional reuse. Reuse within the same kind of video, or across earlier finished videos (the permanent registry), is still blocked.

## 7. Autonomous workflow
After valid source media appears in `sourcevideo/`, work without asking the user to choose moments, format, runtime, number of videos, edit style, hook, captions or whether a passing render is approved:
1. validate and match video + audio (`content-intake`); 2. analyze the entire source; 3. detect scenes and candidates; 4. build the candidate ledger; 5. ranking pass; 6. standalone pass; 7. create the edits; 8. render every publishable video; 9. technical + editorial QA; 10. move passing videos to `approved_clips/rankings/`, `approved_clips/standalone/funny/` or `approved_clips/standalone/continuous/`; 11. move failures to `needs-review/` with `FAILURE.txt`; 12. final run report (also saved to `logs/`).

Never upload, publish, transfer to TikTok drafts, spend external credits, delete source files or purge important data. Public publishing and draft transfer always need the user's explicit approval.

## 8. Organization
`sourcevideo/` (inbox) · `source_library/<id>/` (canonical master, transcript, analysis) · `runs/<run>/{analysis→source_library, candidates/, projects/rankings/, projects/standalone/, plan/, working/ (renders until finalized), qa/, logs/}` · `approved_clips/rankings/<run>/<ID-slug>/` · `approved_clips/standalone/funny/<run>/<ID-slug>/` (short jokes) · `approved_clips/standalone/continuous/<run>/<ID-slug>/` (full scenes) · `approved_clips/_reports/<source>/<run>/index.html` · `needs-review/<run>/<ID-slug>/` · `logs/`.

Every approved output has a manifest with: output filename, source id, source timecodes, format, theme/headline, hook used, payoff, editing decisions, final runtime, candidate ids, intentional reuse, QA result.

## 9. Implementation guarantees (enforced and tested)
No duration-based warnings or rejections (only the platform upload limit); rank count >= 3 with no padding quota; near-duplicate and duplicate-export prevention; safe handling of any 16:9 source resolution (ratio-mapped; odd aspect ratios render uncropped and go to review); captions inside the safe area; audio/video sync and cut-boundary QA; two-decode confirmed caption wording.

## 10. Recovery pass (audio, fragments, risky scenes, ledger)
A first-pass rejection is provisional until these are done; rejection by contact sheet alone is never valid.

**Audio is evidence.** A still frame cannot prove an audio joke, so the absence of visual proof is a gap in the evidence, not a reason to reject. For every audio-dependent candidate: extract the original audio for the whole scene; use both Whisper decodes with word timestamps; compare transcript with visible reactions/actions; inspect neighbouring segments for missing setup or payoff; run audio-event analysis (`clipagent.audioevents`: loud peaks/yelling, impacts, silence gaps, music/spectral changes, laughter-like bursts); reconstruct the full scene before judging; accept every moment whose audio, transcript, timing and visuals together support a payoff. If a required audio event cannot be analysed, improve the pipeline - do not discard the candidate.

**Events, not segment numbers.** Before calling anything a fragment, inspect at least the preceding and following segments and keep expanding while the same conversation, joke, reaction or event is still developing. Merge as many neighbours as needed to recover setup, escalation, payoff and valuable reaction; ignore segmentation boundaries that cut through one event. Boundaries come from the event, never from a segment id or a runtime.

**Safe transformations before rejecting risky material.** Sexual/innuendo content is fine (see section 2). For anything else that is genuinely unsuitable (e.g. hateful slurs, a derogatory on-screen title, a context-free terror reference) first test whether the strong part survives truthfully by: trimming the unsafe part, starting later/ending earlier, cropping/reframing, blurring or covering problematic on-screen text, replacing an unsafe headline with an accurate safe one, removing unneeded captions, briefly muting a single unsafe word when the payoff survives, or using the reaction without the prohibited material. Never change meaning or invent context. A mention (e.g. of terrorism) is not a decision: evaluate the actual context, meaning and visuals. If the risky content IS the whole point and cannot be made suitable, reject with the exact reason.

**Segment ledger (reconciled).** `candidates/recovery-ledger.json` (+ `.csv`) lists EVERY segment S001..Snnn exactly once with: status (used directly / merged into another scene / in a ranking / in a standalone / rejected after recovery), the exact reason, audio verification method, neighbouring segments inspected, and the final output filename. `contentflow.reconcile_ledger` refuses a ledger that omits, duplicates or leaves any segment unexplained, and counts merged segments from the ledger itself (the report quotes those counts, not hand-written ones).

**Production.** No target video count, no filler, no runtime-based rejection or shortening; both rankings and standalones; materially different reuse allowed across formats; render and QA everything approved automatically; passes go to `approved_clips/{rankings,standalone/funny,standalone/continuous}/`, genuine failures to `needs-review/` with the reason; no approval requests. The final report states the number of FINAL VIDEOS (not source moments) with filename, format, runtime, source segments and QA result.
