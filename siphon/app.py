"""Siphon — a local, private media downloader built on yt-dlp.

Runs a tiny web server on 127.0.0.1 and drives yt-dlp in subprocesses,
so engine updates apply to the next download without a restart.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

APP = "Siphon"
VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parent          # .../Siphon/siphon
BASE = ROOT.parent                              # .../Siphon
WEB = ROOT / "web"
DATA = BASE / "data"
TMP = DATA / "tmp"
CACHE = DATA / "cache"
CONFIG_FILE = DATA / "config.json"
ENGINE_FILE = DATA / "engine.json"
LOG_FILE = DATA / "siphon.log"

DEFAULT_PORT = 8790
MAX_PARALLEL = 3
IDLE_EXIT_SECONDS = 10 * 60          # quit when no page has polled for this long and nothing runs
ENGINE_UPDATE_EVERY = 20 * 3600      # yt-dlp: check roughly daily
DENO_UPDATE_EVERY = 7 * 24 * 3600    # deno: weekly is plenty
INFO_TIMEOUT = 150

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

AUDIO_FORMATS = {"mp3", "wav", "flac", "m4a"}
MP3_BITRATES = {320, 256, 192, 128}
COVER_FORMATS = {"mp3", "flac", "m4a"}
VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]

log = logging.getLogger("siphon")


# ───────────────────────────── utilities ─────────────────────────────

def setup_logging() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    if sys.stderr is not None:  # pythonw has no console
        log.addHandler(logging.StreamHandler())


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def console_python() -> str:
    """python.exe next to pythonw.exe, so children have stdout and no window pops up."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def scripts_dir() -> Path:
    return Path(console_python()).parent


def child_env() -> dict:
    env = dict(os.environ)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env["PATH"] = str(scripts_dir()) + os.pathsep + env.get("PATH", "")
    return env


