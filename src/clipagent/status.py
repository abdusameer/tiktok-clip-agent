"""Visible workload tracking for /make-content: runs/<run-id>/STATUS.md and status.json, updated through every stage."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("INGESTING", "ANALYZING", "CLASSIFYING", "PLANNING", "RENDERING", "QA", "ORGANIZING", "CLEANING", "COMPLETE", "FAILED")
COUNTERS = ("segments_detected", "segments_analyzed", "ranking_candidates", "funny_candidates", "continuous_candidates", "rejected_candidates",
            "videos_planned", "videos_rendering", "videos_in_qa", "videos_passed", "videos_needing_review", "videos_ready_for_drafts")
LABELS = {"segments_detected": "Segments detected", "segments_analyzed": "Segments analyzed", "ranking_candidates": "Ranking candidates",
          "funny_candidates": "Funny-moment candidates", "continuous_candidates": "Continuous-moment candidates",
          "rejected_candidates": "Rejected candidates", "videos_planned": "Videos planned", "videos_rendering": "Videos rendering",
          "videos_in_qa": "Videos in QA", "videos_passed": "Videos passed", "videos_needing_review": "Videos needing review",
          "videos_ready_for_drafts": "Videos ready for drafts"}


class RunStatus:
    def __init__(self, run_dir: Path, source_id: str = "", source_name: str = "", source_duration: float = 0.0):
        self.dir = Path(run_dir)
        self.data = {"run_id": self.dir.name, "stage": "INGESTING", "source_id": source_id, "source_name": source_name,
                     "source_duration_sec": round(source_duration, 2), **{k: 0 for k in COUNTERS}, "cleanup": "not started",
                     "errors": [], "warnings": [], "history": [], "updated_at": None}
        p = self.dir / "status.json"
        if p.is_file():
            self.data.update(json.loads(p.read_text()))

    def stage(self, name: str, **counters) -> None:
        if name not in STAGES:
            raise ValueError(f"unknown stage {name!r}")
        self.data["stage"] = name
        self.data["history"].append({"stage": name, "at": datetime.now(timezone.utc).isoformat()})
        self.update(**counters)

    def update(self, **kw) -> None:
        for k, v in kw.items():
            if k not in self.data:
                raise KeyError(f"unknown status field {k!r}")
            self.data[k] = v
        self.write()

    def add(self, key: str, n: int = 1) -> None:
        self.data[key] += n
        self.write()

    def warn(self, msg: str) -> None:
        self.data["warnings"].append(msg)
        self.write()

    def error(self, msg: str) -> None:
        self.data["errors"].append(msg)
        self.write()

    def fail(self, msg: str) -> None:
        self.error(msg)
        self.stage("FAILED")

    def write(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        d = self.data
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps(d, indent=2))
        os.replace(tmp, self.dir / "status.json")
        rows = "\n".join(f"| {LABELS[k]} | {d[k]} |" for k in COUNTERS)
        done = [h["stage"] for h in d["history"]]
        stages = " -> ".join(f"**{s}**" if s == d["stage"] else (f"~~{s}~~" if s in done else s) for s in STAGES[:-1])
        md = (f"# Run {d['run_id']}\n\n**Current stage: {d['stage']}**\n\n{stages}\n\n"
              f"- Source ID: `{d['source_id']}`\n- Source name: {d['source_name']}\n- Source duration: {d['source_duration_sec']} s\n"
              f"- Cleanup: {d['cleanup']}\n- Updated: {d['updated_at']}\n\n| Workload | Count |\n|---|---|\n{rows}\n\n"
              f"## Errors ({len(d['errors'])})\n" + ("\n".join(f"- {e}" for e in d["errors"]) or "none") +
              f"\n\n## Warnings ({len(d['warnings'])})\n" + ("\n".join(f"- {w}" for w in d["warnings"]) or "none") + "\n")
        tmp = self.dir / "STATUS.md.tmp"
        tmp.write_text(md)
        os.replace(tmp, self.dir / "STATUS.md")
