Inspect the connected Higgsfield CLI and its installed skills.
Do not generate anything, publish anything, or spend credits.

Report:
1. The installed Higgsfield CLI version.
2. Which Higgsfield skills are available.
3. Whether you can access my existing Higgsfield assets.
4. Whether you can perform video-to-video editing.
5. Whether you can generate captions or subtitles.
6. Whether you can create voiceovers and B-roll.
7. Whether you can send completed videos to TikTok drafts.
8. Whether you can retrieve TikTok performance analytics.
9. My available Higgsfield credit balance, if checking it is free.
10. Any missing tools required for processing long streamer recordings.

Do not install or modify anything yet. Only inspect and report.
## Ranking video production
- `/make-rankings` is the standard production workflow; detailed steps live in `.claude/skills/make-rankings/SKILL.md`, not here.
- `config/ranking_style.json` is the source of truth for the approved visual style.
- Every finished video has 4-5 ranks, never more than five. Rank and title are stored separately and the rank always matches badge, position and countdown.
- Profanity stays uncensored unless I request otherwise.
- Public publishing always requires my explicit approval.
- `/deleteandcleanup <run-id>` previews, then (only on the exact phrase `DELETE RUN <run-id>`) moves a completed run's working files to the Trash and keeps the final MP4 under `finals/`; the procedure lives in its skill.