def run_quiet(args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=child_env(), stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def default_download_dir() -> Path:
    base = None
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            u = uuid.UUID("374DE290-123F-4565-9164-39C4925E467B")  # FOLDERID_Downloads
            guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            out = ctypes.c_wchar_p()
            fn = ctypes.windll.shell32.SHGetKnownFolderPath
            fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            if fn(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0 and out.value:
                base = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            base = None
    if base is None:
        base = Path.home() / "Downloads"
    return base / APP


def human_version(text: str) -> str:
    m = re.search(r"\d+(?:\.\d+)+", text or "")
    return m.group(0) if m else ""


# ───────────────────────────── presets ─────────────────────────────

def normalize_preset(mode, fmt, quality) -> dict | None:
    """Validate a mode/format/quality triple. None when it is unusable."""
    if mode == "audio":
        if fmt not in AUDIO_FORMATS:
            return None
        if fmt == "mp3":
            try:
                quality = int(quality)
            except (TypeError, ValueError):
                quality = 320
            if quality not in MP3_BITRATES:
                quality = 320
        else:
            quality = None
    elif mode == "video":
        if fmt != "mp4":
            return None
        if quality != "best":
            try:
                quality = int(quality)
            except (TypeError, ValueError):
                quality = "best"
            if quality not in VIDEO_HEIGHTS:
                quality = "best"
    else:
        return None
    return {"mode": mode, "format": fmt, "quality": quality}


# ───────────────────────────── tracklists ─────────────────────────────

# a timestamp, not part of a longer run of digits or colons
TS_RE = re.compile(r"(?<![\d:])(\d{1,3}:[0-5]?\d(?::[0-5]?\d)?)(?![\d:])")
LEAD_JUNK = re.compile(r"^[\s\-–—~•·:.,|)\]\['\"<>*=_]+")
TAIL_JUNK = re.compile(r"[\s\-–—~•·:.,|(\[<>*=_]+$")
GAP = re.compile(r"\s{2,}")
TRACK_NO = re.compile(r"^(?:\d{1,3}\s*[.)\-–]|\(\d{1,3}\))\s*")
MAX_MARKS = 300


def parse_clock(text: str) -> int:
    parts = [int(p) for p in text.split(":")]
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_tracklist(text: str, duration: int = 0) -> list[dict]:
    """Pull `12:34 Artist - Title` lines out of a description or a comment.

    Most mixes never carry real chapter markers: the split points live in the
    description, or in whichever comment someone took the time to write. One
    stray timestamp in a sentence is not a tracklist, hence the floor of two
    and the requirement that they climb.
    """
    found: list[tuple[int, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = TS_RE.search(line)
        if not m:
            continue
        # cutting the timestamp out of the middle leaves a gap and loose punctuation
        rest = GAP.sub(" ", f"{line[:m.start()]} {line[m.end():]}".strip())
        rest = TRACK_NO.sub("", LEAD_JUNK.sub("", rest))
        title = TAIL_JUNK.sub("", LEAD_JUNK.sub("", rest)).strip()
        found.append((parse_clock(m.group(1)), title[:150]))
        if len(found) > MAX_MARKS:
            break
    # keep the first title seen for a given second, in reading order
    seen: dict[int, str] = {}
    for start, title in found:
        seen.setdefault(start, title)
    starts = sorted(seen)
    if duration:
        starts = [s for s in starts if s < duration - 1]
    if len(starts) < 2:
        return []
    return close_marks([{"start": s, "title": seen[s]} for s in starts], duration)


def close_marks(marks: list[dict], duration: int = 0) -> list[dict]:
    """Give each mark an end: the next one's start, the last one the duration."""
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1]["start"] if i + 1 < len(marks) else (duration or 0)
        if end and end <= m["start"]:
            continue
        out.append({"start": int(m["start"]), "end": int(end), "title": str(m.get("title") or "").strip()[:150]})
    return out


def marks_from_info(d: dict, duration: int) -> tuple[list[dict], str]:
    """Chapter markers when the site has them, the description otherwise."""
    chapters = [c for c in (d.get("chapters") or []) if isinstance(c, dict)]
    if len(chapters) > 1:
        marks = [{"start": int(c.get("start_time") or 0), "title": str(c.get("title") or "")} for c in chapters]
        marks.sort(key=lambda m: m["start"])
        return close_marks(marks, duration), "chapters"
    found = parse_tracklist(d.get("description") or "", duration)
    return (found, "description") if found else ([], "")


class Destinations:
    """Named folders, each carrying the format it exists for.

    A sampling folder wants WAV, a music folder wants MP3 320: where a file goes
    and what it is are one decision, so they travel together and one click sets
    both. The format belongs to the destination, so changing it while a
    destination is selected redefines that destination.
    """

    MAX = 8

    def __init__(self, cfg: dict):
        raw = cfg.get("destinations")
        items = [self._clean(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
        self.items = [d for d in items if d]
        if not self.items:
            # carries over the single folder kept by earlier versions
            self.items = [{"id": uuid.uuid4().hex[:8], "name": APP,
                           "folder": str(cfg.get("folder") or default_download_dir()),
                           "mode": "audio", "format": "mp3", "quality": 320}]
        self.active = cfg.get("active") if self._find(cfg.get("active")) else self.items[0]["id"]

    @staticmethod
    def _clean(d: dict) -> dict | None:
        folder = str(d.get("folder") or "").strip()
        preset = normalize_preset(d.get("mode"), d.get("format"), d.get("quality"))
        if not folder or not preset:
            return None
        return {"id": str(d.get("id") or uuid.uuid4().hex[:8])[:16],
                "name": str(d.get("name") or "").strip()[:40] or Path(folder).name,
                "folder": folder, **preset}

    def _find(self, dest_id) -> dict | None:
        return next((d for d in self.items if d["id"] == dest_id), None)

    def current(self) -> dict:
        return self._find(self.active) or self.items[0]

    def get(self, dest_id) -> dict:
        return self._find(dest_id) or self.current()

    def select(self, dest_id) -> bool:
        if not self._find(dest_id):
            return False
        self.active = dest_id
        return True

    def save(self, data: dict) -> dict | None:
        d = self._clean(data)
        if not d:
            return None
        existing = self._find(d["id"])
        if existing:
            existing.update(d)
            return existing
        if len(self.items) >= self.MAX:
            return None
        self.items.append(d)
        return d

    def remove(self, dest_id) -> bool:
        if len(self.items) <= 1:
            return False          # there is always somewhere to download to
        d = self._find(dest_id)
        if not d:
            return False
        self.items.remove(d)
        if self.active == dest_id:
            self.active = self.items[0]["id"]
        return True

    def public(self) -> dict:
        return {"items": self.items, "active": self.active}


# ───────────────────────────── engine ─────────────────────────────

class Engine:
    """Locates yt-dlp / FFmpeg / JS runtime and keeps them up to date."""

    def __init__(self):
        self.python = console_python()
        self.lock = threading.Lock()          # one update at a time
        self.cv = threading.Condition()       # guards `users` and `ready`
        self.users = 0                        # downloads / lookups currently using the engine
        self.ready = threading.Event()
        self.ready.set()
        self.status = "idle"          # idle | updating | error
        self.versions: dict = {}
        self.last_error = ""

    def acquire(self, should_stop=lambda: False) -> bool:
        """Register a user of the engine; waits while an update is running."""
        while True:
            with self.cv:
                if self.ready.is_set():
                    self.users += 1
                    return True
            if should_stop():
                return False
            self.ready.wait(timeout=1)

    def release(self) -> None:
        with self.cv:
            self.users = max(0, self.users - 1)
            self.cv.notify_all()

    # paths are resolved per job: an upgrade can rename binaries
    def ffmpeg(self) -> str | None:
        env_exe = os.environ.get("IMAGEIO_FFMPEG_EXE")
        if env_exe and Path(env_exe).is_file():
            return env_exe
        spec = importlib.util.find_spec("imageio_ffmpeg")
        if spec and spec.origin:
            bins = Path(spec.origin).parent / "binaries"
            cands = sorted((p for p in bins.glob("ffmpeg*") if p.is_file() and p.suffix.lower() not in {".md", ".txt"}),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                exe = cands[0]
                if not IS_WIN and not os.access(exe, os.X_OK):
                    try:
                        exe.chmod(exe.stat().st_mode | 0o111)
                    except OSError:
                        pass
                return str(exe)
        return shutil.which("ffmpeg")

    def deno(self) -> str | None:
        name = "deno.exe" if IS_WIN else "deno"
        local = scripts_dir() / name
        if local.is_file():
            return str(local)
        return shutil.which("deno")

    def node(self) -> str | None:
        node = shutil.which("node")
        if not node:
            return None
        try:
            out = run_quiet([node, "--version"], timeout=10).stdout
            if int(human_version(out).split(".")[0]) >= 22:
                return node
        except Exception:
            pass
        return None

    def js_runtime_args(self) -> list[str]:
        deno = self.deno()
        if deno:
            return ["--js-runtimes", f"deno:{deno}"]
        node = self.node()
        if node:
            return ["--js-runtimes", f"node:{node}"]
        return []

    def base_args(self) -> list[str]:
        args = [self.python, "-m", "yt_dlp", "--ignore-config", "--no-color", "--cache-dir", str(CACHE),
                "--socket-timeout", "30"]
        ff = self.ffmpeg()
        if ff:
            args += ["--ffmpeg-location", ff]
        return args + self.js_runtime_args()

    def refresh_versions(self) -> dict:
        code = ("import json,importlib.metadata as m\n"
                "def v(n):\n"
                "    try: return m.version(n)\n"
                "    except Exception: return ''\n"
                "print(json.dumps({'yt_dlp': v('yt-dlp'), 'ejs': v('yt-dlp-ejs'), 'imageio_ffmpeg': v('imageio-ffmpeg')}))")
        versions = {"yt_dlp": "", "ejs": "", "deno": "", "ffmpeg": ""}
        try:
            versions.update(json.loads(run_quiet([self.python, "-c", code], timeout=30).stdout.strip() or "{}"))
        except Exception as exc:
            log.warning("version probe failed: %s", exc)
        deno = self.deno()
        if deno:
            try:
                versions["deno"] = human_version(run_quiet([deno, "--version"], timeout=20).stdout.splitlines()[0])
            except Exception:
                pass
        elif self.node():
            versions["deno"] = ""
            versions["node"] = human_version(run_quiet([self.node(), "--version"], timeout=10).stdout)
        ff = self.ffmpeg()
        if ff:
            versions["ffmpeg"] = human_version(Path(ff).name) or "system"
        versions.pop("imageio_ffmpeg", None)
        self.versions = versions
        return versions

    def snapshot(self) -> dict:
        meta = read_json(ENGINE_FILE, {})
        return {"status": self.status, "versions": self.versions, "checked": meta.get("checked", 0),
                "error": self.last_error}

    def update(self, force: bool = False) -> bool:
        """pip-upgrade the engine. New downloads wait while this runs."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            meta = read_json(ENGINE_FILE, {})
            now = time.time()
            pkgs = []
            if force or now - meta.get("checked", 0) > ENGINE_UPDATE_EVERY:
                pkgs.append("yt-dlp[default]")
            if force or now - meta.get("deno_checked", 0) > DENO_UPDATE_EVERY:
                pkgs.append("deno")
            if not pkgs:
                return True
            # block new users, then let running downloads finish (binaries are locked on Windows)
            with self.cv:
                self.ready.clear()
                self.status = "updating"
                deadline = time.time() + 900
                while self.users and time.time() < deadline:
                    self.cv.wait(timeout=1)
            log.info("engine update: %s", pkgs)
            res = run_quiet([self.python, "-m", "pip", "install", "-U", "--disable-pip-version-check",
                             "--no-input", "-q", "--timeout", "15", "--retries", "2", *pkgs], timeout=900)
            if res.returncode == 0:
                meta["checked"] = now
                if "deno" in pkgs:
                    meta["deno_checked"] = now
                write_json(ENGINE_FILE, meta)
                self.status, self.last_error = "idle", ""
            else:
                tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
                self.last_error = " ".join(tail)[:400]
                self.status = "error"
                log.warning("engine update failed: %s", self.last_error)
            return res.returncode == 0
        except Exception as exc:
            self.status, self.last_error = "error", str(exc)[:400]
            log.exception("engine update crashed")
            return False
        finally:
            self.refresh_versions()
            self.ready.set()
            self.lock.release()


# ───────────────────────────── errors ─────────────────────────────

ERROR_RULES = [
    (r"Unsupported URL|is not a valid URL|No video formats found|Unable to extract", "unsupported"),
    (r"confirm you.?re not a bot|Sign in to confirm you", "bot"),
    (r"confirm your age|age.restricted|inappropriate for some users", "age"),
    (r"members.only|Join this channel|channel membership", "members"),
    (r"Private video|This video is private|privacy", "private"),
    (r"Premieres in|will begin in|is_upcoming|This live event", "upcoming"),
    (r"copyright|blocked it in your country|not available in your country|geo.?restrict", "blocked"),
    (r"Video unavailable|This video is unavailable|has been removed|does not exist|HTTP Error 404|not available", "unavailable"),
    (r"Requested format is not available|No suitable formats|format is not available", "format"),
    (r"HTTP Error 403|HTTP Error 429|Too Many Requests|n challenge|signature|nsig|JS runtime|EJS|PO Token", "blocked_engine"),
    (r"ffmpeg|ffprobe|Postprocessing|audio extraction|Conversion failed|Merger", "convert"),
    (r"getaddrinfo|Name or service not known|timed out|Connection|Network is unreachable|Unable to download webpage|SSL", "network"),
    (r"No module named|Traceback|ModuleNotFoundError", "engine"),
]


def classify_error(text: str) -> str:
    for pattern, code in ERROR_RULES:
        if re.search(pattern, text or "", re.I):
            return code
    return "generic"


def clean_error(lines: list[str]) -> str:
    msg = ""
    for line in lines:
        if line.startswith("ERROR:"):
            msg = line
    if not msg and lines:
        msg = lines[-1]
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]*:\s*", "", msg)
    return msg.strip()[:500]


# ───────────────────────────── info ─────────────────────────────

def pick_thumbnail(d: dict) -> str:
    if d.get("thumbnail"):
        return d["thumbnail"]
    thumbs = [t for t in (d.get("thumbnails") or []) if t.get("url")]
    if thumbs:
        thumbs.sort(key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)))
        return thumbs[-1]["url"]
    return ""


def short_side(f: dict) -> int:
    w, h = f.get("width") or 0, f.get("height") or 0
    return min(w, h) if w and h else h


def normalize_info(d: dict, url: str) -> dict:
    kind = d.get("_type") or "video"
    title = d.get("title") or d.get("fulltitle") or url
    uploader = d.get("uploader") or d.get("channel") or d.get("artist") or d.get("creator") or ""
    site = d.get("extractor_key") or d.get("ie_key") or ""
    if site in {"Generic", "HTML5MediaEmbed"}:
        site = urlparse(url).hostname or ""
    if kind in {"playlist", "multi_video"}:
        entries = [e for e in (d.get("entries") or []) if e]
        thumb = pick_thumbnail(d) or next((pick_thumbnail(e) for e in entries if pick_thumbnail(e)), "")
        return {"kind": "playlist", "title": title, "uploader": uploader, "site": site,
                "count": d.get("playlist_count") or len(entries), "thumbnail": thumb,
                "url": d.get("webpage_url") or url, "hasVideo": True, "hasAudio": True,
                "maxHeight": 0, "heights": VIDEO_HEIGHTS[:]}
    live = d.get("live_status")
    if d.get("is_live") or live == "is_live":
        raise InfoError("live", "")
    if live == "is_upcoming":
        raise InfoError("upcoming", "")
    formats = d.get("formats") or [d]
    has_video = any(f.get("vcodec") != "none" for f in formats)
    has_audio = any(f.get("acodec") != "none" for f in formats)
    sides = sorted({short_side(f) for f in formats if f.get("vcodec") not in (None, "none") and short_side(f)}, reverse=True)
    max_h = sides[0] if sides else 0
    heights = [h for h in VIDEO_HEIGHTS if max_h and h < max_h and h >= (sides[-1] if sides else 0) - 1]
    # where a mix splits into tracks: the site's own markers when it has them,
    # otherwise whatever the description spells out
    duration = int(d.get("duration") or 0)
    marks, marks_from = marks_from_info(d, duration)
    return {"kind": "video", "title": title, "uploader": uploader, "site": site,
            "duration": duration, "thumbnail": pick_thumbnail(d),
            "url": d.get("webpage_url") or url, "hasVideo": has_video, "hasAudio": has_audio,
            "maxHeight": max_h, "heights": heights,
            "marks": marks, "marksFrom": marks_from}


class InfoError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def fetch_info(engine: Engine, url: str) -> dict:
    engine.acquire()
    try:
        args = engine.base_args() + ["-J", "--flat-playlist", "--no-playlist", "--no-warnings", "--", url]
        res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=INFO_TIMEOUT, env=child_env(), stdin=subprocess.DEVNULL,
                             creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise InfoError("timeout", "")
    finally:
        engine.release()
    if res.returncode != 0 or not res.stdout.strip():
        lines = [l for l in res.stderr.splitlines() if l.strip()]
        detail = clean_error(lines)
        log.info("info failed for %s: %s", url, detail)
        raise InfoError(classify_error(detail or res.stderr), detail)
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise InfoError("engine", "Invalid response from yt-dlp")
    return normalize_info(data, url)


COMMENT_TIMEOUT = 420
COMMENT_ARGS = "youtube:comment_sort=top;max_comments=150,all,0,0"


def fetch_comment_tracklist(engine: Engine, url: str, duration: int) -> dict:
    """Hunt for the comment somebody wrote the tracklist into.

    Under a long set there is nearly always one: a listener lists every ID with
    its timecode, and it floats to the top. Candidates are ranked by how many
    timestamps they carry rather than by likes, because the most-liked comment
    is usually applause. Replies are skipped: tracklists are top-level posts,
    and fetching them is what makes this slow.
    """
    engine.acquire()
    try:
        args = engine.base_args() + ["-J", "--no-playlist", "--no-warnings", "--write-comments",
                                     "--extractor-args", COMMENT_ARGS, "--", url]
        res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=COMMENT_TIMEOUT, env=child_env(), stdin=subprocess.DEVNULL,
                             creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise InfoError("timeout", "")
    finally:
        engine.release()
    if res.returncode != 0 or not res.stdout.strip():
        detail = clean_error([l for l in res.stderr.splitlines() if l.strip()])
        raise InfoError(classify_error(detail or res.stderr), detail)
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise InfoError("engine", "Invalid response from yt-dlp")
    comments = data.get("comments") or []
    best = best_comment_tracklist(comments, duration)
    log.info("comment tracklist for %s: %s comments, best has %s marks",
             url, len(comments), len(best["marks"]))
    return best


def best_comment_tracklist(comments, duration: int) -> dict:
    """Of all the comments, the one carrying the most timestamps wins."""
    best: dict = {"marks": [], "author": "", "likes": 0, "scanned": 0}
    for c in comments if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        best["scanned"] += 1
        found = parse_tracklist(c.get("text") or "", duration)
        if len(found) > len(best["marks"]):
            best.update(marks=found, author=str(c.get("author") or "")[:80],
                        likes=int(c.get("like_count") or 0))
    return best


# ───────────────────────────── jobs ─────────────────────────────

PP_STAGE = {
    "Merger": "merging", "FFmpegMerger": "merging",
    "ExtractAudio": "converting", "FFmpegExtractAudio": "converting",
    "VideoRemuxer": "converting", "VideoConvertor": "converting",
    "ThumbnailsConvertor": "cover", "EmbedThumbnail": "cover",
    "Metadata": "tagging", "FFmpegMetadata": "tagging",
    "MoveFiles": "finishing", "MoveFilesAfterDownload": "finishing",
}

# Windows forbids these outright; the rest of the world merely suffers from them
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str) -> str:
    name = BAD_NAME.sub("_", text or "").strip(" .")
    return name[:110] or fallback


def clean_marks(raw) -> list[dict]:
    """Validate cut points coming from the page."""
    out = []
    for m in raw[:MAX_MARKS] if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        try:
            start, end = max(0, int(float(m.get("start") or 0))), int(float(m.get("end") or 0))
        except (TypeError, ValueError):
            continue
        if end and end <= start:
            continue
        out.append({"start": start, "end": end, "title": str(m.get("title") or "")[:150]})
    out.sort(key=lambda m: m["start"])
    return out


class Job:
    def __init__(self, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.url = params["url"]
        self.title = params.get("title") or self.url
        self.thumbnail = params.get("thumbnail") or ""
        self.mode = params["mode"]                 # audio | video
        self.format = params["format"]             # mp3 wav flac m4a | mp4
        self.quality = params.get("quality")       # bitrate | height | "best"
        self.playlist = bool(params.get("playlist"))
        self.marks = params.get("marks") or []      # cut points, when the file is to be split
        self.split = bool(self.marks)
        self.count = int(params.get("count") or 0)
        self.folder = params["folder"]
        self.status = "queued"                     # queued running done error canceled
        self.stage = "queued"
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.item = 0
        self.items = self.count
        self.failed = 0
        self.files: list[str] = []
        self.error_code = ""
        self.error_detail = ""
        self.created = time.time()
        self.proc: subprocess.Popen | None = None
        self.tmp = TMP / self.id
        self.cancel_requested = False
        self._split = False
        self._size = 0
        self._sized_at = 0.0

    def total_size(self) -> int:
        """Bytes on disk, cached: public() runs on every poll, for every job."""
        if self._sized_at and self.status in ("done", "error", "canceled"):
            return self._size          # a finished job never grows again
        now = time.time()
        if now - self._sized_at < 1.0:
            return self._size
        total = 0
        for f in self.files:
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        self._size, self._sized_at = total, now
        return total

    def public(self) -> dict:
        size = self.total_size()
        return {"id": self.id, "url": self.url, "title": self.title, "thumbnail": self.thumbnail,
                "mode": self.mode, "format": self.format, "quality": self.quality,
                "playlist": self.playlist, "split": self.split,
                "status": self.status, "stage": self.stage, "percent": round(self.percent, 1),
                "speed": self.speed, "eta": self.eta, "item": self.item, "items": self.items,
                "failed": self.failed, "files": [Path(f).name for f in self.files], "size": size,
                "single": len(self.files) == 1 and not self.playlist,
                "error": {"code": self.error_code, "detail": self.error_detail} if self.error_code else None,
                "created": self.created}


def build_job_args(engine: Engine, job: Job) -> list[str]:
    a = engine.base_args() + [
        "--newline", "--progress", "--no-simulate",
        "-P", f"home:{job.folder}", "-P", f"temp:{job.tmp}",
        "--trim-filenames", "120", "--concurrent-fragments", "4", "--embed-metadata",
        "--progress-template",
        "download:SIPHON_DL %(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate,speed,eta})j "
        "%(info.{vcodec,acodec,playlist_index,n_entries})j",
        "--progress-template", "postprocess:SIPHON_PP %(progress.{status,postprocessor})j",
        "--print", "after_move:SIPHON_FILE %(filepath)j",
    ]
    if job.mode == "audio":
        stem = "%(title)s"
    else:
        stem = "%(title)s" if job.quality in (None, "best") else f"%(title)s [{job.quality}p]"
    if job.playlist:
        a += ["--yes-playlist", "--ignore-errors",
              "-o", f"%(playlist_title,playlist_id|Playlist)s/%(playlist_index)03d - {stem}.%(ext)s"]
    else:
        a += ["--no-playlist", "--force-overwrites", "-o", f"{stem}.%(ext)s"]

    if job.mode == "audio":
        a += ["-f", "ba/b", "-x", "--audio-format", job.format]
        if job.format == "mp3":
            a += ["--audio-quality", f"{job.quality}K", "-S", "lang"]
        elif job.format == "m4a":
            a += ["--audio-quality", "256K", "-S", "lang,acodec:aac"]
        else:
            a += ["-S", "lang"]
        if job.format in COVER_FORMATS:
            a += ["--embed-thumbnail", "--convert-thumbnails", "png",
                  "--ppa", "ThumbnailsConvertor+FFmpeg_o:-vf \"crop='min(iw,ih)':'min(iw,ih)'\""]
    else:
        res = "res" if job.quality in (None, "best") else f"res:{job.quality}"
        a += ["-f", "bv*+ba/b", "-S", f"lang,{res},fps,vcodec:h264,acodec:aac",
              "--merge-output-format", "mp4", "--remux-video", "mp4"]
    return a + ["--", job.url]


class JobManager:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(MAX_PARALLEL)

    def active_count(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j.status in ("queued", "running"))

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j.status == "running")

    def add(self, params: dict) -> Job:
        job = Job(params)
        with self.lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def list(self) -> list[dict]:
        with self.lock:
            return [self.jobs[i].public() for i in self.order if i in self.jobs]

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def clear_finished(self) -> None:
        with self.lock:
            for jid in list(self.order):
                if self.jobs[jid].status in ("done", "error", "canceled"):
                    self.order.remove(jid)
                    self.jobs.pop(jid, None)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_requested = True
        if job.proc and job.proc.poll() is None:
            kill_tree(job.proc)
        elif job.status == "queued":
            job.status, job.stage = "canceled", "canceled"
        return True

    def cancel_all(self) -> None:
        for jid in list(self.order):
            self.cancel(jid)

    # ── worker ──
    def _run(self, job: Job) -> None:
        self.slots.acquire()
        using_engine = False
        try:
            if job.cancel_requested:
                job.status, job.stage = "canceled", "canceled"
                return
            if not self.engine.ready.is_set():
                job.stage = "engine"
            using_engine = self.engine.acquire(should_stop=lambda: job.cancel_requested)
            if job.cancel_requested or not using_engine:
                job.status, job.stage = "canceled", "canceled"
                return
            job.status, job.stage = "running", "starting"
            Path(job.folder).mkdir(parents=True, exist_ok=True)
            job.tmp.mkdir(parents=True, exist_ok=True)
            args = build_job_args(self.engine, job)
            log.info("job %s: %s", job.id, subprocess.list2cmdline(args) if IS_WIN else " ".join(args))
            popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env())
            if IS_WIN:
                popen_kw["creationflags"] = NO_WINDOW
            else:
                popen_kw["start_new_session"] = True
            job.proc = subprocess.Popen(args, **popen_kw)
            if job.cancel_requested:  # cancel arrived while the process was starting
                kill_tree(job.proc)
            tail: list[str] = []
            errors: list[str] = []
            assert job.proc.stdout is not None
            for raw in job.proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("SIPHON_"):
                    self._handle(job, line)
                    continue
                tail.append(line)
                tail[:] = tail[-40:]
                if line.startswith("ERROR:"):
                    errors.append(line)
                    if job.playlist:
                        job.failed += 1
            rc = job.proc.wait()
            if job.cancel_requested:
                job.status, job.stage = "canceled", "canceled"
            elif rc == 0 or (job.playlist and job.files):
                if job.marks and job.files and not job.playlist:
                    self._split(job)
                if job.cancel_requested:
                    job.status, job.stage = "canceled", "canceled"
                else:
                    job.status, job.stage, job.percent = "done", "done", 100.0
            else:
                detail = clean_error(errors or tail)
                job.status, job.stage = "error", "error"
                job.error_code = classify_error(detail or "\n".join(tail))
                job.error_detail = detail
                log.info("job %s failed (%s): %s", job.id, rc, "\n".join(tail[-8:]))
        except Exception as exc:
            log.exception("job %s crashed", job.id)
            job.status, job.stage = "error", "error"
            job.error_code, job.error_detail = "engine", str(exc)[:300]
        finally:
            job.speed = job.eta = None
            if using_engine:
                self.engine.release()
            remove_tree(job.tmp)
            self.slots.release()

    def _split(self, job: Job) -> None:
        """Cut the finished file at its marks, into a folder of its own.

        A stream copy, so it is lossless and near-instant, and every piece keeps
        the cover and the tags the download already wrote. The full file stays
        where it is: the pieces go in a subfolder beside it.
        """
        ff = self.engine.ffmpeg()
        source = next((f for f in job.files if os.path.isfile(f)), None)
        if not ff or not source:
            log.info("job %s: nothing to split", job.id)
            return
        src = Path(source)
        out_dir = Path(job.folder) / safe_name(src.stem, job.id)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("job %s: cannot create %s: %s", job.id, out_dir, exc)
            return
        job.stage, job.items, job.percent = "splitting", len(job.marks), 0.0
        pieces = []
        for n, mark in enumerate(job.marks, 1):
            if job.cancel_requested:
                break
            job.item = n
            job.percent = 100.0 * (n - 1) / len(job.marks)
            target = out_dir / f"{n:03d} - {safe_name(mark.get('title'), f'{src.stem} {n}')}{src.suffix}"
            # -ss and -t ahead of -i are input options: seek, then read only that much
            args = [ff, "-nostdin", "-loglevel", "error", "-y", "-ss", str(mark["start"])]
            if mark.get("end"):
                args += ["-t", str(mark["end"] - mark["start"])]
            args += ["-i", str(src), "-map", "0", "-dn", "-ignore_unknown", "-c", "copy", str(target)]
            try:
                res = run_quiet(args, timeout=900)
            except subprocess.TimeoutExpired:
                job.failed += 1
                continue
            if res.returncode == 0 and target.is_file() and target.stat().st_size:
                pieces.append(str(target))
            else:
                job.failed += 1
                log.info("job %s: piece %s failed: %s", job.id, n, (res.stderr or "")[-200:])
        job.files.extend(pieces)
        job.percent = 100.0
        log.info("job %s: %s pieces from %s marks", job.id, len(pieces), len(job.marks))

    def _handle(self, job: Job, line: str) -> None:
        tag, _, rest = line.partition(" ")
        try:
            if tag == "SIPHON_FILE":
                path = json.loads(rest)
                if path and path not in job.files:
                    job.files.append(path)
                return
            decoder = json.JSONDecoder()
            first, end = decoder.raw_decode(rest)
            if tag == "SIPHON_DL":
                info, _ = decoder.raw_decode(rest[end:].lstrip())
                self._progress(job, first, info)
            elif tag == "SIPHON_PP":
                if first.get("status") == "started":
                    job.stage = PP_STAGE.get(first.get("postprocessor") or "", "processing")
                    job.speed = job.eta = None
        except (ValueError, json.JSONDecodeError):
            pass

    def _progress(self, job: Job, p: dict, info: dict) -> None:
        vcodec, acodec = info.get("vcodec"), info.get("acodec")
        idx, n = info.get("playlist_index"), info.get("n_entries")
        if job.playlist and isinstance(idx, int):
            job.item = idx
            if isinstance(n, int) and n > 0:
                job.items = n
        total = p.get("total_bytes") or p.get("total_bytes_estimate") or 0
        done = p.get("downloaded_bytes") or 0
        frac = min(done / total, 1.0) if total else 0.0
        if p.get("status") == "finished":
            frac = 1.0
        part = "media"
        if vcodec and vcodec != "none" and acodec == "none":
            part, job._split = "video", True
        elif vcodec == "none" and acodec and acodec != "none":
            part = "audio"
        # map split video+audio downloads onto one smooth bar
        if job.mode == "video" and job._split:
            frac = frac * 0.9 if part == "video" else (0.9 + frac * 0.1 if part == "audio" else frac)
        if job.playlist and job.items:
            pos = max(job.item, 1) - 1
            overall = (pos + frac) / job.items
        else:
            overall = frac
        job.percent = min(100.0, max(job.percent, overall * 100))
        job.stage = f"download_{part}" if job.mode == "video" else "download"
        job.speed = p.get("speed")
        job.eta = p.get("eta")


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def remove_tree(path: Path) -> None:
    for _ in range(10):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.5)


# ───────────────────────────── desktop helpers ─────────────────────────────

def open_folder(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        os.startfile(path)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal_file(path: str) -> None:
    if not os.path.exists(path):
        open_folder(str(Path(path).parent))
        return
    if IS_WIN:
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        open_folder(str(Path(path).parent))


PICKER = r"""
import sys
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
root.lift()
root.focus_force()
root.update()
path = filedialog.askdirectory(initialdir=sys.argv[1], title=sys.argv[2], mustexist=False, parent=root)
root.destroy()
sys.stdout.write(path or "")
"""


def choose_folder(current: str, title: str) -> str | None:
    res = run_quiet([console_python(), "-c", PICKER, current, title], timeout=3600)
    if res.returncode != 0:
        lines = (res.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "folder picker unavailable")
    out = res.stdout.strip()
    return str(Path(out)) if out else None


# ───────────────────────────── HTTP ─────────────────────────────

class State:
    def __init__(self, port: int):
        self.port = port
        self.engine = Engine()
        self.jobs = JobManager(self.engine)
        cfg = read_json(CONFIG_FILE, {})
        self.dests = Destinations(cfg)
        self.cfg_lock = threading.Lock()      # config writes arrive from several threads
        self.last_seen = time.time()
        self.server: ThreadingHTTPServer | None = None

    @property
    def folder(self) -> str:
        return self.dests.current()["folder"]

    def save_config(self) -> None:
        with self.cfg_lock:
            write_json(CONFIG_FILE, {"destinations": self.dests.items, "active": self.dests.active})


STATE: State | None = None
URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.I)


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ── guards ──
    def _host_ok(self) -> bool:
        assert STATE
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{STATE.port}", f"localhost:{STATE.port}"}

    def _origin_ok(self) -> bool:
        assert STATE
        if self.headers.get("X-Siphon") != "1":
            return False
        origin = self.headers.get("Origin")
        return origin in (None, f"http://127.0.0.1:{STATE.port}", f"http://localhost:{STATE.port}")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("bad body")
        return data

    # ── GET ──
    def do_GET(self):
        assert STATE
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        STATE.last_seen = time.time()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = (WEB / "index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; img-src * data: blob:; "
                               "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'"})
        m = re.fullmatch(r"/fonts/([a-z0-9-]+\.woff2)", path)
        if m and (WEB / "fonts" / m.group(1)).is_file():
            return self._send(200, (WEB / "fonts" / m.group(1)).read_bytes(), "font/woff2",
                              {"Cache-Control": "max-age=604800"})
        if path == "/api/ping":
            return self._json(200, {"app": APP, "version": VERSION})
        if path == "/api/state":
            return self._json(200, {"app": APP, "version": VERSION, "folder": STATE.folder,
                                    "destinations": STATE.dests.public(),
                                    "engine": STATE.engine.snapshot(), "jobs": STATE.jobs.list()})
        if path == "/api/jobs":
            return self._json(200, {"jobs": STATE.jobs.list(), "engine": STATE.engine.snapshot()})
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/file", path)
        if m:
            job = STATE.jobs.get(m.group(1))
            if not job or job.status != "done" or len(job.files) != 1 or not os.path.isfile(job.files[0]):
                return self._send(404, b"Not found", "text/plain")
            return self._stream_file(job.files[0])
        return self._send(404, b"Not found", "text/plain")

    do_HEAD = do_GET

    def _stream_file(self, path: str) -> None:
        name = Path(path).name
        ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 256)

    # ── POST ──
    def do_POST(self):
        assert STATE
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, b"Forbidden", "text/plain")
        STATE.last_seen = time.time()
        path = urlparse(self.path).path
        try:
            body = self._body()
        except Exception:
            return self._json(400, {"error": "bad_request"})
        try:
            return self._route_post(path, body)
        except Exception as exc:
            log.exception("POST %s failed", path)
            return self._json(500, {"error": "server", "detail": str(exc)[:300]})

    def _route_post(self, path: str, body: dict):
        assert STATE
        if path == "/api/info":
            url = str(body.get("url") or "").strip()
            if len(url) > 2048 or not URL_RE.match(url):
                return self._json(400, {"error": "invalid_url"})
            try:
                return self._json(200, {"info": fetch_info(STATE.engine, url)})
            except InfoError as e:
                return self._json(422, {"error": e.code, "detail": e.detail})

        if path == "/api/tracklist":
            url = str(body.get("url") or "").strip()
            if len(url) > 2048 or not URL_RE.match(url):
                return self._json(400, {"error": "invalid_url"})
            try:
                duration = max(0, int(body.get("duration") or 0))
            except (TypeError, ValueError):
                duration = 0
            try:
                found = fetch_comment_tracklist(STATE.engine, url, duration)
            except InfoError as e:
                return self._json(422, {"error": e.code, "detail": e.detail})
            return self._json(200, {"marks": found["marks"], "author": found["author"],
                                    "likes": found["likes"], "scanned": found["scanned"]})

        if path == "/api/download":
            url = str(body.get("url") or "").strip()
            if len(url) > 2048 or not URL_RE.match(url):
                return self._json(400, {"error": "invalid_url"})
            dest = STATE.dests.get(body.get("destination"))
            # the destination carries the format; an explicit one still wins, for
            # retries and for callers that know what they want
            preset = (normalize_preset(body.get("mode"), body.get("format"), body.get("quality"))
                      if body.get("mode") else {k: dest[k] for k in ("mode", "format", "quality")})
            if not preset:
                return self._json(400, {"error": "bad_format"})
            thumb = str(body.get("thumbnail") or "")
            try:
                count = max(0, int(body.get("count") or 0))
            except (TypeError, ValueError):
                count = 0
            job = STATE.jobs.add({
                "url": url, **preset,
                "title": str(body.get("title") or "")[:300],
                "thumbnail": thumb if thumb.startswith(("http://", "https://")) else "",
                "playlist": bool(body.get("playlist")), "marks": clean_marks(body.get("marks")),
                "count": count, "folder": dest["folder"],
            })
            return self._json(200, {"job": job.public()})

        if path == "/api/destinations/select":
            if not STATE.dests.select(str(body.get("id") or "")):
                return self._json(404, {"error": "not_found"})
            STATE.save_config()
            return self._json(200, {"destinations": STATE.dests.public()})

        if path == "/api/destinations/save":
            saved = STATE.dests.save(body)
            if not saved:
                return self._json(400, {"error": "bad_destination"})
            STATE.save_config()
            return self._json(200, {"destinations": STATE.dests.public(), "saved": saved})

        if path == "/api/destinations/remove":
            if not STATE.dests.remove(str(body.get("id") or "")):
                return self._json(400, {"error": "bad_destination"})
            STATE.save_config()
            return self._json(200, {"destinations": STATE.dests.public()})

        if path == "/api/destinations/add":
            try:                   # a destination without a folder is meaningless, so ask first
                picked = choose_folder(STATE.folder, str(body.get("title") or "Download folder"))
            except Exception as exc:
                return self._json(500, {"error": "picker", "detail": str(exc)[:200]})
            if picked:
                Path(picked).mkdir(parents=True, exist_ok=True)
                saved = STATE.dests.save({"folder": picked, "name": Path(picked).name,
                                          "mode": "audio", "format": "mp3", "quality": 320})
                if not saved:
                    return self._json(400, {"error": "too_many"})
                STATE.dests.select(saved["id"])
                STATE.save_config()
            return self._json(200, {"destinations": STATE.dests.public()})

        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/(cancel|reveal)", path)
        if m:
            job = STATE.jobs.get(m.group(1))
            if not job:
                return self._json(404, {"error": "not_found"})
            if m.group(2) == "cancel":
                return self._json(200, {"ok": STATE.jobs.cancel(job.id)})
            if job.files:
                target = job.files[0]
                if job.playlist:
                    open_folder(str(Path(target).parent))
                else:
                    reveal_file(target)
            else:
                open_folder(job.folder)
            return self._json(200, {"ok": True})

        if path == "/api/jobs/clear":
            STATE.jobs.clear_finished()
            return self._json(200, {"jobs": STATE.jobs.list()})

        if path == "/api/folder/open":
            open_folder(STATE.folder)
            return self._json(200, {"ok": True})

        if path == "/api/folder/choose":
            try:
                picked = choose_folder(STATE.folder, str(body.get("title") or "Download folder"))
            except Exception as exc:
                return self._json(500, {"error": "picker", "detail": str(exc)[:200]})
            if picked:
                Path(picked).mkdir(parents=True, exist_ok=True)
                STATE.dests.current()["folder"] = picked   # re-points the selected destination
                STATE.save_config()
            return self._json(200, {"folder": STATE.folder, "destinations": STATE.dests.public()})

        if path == "/api/engine/update":
            if STATE.jobs.active_count():
                return self._json(409, {"error": "busy"})
            threading.Thread(target=STATE.engine.update, kwargs={"force": True}, daemon=True).start()
            time.sleep(0.2)
            return self._json(200, {"engine": STATE.engine.snapshot()})

        if path == "/api/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=shutdown, daemon=True).start()
            return None

        return self._json(404, {"error": "not_found"})


# ───────────────────────────── lifecycle ─────────────────────────────

def shutdown() -> None:
    assert STATE
    time.sleep(0.3)
    STATE.jobs.cancel_all()
    deadline = time.time() + 10
    while STATE.jobs.running_count() and time.time() < deadline:
        time.sleep(0.2)
    log.info("shutting down")
    if STATE.server:
        STATE.server.shutdown()


def watchdog() -> None:
    assert STATE
    while True:
        time.sleep(15)
        idle = time.time() - STATE.last_seen
        if idle > IDLE_EXIT_SECONDS and not STATE.jobs.active_count() and not STATE.engine.lock.locked():
            log.info("no open page for %ss, exiting", int(idle))
            shutdown()
            return


def find_running_instance() -> int | None:
    """Port of an already running Siphon, so a second launch just opens the page."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                pass
        except OSError:
            continue
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
                if json.loads(r.read().decode()).get("app") == APP:
                    return port
        except Exception:
            continue
    return None


def bind_server() -> ThreadingHTTPServer:
    last_err = None
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            srv.daemon_threads = True
            return srv
        except OSError as exc:
            last_err = exc
    raise SystemExit(f"No free port near {DEFAULT_PORT}: {last_err}")


def main() -> None:
    global STATE
    setup_logging()
    no_browser = "--no-browser" in sys.argv
    running = find_running_instance()
    if running:
        if not no_browser:
            webbrowser.open(f"http://127.0.0.1:{running}/")
        return
    TMP.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    for leftover in TMP.iterdir():
        remove_tree(leftover) if leftover.is_dir() else leftover.unlink(missing_ok=True)

    server = bind_server()
    port = server.server_address[1]
    STATE = State(port)
    STATE.server = server
    log.info("%s %s on http://127.0.0.1:%s (python %s)", APP, VERSION, port, sys.version.split()[0])

    def warm_engine():
        assert STATE
        STATE.engine.refresh_versions()
        if "--no-update" not in sys.argv:
            STATE.engine.update(force=False)

    threading.Thread(target=warm_engine, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    if not no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        STATE.jobs.cancel_all()
        server.server_close()
        remove_tree(TMP)


if __name__ == "__main__":
    main()
