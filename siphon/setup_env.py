"""Installs or repairs Siphon's engine inside its private environment (.venv).

Run by the launcher before every start. It is instant when everything is in
place; it only downloads on first launch or when something is missing.
"""
from __future__ import annotations

import importlib.metadata as md
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
ENGINE_FILE = DATA / "engine.json"

PACKAGES = ["yt-dlp[default]", "deno", "imageio-ffmpeg"]
DISTS = ["yt-dlp", "yt-dlp-ejs", "deno", "imageio-ffmpeg"]


def missing() -> list[str]:
    out = []
    for dist in DISTS:
        try:
            md.version(dist)
        except md.PackageNotFoundError:
            out.append(dist)
    if not out:
        # binaries must be there too, not just the metadata
        spec = importlib.util.find_spec("imageio_ffmpeg")
        bins = Path(spec.origin).parent / "binaries" if spec and spec.origin else None
        # a Path is always truthy, so the folder has to be probed before iterating it
        if not bins or not bins.is_dir() or not any(p.name.startswith("ffmpeg") for p in bins.iterdir()):
            out.append("imageio-ffmpeg")
        deno = Path(sys.executable).parent / ("deno.exe" if os.name == "nt" else "deno")
        if not deno.is_file():
            out.append("deno")
    return out


def main() -> int:
    if sys.version_info < (3, 10):
        print("  Siphon needs Python 3.10 or newer.")
        return 1
    todo = missing()
    if not todo:
        return 0
    print()
    print("  Siphon - installing the download engine (yt-dlp, Deno, FFmpeg)")
    print("  About 80 MB, only needed once.")
    print()
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", "--no-input", *PACKAGES]
    if subprocess.call(cmd) != 0:
        print()
        print("  Install failed. Check your internet connection, then launch Siphon again.")
        return 1
    still = missing()
    if still:
        print(f"  Install incomplete, missing: {', '.join(still)}")
        return 1
    DATA.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        meta = json.loads(ENGINE_FILE.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    meta.update(checked=now, deno_checked=now)
    ENGINE_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print()
    print("  Ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
