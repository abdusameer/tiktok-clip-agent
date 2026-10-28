# Viral short-form research (reference for /make-content)

Evidence quality is mixed: much of it is vendor/SEO analysis and correlational. Treat as heuristics for experiments, never as editing rules: no figure here (hook window, completion targets, ideal length) may become a hard requirement, warning, score or rejection reason. The editorial policy is `CONTENT_EDITING_RULES.md` (runtime is irrelevant to quality; the moment decides the duration). Re-verify before citing numbers.

## What the evidence says
| Finding | Source quality | Use |
|---|---|---|
| TikTok allows uploads up to 60 min; it rewards total watch time and finish rate | TikTok support (first-party) | Length is not capped at 60 s; never pad or trim to hit a target. NOTE: `limits.video_platform_max_seconds` in `config/ranking_style.json` (600 s) is the project's technical upload cap and is unverified against this 60-minute claim - re-check TikTok's current limit before relying on either |
| A complete continuous incident can far outperform disconnected clips (iShowSpeed "Finally Fought Back" 0:57, 300M+) | Public view counts | Preserve chronology and the full payoff |
| Single well-presented events work without a ranking wrapper (Rudy "Aura Farm" 0:38, 34M) | Public view counts | Standalone moments are a first-class format |
| Rankings work only when every entry shares one *specific* theme; generic "funniest moments" does not | Examples + judgment | Rank only with a truthful, narrow promise |
| Opus 2026 sample (1,449 TikToks): viral-tier median 41 s vs 50 s overall; 15-30 s over-represented, 60-90 s under-represented | Vendor, correlational | Mild pull toward tight edits; not a ceiling |
| 80% of viral-tier clips use burned-in captions; payoff or hook in the first ~3 s recommended | Vendor, no baseline | Captions help; open on the strongest beat |
| Retention signals: completion, rewatch, shares, saves. Weights unpublished and sources conflict | Creator blogs | Optimize for finish and rewatch, not one metric |
| Rewatch is driven by tight payoff, loopable endings, density | Creator blogs | End right after the strongest beat |
| Twitch clip virality is "about the people", not the game | Kotaku | Prioritize human reaction and unplanned moments |
| More cuts do not automatically raise engagement (2026 experiment) | Conference paper | No cut-per-N-seconds quotas |
| Action changes mark event boundaries more than camera cuts | Film cognition research | Merge shots into semantic events |
| Caption layout (1 vs 2 lines) changes where viewers look | Eye-tracking study | Keep captions out of faces and action |
| YouTube (Oct 2026): channels mainly re-uploading others' clips without significant changes will see less Shorts reach; voice-over narration, minor edits and templates do not count as new | Creator Liaison statement via press | Every output needs real added value |

## Why clips go viral (working model)
1. Immediate, legible stakes: viewer understands what is happening within ~3 s.
2. A complete arc: setup, escalation, payoff, reaction.
3. Human reaction or unplanned moment as the payoff.
4. Specificity: a precise theme or headline beats a generic label.
5. Ending on the beat, not after it (rewatch and finish rate).
6. Added value so the post is not just a re-upload.

## Experiments worth running (not rules)
Record each video's runtime in its manifest and compare finish rate, rewatches and shares across *your own* posts. Candidate experiments: a 1-2 s cold open of the #1 moment on rankings; ranking size (3 vs 5 vs more); standalone full-scene vs tight cut. Change one thing at a time. Outside benchmarks (completion targets by length, 1.5 s vs 3 s hook windows) conflict across sources and come from unpublished vendor data - they only suggest what to test.
