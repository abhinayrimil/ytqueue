"""
ytqueue — a local yt-dlp download queue.

Run:  python app.py            (opens as a desktop window; falls back to your browser)
      python app.py --browser  (always use the browser at http://127.0.0.1:8765)
"""
import copy
import os
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import yt_dlp
from flask import Flask, abort, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("YTQ_DB_PATH", BASE / "queue.db"))
# Headless = no desktop window, no browser launch, no "open folder" (e.g. inside a container).
HEADLESS = os.environ.get("YTQ_HEADLESS", "").lower() in ("1", "true", "yes")
# Extra folders to offer as shortcuts in the folder browser, comma-separated (e.g. container mounts).
EXTRA_SHORTCUTS = [p.strip() for p in os.environ.get("YTQ_SHORTCUTS", "").split(",") if p.strip()]
# Hostnames the server answers to. "*" disables the check (not recommended).
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in os.environ.get("YTQ_ALLOWED_HOSTS", "").split(",") if h.strip()}
WORKERS = int(os.environ.get("YTQ_WORKERS", "1"))
HOST = os.environ.get("YTQ_HOST", "127.0.0.1")
PORT = int(os.environ.get("YTQ_PORT", "8765"))
# Always leave at least this much free on the target disk.
RESERVE = int(os.environ.get("YTQ_RESERVE_MB", "500")) * 1024 * 1024
# Abort a running download if free space drops below this (e.g. something else filled the disk).
MIN_FREE_RUNNING = 100 * 1024 * 1024

_dl = Path.home() / "Downloads"
INITIAL_DIR = os.environ.get("YTQ_DOWNLOAD_DIR") or str((_dl if _dl.is_dir() else BASE / "downloads"))

FORMATS = {
    "best":  {"format": "bv*+ba/b", "merge_output_format": "mp4"},
    "1080p": {"format": "bv*[height<=1080]+ba/b[height<=1080]", "merge_output_format": "mp4"},
    "720p":  {"format": "bv*[height<=720]+ba/b[height<=720]", "merge_output_format": "mp4"},
    "480p":  {"format": "bv*[height<=480]+ba/b[height<=480]", "merge_output_format": "mp4"},
    "audio": {
        "format": "ba/b",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    },
}

ANSI = re.compile(r"\x1b\[[0-9;]*m")
URL_RE = re.compile(r"https?://\S+")
ACTIVE = ("checking", "downloading", "processing")


def human(n):
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


# ---------------------------------------------------------------- filesystem helpers
def norm(path):
    return Path(os.path.expandvars(str(path))).expanduser().resolve()


