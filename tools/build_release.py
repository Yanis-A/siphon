"""Build the downloadable Siphon archives, one per system, each with its own Python.

    python tools/build_release.py [--out dist] [--only windows-x64,...]

Someone who downloads Siphon should not have to install Python first, nor find
the "Add to PATH" box on the way. Each archive therefore carries a standalone
CPython from python-build-standalone (the builds uv installs), in runtime/, and
the launchers pick it up before looking for a system Python. The download
engine itself (yt-dlp, Deno, FFmpeg) is still installed on first launch, into a
private environment, so it keeps updating itself as it does today.

Windows gets a zip. macOS and Linux get a .tar.gz: the runtime holds symlinks
and executable bits, which a zip does not carry reliably, and a double-click
opens a .tar.gz on a Mac just as it opens a zip.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Pinned on purpose: a release must be rebuildable byte for byte, and a new
# Python is a change to test, not something to pick up by accident.
PBS_RELEASE = "20261021"
PYTHON = "3.13.17"
PBS_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_RELEASE}"

TARGETS = {
    "windows-x64": ("x86_64-pc-windows-msvc", "zip"),
    "macos-arm64": ("aarch64-apple-darwin", "tar.gz"),
    "macos-x64": ("x86_64-apple-darwin", "tar.gz"),
    "linux-x64": ("x86_64-unknown-linux-gnu", "tar.gz"),
}

# Repository files that are for working on Siphon, not for running it.
DEV_ONLY = re.compile(r"^(\.git|\.github/|tools/)")
LAUNCHERS = {"Siphon.bat", "Siphon.command"}


def app_version() -> str:
    m = re.search(r'^VERSION = "([^"]+)"', (ROOT / "siphon" / "app.py").read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit("VERSION not found in siphon/app.py")
    return m.group(1)


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        print(f"  downloading {url.rsplit('/', 1)[-1]}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(dest)
    return dest


def checked_runtime(triple: str, cache: Path) -> Path:
    """The standalone Python for a target, verified against the release's checksums."""
    name = f"cpython-{PYTHON}+{PBS_RELEASE}-{triple}-install_only_stripped.tar.gz"
    path = fetch(f"{PBS_URL}/{name}", cache / name)
    sums = fetch(f"{PBS_URL}/SHA256SUMS", cache / f"SHA256SUMS-{PBS_RELEASE}").read_text()
    want = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(name)), None)
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if want != got:
        path.unlink()
        sys.exit(f"checksum mismatch for {name}: expected {want}, got {got}")
    return path


def app_files() -> list[str]:
    """What git tracks, minus the developer side: never a stray .venv, data folder or log."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p and not DEV_ONLY.match(p))


def build(target: str, version: str, out: Path, cache: Path) -> Path:
    triple, kind = TARGETS[target]
    runtime = checked_runtime(triple, cache)
    windows = target.startswith("windows")
    files = [f for f in app_files() if f not in LAUNCHERS or f == ("Siphon.bat" if windows else "Siphon.command")]
    archive = out / f"Siphon-{version}-{target}.{kind}"
    # a fixed date: two builds of the same sources give the same archive
    stamp = 1_700_000_000
    with tarfile.open(runtime, "r:gz") as src:
        members = [m for m in src.getmembers() if m.name == "python" or m.name.startswith("python/")]
        if kind == "zip":
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                for f in files:
                    z.write(ROOT / f, f"Siphon/{f}")
                for m in members:
                    if m.isfile():
                        z.writestr(zipfile.ZipInfo("Siphon/runtime" + m.name[len("python"):],
                                                   time.gmtime(stamp)[:6]), src.extractfile(m).read(),
                                   zipfile.ZIP_DEFLATED, 9)
        else:
            with tarfile.open(archive, "w:gz", compresslevel=9) as t:
                for f in files:
                    data = (ROOT / f).read_bytes()
                    info = tarfile.TarInfo(f"Siphon/{f}")
                    info.size, info.mtime = len(data), stamp
                    info.mode = 0o755 if f in LAUNCHERS else 0o644
                    t.addfile(info, io.BytesIO(data))
                for m in members:
                    m.name = "Siphon/runtime" + m.name[len("python"):]
                    m.uid = m.gid = 0
                    m.uname = m.gname = ""
                    t.addfile(m, src.extractfile(m) if m.isfile() else None)
    size = archive.stat().st_size / 1e6
    print(f"  {archive.name}  {size:.1f} MB")
    return archive


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--only", default="", help="comma-separated targets: " + ", ".join(TARGETS))
    args = ap.parse_args()
    targets = [t for t in args.only.split(",") if t] or list(TARGETS)
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        sys.exit(f"unknown target: {', '.join(unknown)}")
    out = Path(args.out)
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    version = app_version()
    print(f"Siphon {version}, Python {PYTHON} ({PBS_RELEASE})")
    built = [build(t, version, out, cache) for t in targets]
    sums = out / "SHA256SUMS.txt"
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(out.glob("Siphon-*"))]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {sums.name}")
    return 0 if built else 1


if __name__ == "__main__":
    sys.exit(main())
