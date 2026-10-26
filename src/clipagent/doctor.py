"""Check that required local software is available. No network, no credits."""
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as exc:
        return f"error: {exc}"


def _tool(name: str, version_cmd: list[str], required: bool = True) -> Check:
    path = shutil.which(name)
    if not path:
        return Check(name, False, "not found", required)
    return Check(name, True, _version(version_cmd), required)


BREW_BIN = "/opt/homebrew/bin"


def path_check() -> Check:
    import os
    ok = BREW_BIN in os.environ.get("PATH", "").split(os.pathsep) or bool(shutil.which("ffmpeg"))
    return Check("PATH", ok, "ffmpeg reachable" if shutil.which("ffmpeg") else f"ffmpeg not on PATH; export PATH={BREW_BIN}:$PATH")


def pillow_check() -> Check:
    """Overlay text is drawn by /usr/bin/python3 (it ships Pillow)."""
    try:
        out = subprocess.run(["/usr/bin/python3", "-c", "import PIL;print(PIL.__version__)"], capture_output=True, text=True, timeout=20)
        return Check("pillow (/usr/bin/python3)", out.returncode == 0, out.stdout.strip() or out.stderr.strip()[-120:])
    except Exception as exc:
        return Check("pillow (/usr/bin/python3)", False, f"error: {exc}")


def run_checks() -> list[Check]:
    py_ok = sys.version_info >= (3, 11)
    checks = [
        Check("python", py_ok, sys.version.split()[0] + ("" if py_ok else " (need >= 3.11)")),
        _tool("ffmpeg", ["ffmpeg", "-version"]),
        _tool("ffprobe", ["ffprobe", "-version"]),
        _tool("git", ["git", "--version"]),
        path_check(),
        pillow_check(),
        _tool("higgsfield", ["higgsfield", "--version"], required=False),
    ]
    try:
        import faster_whisper

        checks.append(Check("faster-whisper", True, faster_whisper.__version__))
    except ImportError:
        checks.append(Check("faster-whisper", False, "not installed"))
    return checks


def all_required_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.required)
