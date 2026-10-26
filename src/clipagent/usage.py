"""Per-run usage log (runs/<id>/usage.json) and summaries. Nothing is estimated: unknown values stay null."""
import json
import time
from contextlib import contextmanager
from pathlib import Path


class UsageLog:
    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "usage.json"
        self.t0 = time.time()
        self.d = {"run_id": Path(run_dir).name, "source": {"duration_sec": None, "size_bytes": None},
                  "stages_sec": {}, "transcription": {"model": None, "seconds": None},
                  "candidate_count": None, "completed_videos": 0, "outputs": [],
                  "ffmpeg": {"executions": 0, "retries": 0}, "errors": [], "warnings": [],
                  "higgsfield_credits": {"before": None, "after": None},
                  "tiktok_transfer": "not_started", "claude_usage": None, "wall_clock_sec": None}

    @contextmanager
    def stage(self, name: str):
        t = time.time()
        try:
            yield
        except Exception as exc:
            self.d["errors"].append(f"{name}: {exc}")
            raise
        finally:
            self.d["stages_sec"][name] = round(self.d["stages_sec"].get(name, 0) + time.time() - t, 3)
            self.save()

    def ffmpeg(self, retry: bool = False) -> None:
        self.d["ffmpeg"]["executions"] += 1
        self.d["ffmpeg"]["retries"] += int(retry)

    def warn(self, msg: str) -> None:
        self.d["warnings"].append(msg)

    def output(self, path: Path, duration: float | None) -> None:
        self.d["outputs"].append({"file": Path(path).name, "duration_sec": duration, "size_bytes": Path(path).stat().st_size})
        self.d["completed_videos"] = len(self.d["outputs"])

    def save(self) -> None:
        self.d["wall_clock_sec"] = round(time.time() - self.t0, 3)
        self.path.write_text(json.dumps(self.d, indent=2))


def load_all(runs_root: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(Path(runs_root).glob("*/usage.json"))]


def summarize(runs: list[dict]) -> dict:
    def tot(f):
        return sum(f(r) or 0 for r in runs)
    return {"runs": len(runs),
            "completed_videos": tot(lambda r: r["completed_videos"]),
            "wall_clock_sec": round(tot(lambda r: r["wall_clock_sec"]), 1),
            "ffmpeg_executions": tot(lambda r: r["ffmpeg"]["executions"]),
            "ffmpeg_retries": tot(lambda r: r["ffmpeg"]["retries"]),
            "errors": tot(lambda r: len(r["errors"])), "warnings": tot(lambda r: len(r["warnings"])),
            "credits_spent": round(tot(lambda r: (r["higgsfield_credits"]["before"] - r["higgsfield_credits"]["after"])
                                       if None not in r["higgsfield_credits"].values() else 0), 2),
            "claude_usage": "not available" if all(r["claude_usage"] is None for r in runs) else "see runs"}


def format_run(r: dict) -> str:
    cr = r["higgsfield_credits"]
    lines = [f"{r['run_id']}: {r['completed_videos']} video(s), wall {r['wall_clock_sec']}s, "
             f"ffmpeg {r['ffmpeg']['executions']} (retries {r['ffmpeg']['retries']}), "
             f"candidates {r['candidate_count']}, transcribe {r['transcription']['model']} {r['transcription']['seconds']}s, "
             f"credits {cr['before']} -> {cr['after']}, tiktok {r['tiktok_transfer']}, claude {r['claude_usage']}"]
    lines += [f"    stage {k}: {v}s" for k, v in r["stages_sec"].items()]
    lines += [f"    output {o['file']}: {o['duration_sec']}s, {o['size_bytes']} B" for o in r["outputs"]]
    lines += [f"    ERROR {e}" for e in r["errors"]] + [f"    warn {w}" for w in r["warnings"]]
    return "\n".join(lines)