def existing_parent(p):
    p = norm(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def disk_info(path):
    p = norm(path)
    base = existing_parent(p)
    u = shutil.disk_usage(base)
    return {"path": str(p), "exists": p.is_dir(), "writable": os.access(base, os.W_OK),
            "free": u.free, "total": u.total}


def validate_dir(path):
    """Return a normalised folder path, or raise ValueError with a readable reason."""
    if not path or not str(path).strip():
        raise ValueError("No folder given")
    p = norm(path.strip())
    if p.exists() and not p.is_dir():
        raise ValueError(f"{p} is a file, not a folder")
    base = existing_parent(p)
    if not os.access(base, os.W_OK):
        raise ValueError(f"No write permission for {base}")
    return str(p)


def open_in_file_manager(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------- database
db_lock = threading.Lock()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    format      TEXT NOT NULL DEFAULT 'best',
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued|checking|downloading|processing|done|error|cancelled
    position    REAL,
    title       TEXT,
    progress    REAL,
    speed       REAL,
    eta         INTEGER,
    filepath    TEXT,
    error       TEXT,
    created_at  REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
""")
_cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
for _c, _t in (("save_dir", "TEXT"), ("size_est", "INTEGER")):
    if _c not in _cols:
        db.execute(f"ALTER TABLE jobs ADD COLUMN {_c} {_t}")
# Anything left in progress from a previous run goes back in the queue.
db.execute("UPDATE jobs SET status='queued', progress=NULL, speed=NULL, eta=NULL "
           "WHERE status IN ('checking','downloading','processing')")
db.commit()


def q(sql, args=(), one=False):
    with db_lock:
        cur = db.execute(sql, args)
        db.commit()
        rows = cur.fetchall()
    return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]


def update(job_id, **fields):
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        q(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_setting(key, default=None):
    row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    q("INSERT INTO settings (key, value) VALUES (?, ?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def default_dir():
    return get_setting("default_dir") or str(norm(INITIAL_DIR))


# ---------------------------------------------------------------- disk space
reserve_lock = threading.Lock()
reservations = {}  # job id -> (st_dev, bytes) for downloads currently running


class UserError(Exception):
    """An error whose message is shown to the user as-is."""


class Cancelled(Exception):
    pass


def estimate_size(info):
    """Best-effort size in bytes of what yt-dlp is about to download, or None if unknown."""
    if info.get("_type") == "playlist":
        return None
    formats = info.get("requested_formats") or [info]
    total = 0
    for f in formats:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size:
            tbr, dur = f.get("tbr") or info.get("tbr"), info.get("duration")
            if not (tbr and dur):
                return None
            size = tbr * 1000 / 8 * dur  # kbit/s * seconds -> bytes
        total += size
    return int(total)


def bytes_needed(est, info, fmt):
    """Peak disk usage: merging keeps the parts and the merged file at once; mp3 keeps the source too."""
    if est is None:
        return None
    if fmt == "audio":
        factor = 1.6
    elif len(info.get("requested_formats") or []) > 1:
        factor = 2.0
    else:
        factor = 1.05
    return int(est * factor)


def claim_space(jid, save_dir, need):
    dev = os.stat(save_dir).st_dev
    with reserve_lock:
        free = shutil.disk_usage(save_dir).free
        others = sum(b for j, (d, b) in reservations.items() if d == dev and j != jid)
        available = free - others - RESERVE
        if need is None:
            if available <= 0:
                raise UserError(f"Not enough disk space in {save_dir}: only {human(free)} free "
                                f"(keeping {human(RESERVE)} in reserve). File size is unknown.")
        elif need > available:
            msg = (f"Not enough disk space in {save_dir}: needs about {human(need)} "
                   f"(including temporary files), "
                   f"but only {human(max(available, 0))} is usable ({human(free)} free, "
                   f"{human(RESERVE)} kept in reserve")
            msg += f", {human(others)} held by other downloads)." if others else ")."
            raise UserError(msg)
        reservations[jid] = (dev, need or 0)


# ---------------------------------------------------------------- worker
paused = threading.Event()
cancel_flags = set()


def claim_next():
    with db_lock:
        row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY position, id LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE jobs SET status='checking', error=NULL WHERE id=?", (row["id"],))
            db.commit()
        return dict(row) if row else None


def run_job(job):
    jid = job["id"]
    save_dir = Path(job["save_dir"] or default_dir())
    state = {"t": 0.0, "disk_t": 0.0, "abort": None}

    def hook(d):
        if jid in cancel_flags:
            raise Cancelled()
        now = time.time()
        if now - state["disk_t"] > 3:
            state["disk_t"] = now
            free = shutil.disk_usage(save_dir).free
            if free < MIN_FREE_RUNNING:
                state["abort"] = f"Stopped: disk almost full during download ({human(free)} left in {save_dir})."
                raise UserError(state["abort"])
        if d["status"] == "downloading":
            if now - state["t"] < 0.5:
                return
            state["t"] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = (d.get("downloaded_bytes", 0) / total * 100) if total else None
            update(jid, status="downloading", progress=pct, speed=d.get("speed"), eta=d.get("eta"))
        elif d["status"] == "finished":
            update(jid, status="processing", progress=100, speed=None, eta=None)

    opts = {
        "outtmpl": str(save_dir / "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        **copy.deepcopy(FORMATS.get(job["format"], FORMATS["best"])),
    }
    try:
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise UserError(f"Can't create folder {save_dir}: {e.strerror}")
        if not os.access(save_dir, os.W_OK):
            raise UserError(f"No write permission for {save_dir}")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=False)
            if jid in cancel_flags:
                raise Cancelled()
            est = estimate_size(info)
            update(jid, title=info.get("title"), size_est=est)
            claim_space(jid, save_dir, bytes_needed(est, info, job["format"]))
            update(jid, status="downloading")
            info = ydl.process_ie_result(info, download=True)

        downloads = info.get("requested_downloads") or [{}]
        path = downloads[0].get("filepath") or downloads[0].get("_filename")
        update(jid, status="done", progress=100, speed=None, eta=None,
               title=info.get("title"), filepath=path, finished_at=time.time())
    except BaseException as e:  # yt-dlp may wrap our own exceptions in DownloadError
        if jid in cancel_flags:
            update(jid, status="cancelled", speed=None, eta=None, finished_at=time.time())
        else:
            msg = state["abort"] or (str(e) if isinstance(e, UserError) else
                                     ANSI.sub("", str(e)).replace("ERROR: ", "").strip())
            update(jid, status="error", error=msg[:600], speed=None, eta=None, finished_at=time.time())
    finally:
        cancel_flags.discard(jid)
        with reserve_lock:
            reservations.pop(jid, None)


def worker_loop():
    while True:
        if paused.is_set():
            time.sleep(0.5)
            continue
        job = claim_next()
        if job:
            run_job(job)
        else:
            time.sleep(1)


# ---------------------------------------------------------------- api
app = Flask(__name__, static_folder=None)


def request_hostname():
    host = request.host.lower()
    if host.startswith("["):  # [::1]:8765
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if ":" in host else host


@app.before_request
def local_only():
    # Block DNS-rebinding and cross-site requests from random web pages you have open.
    if "*" not in ALLOWED_HOSTNAMES and request_hostname() not in ALLOWED_HOSTNAMES:
        abort(403)
    if request.method != "GET" and request.headers.get("X-YTQ") != "1":
        abort(403)


def body():
    return request.get_json(silent=True) or {}


@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")


@app.get("/api/jobs")
def list_jobs():
    jobs = q("SELECT * FROM jobs ORDER BY "
             "CASE WHEN status IN ('checking','downloading','processing') THEN 0 "
             "WHEN status='queued' THEN 1 ELSE 2 END, "
             "CASE WHEN status='queued' THEN position END, finished_at DESC, id DESC")
    return jsonify(jobs=jobs, paused=paused.is_set(), default_dir=default_dir(), reserve=RESERVE,
                   can_open=not HEADLESS)


@app.post("/api/jobs")
def add_jobs():
    data = body()
    urls = URL_RE.findall(data.get("urls", ""))
    fmt = data.get("format", "best")
    if fmt not in FORMATS:
        return jsonify(error="unknown format"), 400
    if not urls:
        return jsonify(error="No links found"), 400
    try:
        save_dir = validate_dir(data.get("save_dir") or default_dir())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    info = disk_info(save_dir)
    if info["free"] <= RESERVE:
        return jsonify(error=f"Only {human(info['free'])} free in {save_dir}, which is below the "
                             f"{human(RESERVE)} reserve — choose another folder"), 400
    top = q("SELECT COALESCE(MAX(position), 0) AS p FROM jobs", one=True)["p"]
    for i, url in enumerate(urls, 1):
        q("INSERT INTO jobs (url, format, save_dir, position, created_at) VALUES (?,?,?,?,?)",
          (url, fmt, save_dir, top + i, time.time()))
    return jsonify(added=len(urls))


@app.get("/api/settings")
def get_settings():
    return jsonify(default_dir=default_dir())


@app.post("/api/settings")
def save_settings():
    try:
        path = validate_dir(body().get("default_dir"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    set_setting("default_dir", path)
    return jsonify(default_dir=path, disk=disk_info(path))


@app.get("/api/disk")
def disk():
    try:
        return jsonify(disk_info(request.args.get("path") or default_dir()))
    except OSError as e:
        return jsonify(error=e.strerror or str(e)), 400


@app.get("/api/browse")
def browse():
    raw = request.args.get("path", "")
    shortcuts = [{"label": p, "path": p} for p in EXTRA_SHORTCUTS if os.path.isdir(p)]
    if not HEADLESS:
        shortcuts.append({"label": "Home", "path": str(Path.home())})
    for name in ("Downloads", "Videos", "Movies", "Music", "Desktop"):
        p = Path.home() / name
        if p.is_dir() and not HEADLESS:
            shortcuts.append({"label": name, "path": str(p)})
    if HEADLESS:
        pass
    elif sys.platform.startswith("win"):
        shortcuts += [{"label": f"{d}:", "path": f"{d}:\\"}
                      for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    else:
        for mnt in ("/media", "/mnt", "/Volumes", f"/run/media/{os.environ.get('USER', '')}"):
            if os.path.isdir(mnt):
                shortcuts.append({"label": mnt, "path": mnt})
    try:
        p = norm(raw) if raw else norm(default_dir())
        if not p.is_dir():
            p = existing_parent(p)
        dirs = sorted((c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")),
                      key=lambda c: c.name.lower())
        return jsonify(path=str(p), parent=str(p.parent) if p.parent != p else None,
                       dirs=[{"name": c.name, "path": str(c)} for c in dirs],
                       shortcuts=shortcuts, disk=disk_info(p))
    except OSError as e:
        return jsonify(error=e.strerror or str(e), shortcuts=shortcuts), 400


@app.post("/api/mkdir")
def mkdir():
    data = body()
    name = (data.get("name") or "").strip()
    if not name or any(c in name for c in '/\\:*?"<>|') or name in (".", ".."):
        return jsonify(error="Invalid folder name"), 400
    try:
        p = norm(data.get("parent", "")) / name
        p.mkdir()
    except OSError as e:
        return jsonify(error=e.strerror or str(e)), 400
    return jsonify(path=str(p))


@app.post("/api/jobs/<int:jid>/open")
def open_folder(jid):
    if HEADLESS:
        return jsonify(error="Opening folders isn't available in headless mode"), 400
    job = q("SELECT save_dir, filepath FROM jobs WHERE id=?", (jid,), one=True)
    if not job:
        return jsonify(error="not found"), 404
    target = job["save_dir"] or default_dir()
    if not os.path.isdir(target):
        return jsonify(error="Folder no longer exists"), 404
    open_in_file_manager(target)
    return jsonify(ok=True)


@app.post("/api/jobs/<int:jid>/cancel")
def cancel(jid):
    job = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if not job:
        return jsonify(error="not found"), 404
    if job["status"] in ACTIVE:
        cancel_flags.add(jid)
    elif job["status"] == "queued":
        update(jid, status="cancelled", finished_at=time.time())
    return jsonify(ok=True)


@app.post("/api/jobs/<int:jid>/retry")
def retry(jid):
    top = q("SELECT COALESCE(MAX(position), 0) AS p FROM jobs", one=True)["p"]
    q("UPDATE jobs SET status='queued', progress=NULL, error=NULL, finished_at=NULL, position=? "
      "WHERE id=? AND status IN ('error','cancelled','done')", (top + 1, jid))
    return jsonify(ok=True)


@app.post("/api/jobs/<int:jid>/move")
def move(jid):
    direction = body().get("dir")
    queued = q("SELECT id, position FROM jobs WHERE status='queued' ORDER BY position, id")
    ids = [j["id"] for j in queued]
    if jid not in ids:
        return jsonify(ok=False)
    i = ids.index(jid)
    if direction == "top":
        update(jid, position=queued[0]["position"] - 1)
    else:
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(queued):
            a, b = queued[i], queued[j]
            update(a["id"], position=b["position"])
            update(b["id"], position=a["position"])
    return jsonify(ok=True)


@app.delete("/api/jobs/<int:jid>")
def delete(jid):
    job = q("SELECT status FROM jobs WHERE id=?", (jid,), one=True)
    if job and job["status"] in ACTIVE:
        return jsonify(error="cancel it first"), 409
    q("DELETE FROM jobs WHERE id=?", (jid,))
    return jsonify(ok=True)


@app.post("/api/clear")
def clear():
    q("DELETE FROM jobs WHERE status IN ('done','cancelled')")
    return jsonify(ok=True)


@app.post("/api/pause")
def pause():
    paused.set()
    return jsonify(paused=True)


@app.post("/api/resume")
def resume():
    paused.clear()
    return jsonify(paused=False)


# ---------------------------------------------------------------- launcher
class DesktopApi:
    """Exposed to the page as window.pywebview.api when running as a desktop window."""

    def pick_folder(self, start=None):
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:
            kind = webview.FOLDER_DIALOG
        result = webview.windows[0].create_file_dialog(kind, directory=start or "")
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result


def run_server():
    try:
        from waitress import serve  # production-grade server, used when installed (e.g. in Docker)
        serve(app, host=HOST, port=PORT, threads=8, _quiet=True)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


def wait_for_server(url, timeout=10):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            opener.open(url + "api/settings", timeout=1)
            return
        except OSError:
            time.sleep(0.2)


def main():
    url = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '::') else HOST}:{PORT}/"
    for _ in range(max(1, WORKERS)):
        threading.Thread(target=worker_loop, daemon=True).start()
    print(f"ytqueue: {url}  (default folder: {default_dir()})")

    if HEADLESS:
        run_server()
        return

    server_started = False
    if "--browser" not in sys.argv:
        try:
            import webview
            threading.Thread(target=run_server, daemon=True).start()
            server_started = True
            wait_for_server(url)
            webview.create_window("ytqueue", url, js_api=DesktopApi(),
                                  width=1000, height=780, min_size=(560, 520))
            webview.start()
            return
        except Exception as e:  # pywebview missing, or no GUI backend on this system
            print(f"Desktop window unavailable ({e.__class__.__name__}: {e}); opening in your browser.")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if server_started:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        run_server()


if __name__ == "__main__":
    main()
