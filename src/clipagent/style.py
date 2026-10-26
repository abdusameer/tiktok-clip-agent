"""Load the approved ranking template (config/ranking_style.json) - the single source of truth."""
import json
from functools import lru_cache
from pathlib import Path

STYLE_PATH = Path(__file__).resolve().parents[2] / "config" / "ranking_style.json"


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(STYLE_PATH.read_text())
