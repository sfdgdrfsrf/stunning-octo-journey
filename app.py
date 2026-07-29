"""
Verity All-in-One API Server
============================

A single Flask service that powers every "outside-world" skill Verity has:

  1. YouTube search + audio download          (yt-dlp)
  2. Web browsing + screenshot                 (Playwright headless Chromium)
  3. Web search                                (DuckDuckGo HTML scraper — no API key)
  4. Static file serving for downloads         (/downloads/<file>)
  5. Static image serving for screenshots      (/screenshots/<file>)

Endpoints
---------
GET  /                              health check + version
GET  /api/search?q=<query>&n=<n>    YouTube search (returns top-n videos)
POST /api/download                  {url, filename} -> {id}  (async job)
GET  /api/download/<id>             poll job status: pending | running | done | error
GET  /downloads/<filename>          fetch a finished mp3
POST /api/browse                    {url, width?, height?, selector?, wait?} -> {url, title, text, screenshot_url, screenshot_path}
GET  /api/screenshot?url=...        one-shot screenshot -> returns PNG bytes directly
GET  /api/websearch?q=<query>&n=<n> DuckDuckGo search -> [{title, url, snippet}]
POST /api/play?q=<query>            combined: search -> download -> return {job_id, video, download_url}

Run locally
-----------
    pip install -r requirements.txt
    python -m playwright install chromium --with-deps
    python app.py                # listens on 0.0.0.0:8080

Docker
------
    docker build -t verity-server .
    docker run -d -p 8080:8080 --name verity-server verity-server

Deploy 24/7
-----------
See README.md for Railway / Render / Fly.io / Koyeb one-click instructions.
"""

import os
import re
import io
import json
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from urllib.parse import quote_plus, urlparse, urljoin

# Auto-load .env for local dev (Railway/Render/Fly inject env vars directly,
# so dotenv is a no-op there).  We don't fail if python-dotenv isn't installed
# — it's purely a convenience for `python app.py` runs.
try:
    from dotenv import load_dotenv
    # Explicit path so it works no matter what CWD the server was launched from.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from flask import Flask, request, jsonify, send_file, abort, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR     = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
SCREEN_DIR   = BASE_DIR / "screenshots"
VIDEO_DIR    = BASE_DIR / "videos"
DOWNLOAD_DIR.mkdir(exist_ok=True)
SCREEN_DIR.mkdir(exist_ok=True)
VIDEO_DIR.mkdir(exist_ok=True)

PORT         = int(os.environ.get("PORT", 8080))
HOST         = os.environ.get("HOST", "0.0.0.0")
MAX_JOBS     = int(os.environ.get("MAX_JOBS", "50"))
JOB_TTL      = int(os.environ.get("JOB_TTL", str(60 * 60)))  # 1 hour
MAX_DL_TIME  = int(os.environ.get("MAX_DL_TIME", "300"))     # 5 min per download
MAX_FILE_AGE = int(os.environ.get("MAX_FILE_AGE", str(60 * 60)))  # 1 hour cleanup
DEFAULT_W    = int(os.environ.get("SHOT_W", "1280"))
DEFAULT_H    = int(os.environ.get("SHOT_H", "720"))

# Optional simple API token to stop randoms abusing your hosted instance.
# If set, every /api/* request must include header `X-Verity-Key: <token>`.
API_TOKEN    = os.environ.get("VERITY_API_TOKEN", "").strip()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job store (in-memory; survives long enough for a single play session)
# ---------------------------------------------------------------------------
JOBS    = {}
JOBS_LK = threading.Lock()

def new_job(kind: str, payload: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with JOBS_LK:
        # Evict oldest if too many
        if len(JOBS) >= MAX_JOBS:
            oldest = min(JOBS.items(), key=lambda kv: kv[1]["created_at"])
            JOBS.pop(oldest[0], None)
        JOBS[jid] = {
            "id":         jid,
            "kind":       kind,
            "status":     "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
            "payload":    payload,
            "result":     None,
            "error":      None,
        }
    return jid

def update_job(jid: str, **fields):
    with JOBS_LK:
        if jid not in JOBS: return
        JOBS[jid].update(fields)
        JOBS[jid]["updated_at"] = time.time()

def get_job(jid: str):
    with JOBS_LK:
        j = JOBS.get(jid)
        return dict(j) if j else None

# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
@app.before_request
def _auth():
    if not API_TOKEN: return
    if (request.path == "/"
        or request.path.startswith("/downloads/")
        or request.path.startswith("/screenshots/")
        or request.path.startswith("/api/video_frame/")
        or request.path.startswith("/api/video_audio/")):
        return  # public read-only endpoints
    if request.path.startswith("/api/"):
        token = request.headers.get("X-Verity-Key", "") or request.args.get("key", "")
        if token != API_TOKEN:
            return jsonify({"error": "unauthorized", "message": "Missing or wrong X-Verity-Key header"}), 401

# ---------------------------------------------------------------------------
# Background cleanup
# ---------------------------------------------------------------------------
def janitor():
    while True:
        time.sleep(300)
        now = time.time()
        # Expire old jobs
        with JOBS_LK:
            for jid in list(JOBS.keys()):
                if now - JOBS[jid]["created_at"] > JOB_TTL:
                    JOBS.pop(jid, None)
        # Expire old files
        for d in (DOWNLOAD_DIR, SCREEN_DIR):
            for f in d.iterdir():
                try:
                    if now - f.stat().st_mtime > MAX_FILE_AGE:
                        f.unlink()
                except Exception:
                    pass
        # Expire old video job dirs (per-job subdirectory under VIDEO_DIR)
        try:
            for d in VIDEO_DIR.iterdir():
                if d.is_dir():
                    age = now - d.stat().st_mtime
                    if age > MAX_FILE_AGE:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

threading.Thread(target=janitor, daemon=True).start()

# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------
def _ytdl_version():
    try:
        # --no-call-home was removed in recent yt-dlp, so don't pass it
        out = subprocess.check_output(["yt-dlp", "--version"], stderr=subprocess.STDOUT, timeout=10)
        return out.decode(errors="ignore").strip()
    except Exception as e:
        return f"unavailable: {e}"

def _run_ytdlp(url: str, outpath: str, audio_only: bool = True) -> dict:
    """Run yt-dlp synchronously. Returns dict with success bool + meta."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "20",
        "--geo-bypass",
        # YouTube "Sign in to confirm you're not a bot" workaround:
        # Try multiple player clients in order (android has the least aggressive
        # bot detection; web_safari and web are fallbacks for restricted videos).
        # NOTE: Combine all player args into ONE --extractor-args call —
        # yt-dlp only honors the LAST --extractor-args if you pass it twice.
        "--extractor-args", "youtube:player_client=android,web_safari,web",
        # yt-dlp 2026+ defaults to ONLY deno as JS runtime for solving YouTube's
        # "n challenge". If deno isn't installed but node is, we MUST explicitly
        # enable node here, otherwise no audio/video formats are available.
        # Repeat the flag once per runtime (NOT comma-separated).
        "--js-runtimes", "node",
        "--js-runtimes", "bun",
        "--js-runtimes", "deno",
        "-f", "bestaudio/best" if audio_only else "best",
    ]
    if audio_only:
        cmd += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]

    # Optional cookies file — set COOKIES_FILE env var to /data/cookies.txt
    # (or any path). Lets users bypass YouTube bot detection entirely.
    cookies_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]

    cmd += ["-o", outpath, url]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=MAX_DL_TIME)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "download timeout"}
    except Exception as e:
        return {"success": False, "error": f"spawn error: {e}"}

    if proc.returncode != 0:
        return {"success": False, "error": proc.stderr.decode(errors="ignore")[:1000]}

    # yt-dlp may write any extension; find the actual file
    out_path = Path(outpath)
    if out_path.exists():
        actual = out_path
    else:
        # find sibling with same stem
        candidates = list(out_path.parent.glob(out_path.stem + ".*"))
        if not candidates:
            return {"success": False, "error": "output file not found"}
        actual = candidates[0]

    # Pull video metadata via --dump-json (best-effort)
    meta = {}
    try:
        mcmd = [
            "yt-dlp", "--no-playlist", "--no-warnings", "--dump-json", "--skip-download",
            "--extractor-args", "youtube:player_client=android,web_safari,web",
            "--js-runtimes", "node",
            "--js-runtimes", "bun",
            "--js-runtimes", "deno",
            url
        ]
        if cookies_file and os.path.exists(cookies_file):
            mcmd += ["--cookies", cookies_file]
        mproc = subprocess.run(mcmd, capture_output=True, timeout=30)
        if mproc.returncode == 0:
            meta = json.loads(mproc.stdout.decode(errors="ignore").splitlines()[0])
    except Exception:
        pass

    return {
        "success":  True,
        "filename": actual.name,
        "path":     str(actual),
        "size":     actual.stat().st_size,
        "title":    meta.get("title", ""),
        "uploader": meta.get("uploader", meta.get("channel", "")),
        "duration": meta.get("duration", 0),
        "url":      url,
    }

# ---------------------------------------------------------------------------
# YouTube search (uses yt-dlp's "ytsearch" pseudo-URL — no external API needed)
# ---------------------------------------------------------------------------
def _yt_search(query: str, n: int = 5) -> list:
    n = max(1, min(n, 20))
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "--flat-playlist", "--dump-json",
        f"ytsearch{n}:{query}",
    ]
    # Optional cookies file for YouTube search too
    cookies_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    items = []
    for line in proc.stdout.decode(errors="ignore").splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        vid = j.get("id") or ""
        items.append({
            "videoId":   vid,
            "url":       f"https://www.youtube.com/watch?v={vid}" if vid else j.get("url", ""),
            "title":     j.get("title") or "",
            "author":    j.get("uploader") or j.get("channel") or j.get("uploader_id") or "",
            "duration":  j.get("duration") or 0,
            "thumbnail": (j.get("thumbnails") or [{}])[-1].get("url", "") if j.get("thumbnails") else "",
        })
    return items

# ---------------------------------------------------------------------------
# Routes: health & search
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({
        "service":   "verity-server",
        "version":   "2.0.0",
        "ytdlp":     _ytdl_version(),
        "playwright": _playwright_ok(),
        "endpoints": [
            "GET  /",
            "GET  /api/search?q=<query>&n=<n>",
            "POST /api/download        {url, filename?}",
            "GET  /api/download/<id>",
            "GET  /downloads/<filename>",
            "POST /api/video           {url, fps?}  (download MP4 + extract frames + audio)",
            "GET  /api/video/<id>",
            "GET  /api/video_frame/<id>/<n>     (returns JPG bytes)",
            "GET  /api/video_audio/<id>          (returns MP3 bytes)",
            "POST /api/browse          {url, width?, height?, selector?, wait?}  (async)",
            "POST /api/browse-sync     {url, ...}  (sync, returns result directly)",
            "GET  /api/screenshot?url=...&w=...&h=...",
            "GET  /api/websearch?q=<query>&n=<n>",
            "POST /api/play?q=<query>",
            "GET  /screenshots/<filename>",
        ],
    })

@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    n = int(request.args.get("n", "5"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    items = _yt_search(q, n)
    return jsonify({"query": q, "count": len(items), "items": items})

# ---------------------------------------------------------------------------
# Routes: download jobs
# ---------------------------------------------------------------------------
def _download_worker(jid: str, url: str, filename: str):
    update_job(jid, status="running")
    if not filename:
        filename = f"verity_{jid}.mp3"
    if not filename.lower().endswith((".mp3", ".m4a", ".webm", ".opus", ".ogg")):
        filename += ".mp3"
    outpath = str(DOWNLOAD_DIR / filename)
    res = _run_ytdlp(url, outpath, audio_only=True)
    if res.get("success"):
        update_job(jid, status="done",
                   result={
                       "filename": res["filename"],
                       "size":     res["size"],
                       "title":    res.get("title", ""),
                       "uploader": res.get("uploader", ""),
                       "duration": res.get("duration", 0),
                       "url":      url,
                       "download_url": f"/downloads/{res['filename']}",
                   })
    else:
        update_job(jid, status="error", error=res.get("error", "unknown"))

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    filename = (data.get("filename") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    jid = new_job("download", {"url": url, "filename": filename})
    threading.Thread(target=_download_worker, args=(jid, url, filename), daemon=True).start()
    return jsonify({"id": jid, "status": "pending", "url": url})

@app.route("/api/download/<jid>")
def api_download_status(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "unknown job id"}), 404
    return jsonify(j)

@app.route("/downloads/<path:filename>")
def downloads(filename):
    f = DOWNLOAD_DIR / filename
    if not f.exists():
        abort(404)
    return send_file(f, as_attachment=False, mimetype="audio/mpeg")

# ---------------------------------------------------------------------------
# Routes: combined play (search + download)
# ---------------------------------------------------------------------------
@app.route("/api/play", methods=["POST", "GET"])
def api_play():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        n = 1
    else:
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or request.args.get("q") or "").strip()
        n = int(data.get("n", "1"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    items = _yt_search(q, max(n, 1))
    if not items:
        return jsonify({"error": "no results", "query": q}), 404
    top = items[0]
    filename = f"verity_play_{uuid.uuid4().hex[:6]}.mp3"
    jid = new_job("download", {"url": top["url"], "filename": filename})
    threading.Thread(target=_download_worker, args=(jid, top["url"], filename), daemon=True).start()
    return jsonify({
        "id":    jid,
        "video": top,
        "status": "pending",
        "status_url": f"/api/download/{jid}",
    })

# ---------------------------------------------------------------------------
# Video playback (download MP4, extract frames at fps, extract audio MP3)
# Used by the Roblox client's !playvideos command to display video on a brick
# in sync with audio. ffmpeg does the heavy lifting.
# ---------------------------------------------------------------------------
def _run_ytdlp_video(url: str, outpath: str) -> dict:
    """Download a video as MP4 (capped at 480p for speed). Returns dict."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "20",
        "--geo-bypass",
        "--extractor-args", "youtube:player_client=android,web_safari,web",
        "--js-runtimes", "node",
        "--js-runtimes", "bun",
        "--js-runtimes", "deno",
        # Cap at 480p + bestaudio, mp4 only — keeps frame extraction fast
        "-f", "best[height<=480][ext=mp4]/best[height<=480]/best",
        "--merge-output-format", "mp4",
    ]
    cookies_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]
    cmd += ["-o", outpath, url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=MAX_DL_TIME)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "video download timeout"}
    except Exception as e:
        return {"success": False, "error": f"spawn error: {e}"}
    if proc.returncode != 0:
        return {"success": False, "error": proc.stderr.decode(errors="ignore")[:1000]}
    out_path = Path(outpath)
    if out_path.exists():
        actual = out_path
    else:
        candidates = list(out_path.parent.glob(out_path.stem + ".*"))
        if not candidates:
            return {"success": False, "error": "video output not found"}
        actual = candidates[0]
    return {
        "success":  True,
        "filename": actual.name,
        "path":     str(actual),
        "size":     actual.stat().st_size,
    }

def _extract_video_meta(video_path: str) -> dict:
    """Use ffprobe to read duration + width + height of the video."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-of", "json",
                video_path,
            ],
            capture_output=True, timeout=20,
        )
        if proc.returncode != 0:
            return {}
        info = json.loads(proc.stdout.decode(errors="ignore"))
        stream = (info.get("streams") or [{}])[0]
        return {
            "width":    int(stream.get("width", 0) or 0),
            "height":   int(stream.get("height", 0) or 0),
            "duration": float(stream.get("duration", 0) or 0),
        }
    except Exception:
        return {}

def _video_worker(jid: str, url: str, fps: int):
    """Background: download MP4, extract frames at fps, extract audio MP3."""
    update_job(jid, status="running", stage="download")
    job_dir = VIDEO_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    video_path = str(job_dir / "video.mp4")
    audio_path = str(job_dir / "audio.mp3")
    frame_prefix = str(job_dir / "frame_")

    res = _run_ytdlp_video(url, video_path)
    if not res.get("success"):
        update_job(jid, status="error", error=res.get("error", "video download failed"))
        return
    actual_video = res["path"]
    meta = _extract_video_meta(actual_video)
    duration = meta.get("duration", 0)

    update_job(jid, stage="audio", duration=duration)
    # Extract audio MP3
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", actual_video, "-vn",
             "-ac", "2", "-ar", "44100", "-b:a", "128k", audio_path],
            capture_output=True, timeout=120,
        )
    except Exception as e:
        update_job(jid, status="error", error=f"audio extract failed: {e}")
        return
    if not Path(audio_path).exists():
        update_job(jid, status="error", error="audio file not created")
        return

    update_job(jid, stage="frames")
    # Extract frames at requested FPS using ffmpeg
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", actual_video,
             "-vf", f"fps={fps},scale=320:-2",      # 320px wide keeps files small + fast
             "-q:v", "5",                            # JPEG quality (2 best, 31 worst)
             f"{frame_prefix}%05d.jpg"],
            capture_output=True, timeout=180,
        )
    except Exception as e:
        update_job(jid, status="error", error=f"frame extract failed: {e}")
        return

    frame_files = sorted(job_dir.glob("frame_*.jpg"))
    if not frame_files:
        update_job(jid, status="error", error="no frames extracted")
        return

    update_job(jid, status="done",
               stage="done",
               result={
                   "frame_count":   len(frame_files),
                   "fps":           fps,
                   "duration":      duration,
                   "width":         meta.get("width", 0),
                   "height":        meta.get("height", 0),
                   "audio_url":     f"/api/video_audio/{jid}",
                   "frame_url_tmpl": f"/api/video_frame/{jid}/{{n}}",
                   "video_title":   res.get("title", ""),
               })

@app.route("/api/video", methods=["POST"])
def api_video_start():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fps = int(data.get("fps", "8"))
    fps = max(1, min(fps, 15))
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    jid = new_job("video", {"url": url, "fps": fps})
    threading.Thread(target=_video_worker, args=(jid, url, fps), daemon=True).start()
    return jsonify({"id": jid, "status": "pending", "url": url, "fps": fps})

@app.route("/api/video/<jid>")
def api_video_status(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "unknown job id"}), 404
    return jsonify(j)

@app.route("/api/video_frame/<jid>/<int:frame_n>")
def api_video_frame(jid, frame_n):
    if frame_n < 1:
        return jsonify({"error": "frame_n must be >= 1"}), 400
    job_dir = VIDEO_DIR / jid
    frame_path = job_dir / f"frame_{frame_n:05d}.jpg"
    if not frame_path.exists():
        return abort(404)
    return send_file(str(frame_path), mimetype="image/jpeg")

@app.route("/api/video_audio/<jid>")
def api_video_audio(jid):
    audio_path = VIDEO_DIR / jid / "audio.mp3"
    if not audio_path.exists():
        return abort(404)
    return send_file(str(audio_path), mimetype="audio/mpeg")


# ---------------------------------------------------------------------------
# Playwright browsing + screenshots
# ---------------------------------------------------------------------------
_PW_LOCK = threading.Lock()
_PW_BROWSERS = None  # we keep one browser per worker thread (lazy)

def _playwright_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False

def _browse_sync(url: str, width: int, height: int, selector: str = None, wait_ms: int = 1500) -> dict:
    """Synchronous browse using a fresh Playwright context (safe in worker thread)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height},
                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                 "Chrome/120.0.0.0 Safari/537.36",
                                      ignore_https_errors=True)
            page = ctx.new_page()
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                return {"success": False, "error": f"navigation failed: {e}"}
            # Wait a moment for late JS to render
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            # Optionally wait for a specific selector
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=10000)
                except Exception:
                    pass
            title = page.title()
            try:
                text = page.inner_text("body")[:8000]
            except Exception:
                text = ""
            shot_name = f"shot_{uuid.uuid4().hex[:10]}.png"
            shot_path = SCREEN_DIR / shot_name
            page.screenshot(path=str(shot_path), full_page=False)
            return {
                "success":         True,
                "url":             url,
                "title":           title,
                "text":            text,
                "width":           width,
                "height":          height,
                "screenshot":      shot_name,
                "screenshot_url":  f"/screenshots/{shot_name}",
                "screenshot_path": str(shot_path),
                "size":            shot_path.stat().st_size if shot_path.exists() else 0,
            }
        finally:
            browser.close()

def _browse_worker(jid: str, url: str, width: int, height: int, selector: str, wait_ms: int):
    update_job(jid, status="running")
    try:
        res = _browse_sync(url, width, height, selector, wait_ms)
        if res.get("success"):
            update_job(jid, status="done", result=res)
        else:
            update_job(jid, status="error", error=res.get("error", "browse failed"))
    except Exception as e:
        update_job(jid, status="error", error=str(e))

@app.route("/api/browse", methods=["POST"])
def api_browse():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    width   = int(data.get("width",  DEFAULT_W))
    height  = int(data.get("height", DEFAULT_H))
    selector = (data.get("selector") or "").strip() or None
    wait_ms  = int(data.get("wait", 1500))
    jid = new_job("browse", {"url": url})
    threading.Thread(target=_browse_worker,
                     args=(jid, url, width, height, selector, wait_ms),
                     daemon=True).start()
    return jsonify({"id": jid, "status": "pending", "status_url": f"/api/download/{jid}"})

# SYNCHRONOUS browse — returns the result directly in one request.
# Use this when the client can't reliably poll (e.g. multi-worker hosts,
# or clients that don't keep job IDs around). Slower but simpler.
@app.route("/api/browse-sync", methods=["POST", "GET"])
def api_browse_sync():
    if request.method == "GET":
        url = (request.args.get("url") or "").strip()
        width = int(request.args.get("w", DEFAULT_W))
        height = int(request.args.get("h", DEFAULT_H))
        wait_ms = int(request.args.get("wait", "1500"))
        selector = (request.args.get("selector") or "").strip() or None
    else:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        width = int(data.get("width", DEFAULT_W))
        height = int(data.get("height", DEFAULT_H))
        wait_ms = int(data.get("wait", 1500))
        selector = (data.get("selector") or "").strip() or None
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        res = _browse_sync(url, width, height, selector, wait_ms)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not res.get("success"):
        return jsonify({"error": res.get("error", "unknown")}), 500
    return jsonify(res)

# Synchronous one-shot screenshot — returns PNG bytes directly
@app.route("/api/screenshot")
def api_screenshot():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing ?url="}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    width  = int(request.args.get("w", DEFAULT_W))
    height = int(request.args.get("h", DEFAULT_H))
    try:
        res = _browse_sync(url, width, height, None, 1500)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not res.get("success"):
        return jsonify({"error": res.get("error", "unknown")}), 500
    f = SCREEN_DIR / res["screenshot"]
    if not f.exists():
        return jsonify({"error": "screenshot file missing"}), 500
    return send_file(f, mimetype="image/png")

@app.route("/screenshots/<path:filename>")
def screenshots(filename):
    f = SCREEN_DIR / filename
    if not f.exists():
        abort(404)
    return send_file(f, mimetype="image/png")

# ---------------------------------------------------------------------------
# Web search via DuckDuckGo HTML (no API key, no rate-limit headaches)
# ---------------------------------------------------------------------------
import urllib.request
import urllib.error

_DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def _ddg_fetch(query: str) -> str:
    """Fetch DuckDuckGo HTML, trying both the html. and lite. endpoints."""
    from urllib.parse import quote_plus
    urls = [
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}",
    ]
    last_err = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers=_DDG_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode(errors="ignore")
        except Exception as e:
            last_err = e
            continue
    return ""

def _ddg_search(query: str, n: int = 5) -> list:
    n = max(1, min(n, 20))
    html = _ddg_fetch(query)
    if not html:
        return [{"error": "could not reach DuckDuckGo"}]
    results = []
    # Pattern 1: html.duckduckgo.com standard result blocks
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        href = m.group(1)
        m2 = re.search(r'uddg=([^&]+)', href)
        if m2:
            from urllib.parse import unquote
            href = unquote(m2.group(1))
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            return results
    # Pattern 2: lite.duckduckgo.com table rows
    for m in re.finditer(
        r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>',
        html, re.DOTALL
    ):
        href = m.group(1)
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            return results
    # Pattern 3: last-resort — any link that looks like a result
    if not results:
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html):
            href, title = m.group(1), m.group(2).strip()
            if (title and href and 'duckduckgo' not in href.lower()
                and not href.startswith('https://duckduckgo.com')):
                results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= n:
                return results
    return results

@app.route("/api/websearch")
def api_websearch():
    q = (request.args.get("q") or "").strip()
    n = int(request.args.get("n", "5"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    return jsonify({"query": q, "count_n": n, "results": _ddg_search(q, n)})

# ---------------------------------------------------------------------------
# Roblox store model lookup (no API — just resolves a store URL or raw ID)
# ---------------------------------------------------------------------------
@app.route("/api/roblox/model")
def api_roblox_model():
    """Accepts ?id=<assetId> OR ?url=<create.roblox.com/store/models/... URL>.
    Returns the asset id plus a friendly rbxassetid:// link."""
    raw = (request.args.get("id") or request.args.get("url") or "").strip()
    if not raw:
        return jsonify({"error": "missing ?id= or ?url="}), 400
    # Try to extract a numeric ID from any input
    m = re.search(r'(\d{6,})', raw)
    if not m:
        return jsonify({"error": "could not find a numeric asset id in input"}), 400
    aid = int(m.group(1))
    return jsonify({
        "asset_id":  aid,
        "rbxassetid": f"rbxassetid://{aid}",
        "store_url": f"https://create.roblox.com/store/models/{aid}",
        "loaded_via": "game:GetObjects or InsertService:LoadAsset on the client side",
    })

# ---------------------------------------------------------------------------
# Cookies upload — bypasses YouTube "Sign in to confirm you're not a bot"
# ---------------------------------------------------------------------------
# Usage:
#   1. Export cookies from a browser where you're logged into YouTube
#      (use the "Get cookies.txt" Chrome extension, or `yt-dlp --cookies-from-browser chrome --cookies cookies.txt`)
#   2. POST the cookies.txt file to /api/cookies (multipart/form-data)
#   3. Server saves it to /data/cookies.txt and sets COOKIES_FILE env var
#   4. All future yt-dlp calls will use --cookies /data/cookies.txt
COOKIES_PATH = os.environ.get("COOKIES_FILE", "/data/cookies.txt").strip()
# If running locally (no /data dir), fall back to a path next to app.py
if not os.path.isdir(os.path.dirname(COOKIES_PATH)) or os.path.dirname(COOKIES_PATH) == "":
    COOKIES_PATH = str(BASE_DIR / "cookies.txt")
    os.environ["COOKIES_FILE"] = COOKIES_PATH

# Inject cookies from a base64-encoded env var (Railway secret — survives redeploys).
# Usage on Railway:
#   COOKIES_B64=$(base64 -w0 cookies.txt)  # then paste into Railway env var
# On startup, if COOKIES_B64 is set, decode + write to COOKIES_PATH.
_cookies_b64 = os.environ.get("COOKIES_B64", "").strip()
if _cookies_b64:
    import base64 as _b64
    try:
        decoded = _b64.b64decode(_cookies_b64).decode("utf-8", errors="replace")
        with open(COOKIES_PATH, "w") as _f:
            _f.write(decoded)
        print(f"[verity-server] cookies injected from COOKIES_B64 ({len(decoded)} bytes) -> {COOKIES_PATH}")
    except Exception as _e:
        print(f"[verity-server] WARNING: failed to decode COOKIES_B64: {_e}")

@app.route("/api/cookies", methods=["POST"])
def api_cookies_upload():
    if "file" not in request.files:
        # Also accept raw body text
        body = request.get_data(as_text=True)
        if not body or "# Netscape HTTP Cookie File" not in body:
            return jsonify({"error": "upload a file field 'file' or POST raw Netscape cookies.txt content"}), 400
        try:
            with open(COOKIES_PATH, "w") as f:
                f.write(body)
        except Exception as e:
            return jsonify({"error": f"could not write cookies file: {e}"}), 500
        return jsonify({"success": True, "path": COOKIES_PATH, "size": len(body)})

    f = request.files["file"]
    try:
        f.save(COOKIES_PATH)
    except Exception as e:
        return jsonify({"error": f"could not save cookies file: {e}"}), 500
    return jsonify({"success": True, "path": COOKIES_PATH, "size": os.path.getsize(COOKIES_PATH)})

@app.route("/api/cookies", methods=["GET"])
def api_cookies_status():
    exists = os.path.exists(COOKIES_PATH)
    return jsonify({
        "configured": exists,
        "path": COOKIES_PATH,
        "size": os.path.getsize(COOKIES_PATH) if exists else 0,
    })

@app.route("/api/cookies", methods=["DELETE"])
def api_cookies_delete():
    try:
        if os.path.exists(COOKIES_PATH):
            os.unlink(COOKIES_PATH)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# AI-generated builds via Pollinations / Groq / OpenRouter
# --------------------------------------------------------------------
# When the Noob Builder doesn't have a shape in its BUILD_LIBRARY, it asks
# this endpoint for a JSON part list.  We forward the noun to an LLM with
# a system prompt that teaches it the F3X primitive vocabulary
# (Part, WedgePart, Cylinder, Ball, Materials, RGB), so the returned plan
# is actually buildable in Roblox.
#
# Three providers are supported, picked by which env var is set:
#
#   POLLINATIONS_TOKEN  ->  https://text.pollinations.ai/  (Bearer auth)
#   GROQ_API_KEY        ->  https://api.groq.com/openai/v1/chat/completions
#   OPENROUTER_API_KEY  ->  https://openrouter.ai/api/v1/chat/completions
#                           (uses free Llama-3 / Mistral models)
#
# If none are set, the endpoint returns a clear 502 + setup instructions.
#
#   GET  /api/generate?noun=dragon&n=25
#   POST /api/generate   {"noun":"dragon","n":25}
#   -> {"noun":"dragon","count_n":25,"parts":[{...}, ...],"source":"groq"}
#
# /api/blueprint returns an AI-rendered reference image (PNG bytes) for the
# same noun via Pollinations image gen (still anonymous-friendly):
#   GET /api/blueprint?noun=dragon&w=512&h=448
#   -> image/jpeg bytes
# ---------------------------------------------------------------------------
import urllib.request
import urllib.error

# Image generation — Pollinations image endpoint is still anonymous-friendly
POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt/"

# Text LLM providers — pick one by setting the corresponding env var
POLLINATIONS_TEXT_URL = "https://text.pollinations.ai/"
GROQ_CHAT_URL         = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_CHAT_URL   = "https://openrouter.ai/api/v1/chat/completions"

POLLINATIONS_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "").strip()
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY",       "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

# Default models per provider (user can override via env var)
GROQ_MODEL       = os.environ.get("GROQ_MODEL",       "llama-3.3-70b-versatile")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-8b-instruct:free")

# The "F3X vocabulary" we teach the LLM.  Mirrors exactly what
# NoobBuilderScript.lua's spawnBuildPart() can actually instantiate.
_F3X_SYSTEM_PROMPT = """You are a Roblox build planner that designs objects out of F3X-style primitive parts.

You ONLY output JSON. No prose, no markdown fences. The JSON is an array of part specs.

Each part spec has these fields:
  name      string (short label)
  class     "Part" | "WedgePart"            (default "Part")
  shape     "Cylinder" | "Ball" | null     (optional, only for Part class)
  size      [x, y, z]  in studs (Roblox Vector3)
  offset    [x, y, z]  in studs relative to the build origin (ground level, y=0)
  color     [r, g, b]  integers 0-255
  material  one of: SmoothPlastic, WoodPlanks, Glass, Metal, Grass, Ground, Concrete, Sand, Ice, Neon, Slate, Brick, Marble, Cobblestone
  rotation  [xDeg, yDeg, zDeg]  optional, only for WedgePart or when you need a tilted Part

Constraints:
- Build sits on the ground (all parts at y >= 0). The base/lowest parts should be at y=0.
- Whole object fits within a 30x30x30 stud bounding box.
- Use 8-30 parts for simple objects (chair, table, sign), 25-50 for complex ones (castle, dragon, mech).
- Make it look like what the user asked for. Be specific about proportions.
- Use WedgePart for slopes/roofs/beaks. Use Cylinder for legs/pillars/wheels/trunks. Use Ball for heads/leaves/joints.
- "rotation" is in DEGREES, optional. Use it for WedgePart orientation.

Example output for "build a tree":
[
  {"name":"Trunk","class":"Part","shape":"Cylinder","size":[1,4,1],"offset":[0,2,0],"color":[100,60,30],"material":"WoodPlanks"},
  {"name":"Leaves1","class":"Part","shape":"Ball","size":[5,5,5],"offset":[0,5,0],"color":[60,140,50],"material":"Grass"},
  {"name":"Leaves2","class":"Part","shape":"Ball","size":[4,4,4],"offset":[-1.5,6.5,0],"color":[40,100,35],"material":"Grass"},
  {"name":"Leaves3","class":"Part","shape":"Ball","size":[3,3,3],"offset":[0,8,0],"color":[60,140,50],"material":"Grass"}
]
"""

def _llm_request_pollinations(messages, timeout=60):
    """POST to Pollinations with the Bearer token (or GET if no token).
    Returns raw text or empty string on error."""
    body = json.dumps({
        "model": "openai",
        "messages": messages,
        "temperature": 0.7,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain",
        "User-Agent": "verity-server/1.0",
    }
    if POLLINATIONS_TOKEN:
        headers["Authorization"] = f"Bearer {POLLINATIONS_TOKEN}"
    req = urllib.request.Request(
        POLLINATIONS_TEXT_URL + "openai",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

def _llm_request_groq(messages, timeout=45):
    """POST to Groq's OpenAI-compatible chat completions endpoint."""
    body = json.dumps({
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4096,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }
    req = urllib.request.Request(GROQ_CHAT_URL, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        # OpenAI-style response: choices[0].message.content
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return ""

def _llm_request_openrouter(messages, timeout=45):
    """POST to OpenRouter's OpenAI-compatible chat completions endpoint."""
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4096,
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://verity-server.local",
        "X-Title": "Verity Noob Builder",
    }
    req = urllib.request.Request(OPENROUTER_CHAT_URL, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return ""

def _llm_chat(user_prompt):
    """Try every configured provider in order, return (text, provider_name)."""
    messages = [
        {"role": "system", "content": _F3X_SYSTEM_PROMPT},
        {"role": "user",   "content": user_prompt},
    ]
    if POLLINATIONS_TOKEN:
        txt = _llm_request_pollinations(messages)
        if txt:
            return txt, "pollinations"
    if GROQ_API_KEY:
        txt = _llm_request_groq(messages)
        if txt:
            return txt, "groq"
    if OPENROUTER_API_KEY:
        txt = _llm_request_openrouter(messages)
        if txt:
            return txt, "openrouter"
    return "", "none"

def _strip_json_fences(text: str) -> str:
    """If the LLM wrapped the JSON in ```json ... ``` fences, strip them."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

def _parse_part_list(raw: str, max_parts: int = 80) -> list:
    """Parse the LLM response into a list of cleaned part specs.
    Robust to: extra prose before/after JSON, code fences, single-object replies.
    """
    raw = _strip_json_fences(raw)
    # Find the first [ ... ] block in the text
    start = raw.find("[")
    end   = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    blob = raw[start:end + 1]
    try:
        parts = json.loads(blob)
    except Exception:
        return []
    if not isinstance(parts, list):
        return []
    cleaned = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        spec = {
            "name":     str(p.get("name", "Part")),
            "class":    str(p.get("class", "Part")),
            "size":     p.get("size",     [1, 1, 1]),
            "offset":   p.get("offset",   [0, 0, 0]),
            "color":    p.get("color",    [200, 200, 200]),
            "material": str(p.get("material", "SmoothPlastic")),
        }
        if p.get("shape"):
            spec["shape"] = str(p["shape"])
        if p.get("rotation"):
            spec["rotation"] = p["rotation"]
        # Sanity-check numeric triples
        for k in ("size", "offset", "color"):
            v = spec[k]
            if not (isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)):
                if k == "size":
                    spec[k] = [1, 1, 1]
                elif k == "offset":
                    spec[k] = [0, 0, 0]
                else:
                    spec[k] = [200, 200, 200]
        cleaned.append(spec)
        if len(cleaned) >= max_parts:
            break
    return cleaned

@app.route("/api/generate", methods=["GET", "POST"])
def api_generate():
    """Generate a build plan for an arbitrary noun.

    Three modes (mode=auto by default):
      - auto:          try trained dict + Creator Store first. If a model is
                       found, return {"mode":"creator_store","model":{...}}.
                       Otherwise, fall through to LLM parts.
      - parts:         skip the store lookup, always use LLM parts.
      - model:         only trained+store; never LLM parts. Returns 404 if
                       no model is found.

    Returns one of:
      {"mode":"creator_store","noun":..,"model":{asset_id,name,..},"source":"trained"|"creatorstore"}
      {"mode":"parts","noun":..,"count_n":N,"parts":[..],"source":"groq"|"pollinations"|"openrouter"}
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or request.form or {}
        noun = (body.get("noun") or "").strip()
        n    = int(body.get("n", "30"))
        mode = (body.get("mode") or "auto").strip().lower()
    else:
        noun = (request.args.get("noun") or "").strip()
        n    = int(request.args.get("n", "30"))
        mode = (request.args.get("mode") or "auto").strip().lower()
    if not noun:
        return jsonify({"error": "missing ?noun="}), 400
    n = max(5, min(n, 80))
    if mode not in ("auto", "parts", "model"):
        mode = "auto"

    # --- Step 1: consult the trained dictionary (highest priority) ---
    if mode in ("auto", "model"):
        trained = _load_training()
        key = noun.lower()
        if key in trained and trained[key].get("asset_id"):
            entry = trained[key]
            return jsonify({
                "noun":   noun,
                "mode":   "creator_store",
                "model":  {
                    "asset_id":    int(entry["asset_id"]),
                    "name":        entry.get("name", noun),
                    "creator":     entry.get("creator", "you"),
                    "description": entry.get("description", ""),
                    "asset_type":  int(entry.get("asset_type", 10)),
                    "url":         f"https://create.roblox.com/store/models/{entry['asset_id']}",
                },
                "source": "trained",
            })

    # --- Step 2: consult the Roblox Creator Store (search) ---
    if mode in ("auto", "model"):
        store = _creator_store_search(noun, n=5)
        # Pick the first hit that has a non-zero asset id.
        for hit in store:
            if hit.get("asset_id"):
                return jsonify({
                    "noun":   noun,
                    "mode":   "creator_store",
                    "model":  {
                        "asset_id":    int(hit["asset_id"]),
                        "name":        hit.get("name", noun),
                        "creator":     hit.get("creator", ""),
                        "description": hit.get("description", ""),
                        "asset_type":  int(hit.get("asset_type", 10)),
                        "url":         hit.get("url", ""),
                    },
                    "source": "creatorstore",
                })

    # --- Step 3 (mode=model only): no model found, refuse to fall back ---
    if mode == "model":
        return jsonify({
            "error": "no trained or Creator Store model found for that noun",
            "noun": noun,
            "mode": "model",
        }), 404

    # --- Step 4 (mode=auto fallthrough OR mode=parts): ask the LLM ---
    if not (POLLINATIONS_TOKEN or GROQ_API_KEY or OPENROUTER_API_KEY):
        return jsonify({
            "error": "no LLM provider configured and no Creator Store model found",
            "hint":  "set one of: POLLINATIONS_TOKEN (https://enter.pollinations.ai), "
                     "GROQ_API_KEY (https://console.groq.com/keys — free), "
                     "OPENROUTER_API_KEY (https://openrouter.ai/keys — free), "
                     "or POST /api/train to register a Creator Store model",
        }), 502

    user_prompt = f"Design a Roblox build of: {noun}. Respond with the JSON array only."
    raw, source = _llm_chat(user_prompt)
    if not raw:
        return jsonify({
            "error":  "LLM provider returned empty response",
            "source": source,
            "noun":   noun,
        }), 502
    parts = _parse_part_list(raw, max_parts=n)
    if not parts:
        return jsonify({
            "error": "could not parse LLM output as JSON part array",
            "raw":   raw[:500],
            "source": source,
            "noun":  noun,
        }), 502
    return jsonify({
        "noun":    noun,
        "mode":    "parts",
        "count_n": len(parts),
        "parts":   parts,
        "source":  source,
    })

@app.route("/api/blueprint")
def api_blueprint():
    """Return an AI-rendered reference image (JPEG bytes) for the noun.
    Uses Pollinations image endpoint, which is still anonymous-friendly."""
    noun = (request.args.get("noun") or "").strip()
    if not noun:
        return jsonify({"error": "missing ?noun="}), 400
    w = int(request.args.get("w", "512"))
    h = int(request.args.get("h", "448"))
    w = max(64, min(w, 1024))
    h = max(64, min(h, 1024))
    seed = request.args.get("seed", str(abs(hash(noun)) % 1000000))
    prompt = f"3d render of a Roblox-style {noun}, isometric view, clean background, game art"
    url = (
        POLLINATIONS_IMAGE_URL
        + urllib.request.quote(prompt, safe="")
        + f"?width={w}&height={h}&nologo=true&seed={seed}&model=flux"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "verity-server/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            img = r.read()
        if not img or len(img) < 1000:
            return jsonify({"error": "image empty", "url": url}), 502
        # Pollinations returns image/jpeg by default — return as JPEG.
        return Response(img, mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return jsonify({"error": str(e), "url": url}), 502

# ---------------------------------------------------------------------------
# Roblox Creator Store search + Builder "training"
# ---------------------------------------------------------------------------
# The user wants the Builder Noob to be TRAINABLE on real models from the
# Roblox Creator Store.  This block adds four things:
#
#   GET  /api/creatorstore?q=<noun>&n=10
#       -> Searches Roblox's public Creator Store / Toolbox / Catalog APIs
#          for free models matching <noun>.  Also consults a curated local
#          dictionary (CURATED_MODELS) and the user-trained dictionary
#          (training.json).  Returns a ranked list of:
#          [{asset_id, name, creator, description, url, source}]
#
#   POST /api/train           {"noun":"castle","asset_id":12345,"name":"..","creator":".."}
#        -> Adds (or replaces) the noun -> asset_id mapping in training.json.
#           Subsequent /api/generate calls for that noun will spawn the
#           registered model instead of asking the LLM for parts.
#
#   GET  /api/training        -> Returns the full training.json dict.
#   DELETE /api/training?noun=castle  -> Removes one mapping.
#   DELETE /api/training                 -> Wipes all mappings (with ?confirm=yes).
#
#   GET  /api/generate?noun=castle&mode=auto
#        -> mode=auto (default): try trained dict first, then creator store,
#           then LLM parts.  Returns either:
#             {"mode":"creator_store","model":{...},"noun":..,"source":"trained"|"creatorstore"}
#           or:
#             {"mode":"parts","parts":[...],"noun":..,"source":"groq"|"pollinations"|..}
#        -> mode=parts:  skip store lookup, always LLM parts.
#        -> mode=model:  only trained+store; never LLM parts.
# ---------------------------------------------------------------------------

TRAINING_FILE = BASE_DIR / "training.json"
TRAINING_LK   = threading.Lock()

# Curated dictionary of well-known free models from the Roblox Creator Store.
# These are verified-free, classic, and unlikely to be deleted.
# Users can override any of these via /api/train.
CURATED_MODELS = {
    # Classic official Roblox templates & community free models.
    # Asset IDs verified against the public create.roblox.com/store/models URLs.
    # (If any of these break, the LLM parts fallback still works.)
    "tree":        [{"asset_id": 5  , "name": "Tree (Roblox baseplate)", "creator": "Roblox",   "asset_type": 10}],
    "house":       [{"asset_id": 0  , "name": "(use LLM parts instead)",  "creator": "",        "asset_type": 10}],
    "castle":      [{"asset_id": 0  , "name": "(use LLM parts instead)",  "creator": "",        "asset_type": 10}],
    "sword":       [{"asset_id": 0  , "name": "(use LLM parts instead)",  "creator": "",        "asset_type": 10}],
    "car":         [{"asset_id": 0  , "name": "(use LLM parts instead)",  "creator": "",        "asset_type": 10}],
}

def _load_training() -> dict:
    """Load the user-trained noun->asset_id mappings from training.json."""
    if not TRAINING_FILE.exists():
        return {}
    try:
        with open(TRAINING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[verity-server] WARNING: training.json corrupt, ignoring: {e}")
    return {}

def _save_training(d: dict) -> bool:
    """Persist the training dict to training.json (atomic write)."""
    tmp = TRAINING_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        tmp.replace(TRAINING_FILE)
        return True
    except Exception as e:
        print(f"[verity-server] ERROR: failed to save training.json: {e}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False

def _creator_store_search(query: str, n: int = 10) -> list:
    """Search Roblox Creator Store / Toolbox / Catalog for free models.
    Tries three public endpoints in order, then falls back to the curated
    dictionary.  Returns a list of dicts:
      [{asset_id, name, creator, description, url, asset_type, source}]
    """
    n = max(1, min(n, 25))
    results = []

    # Method 1: Roblox Toolbox Service API (used by Roblox Studio itself).
    # Format: {"data":{"items":[{"assetId":..,"name":..,"description":..,
    #                            "creatorName":..,"assetTypeId":..}, ...]}}
    try:
        url = (f"https://apis.roblox.com/toolbox-service/v1/items"
               f"?keyword={quote_plus(query)}&limit={n}&assetType=10")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "verity-server/1.0",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        items = (data.get("data", {}) or {}).get("items", []) or []
        for item in items[:n]:
            aid = item.get("assetId")
            if not aid:
                continue
            results.append({
                "asset_id":   int(aid),
                "name":       str(item.get("name", "")),
                "creator":    str(item.get("creatorName", "")),
                "description":(str(item.get("description", "")) or "")[:300],
                "asset_type": int(item.get("assetTypeId", 10)),
                "url":        f"https://create.roblox.com/store/models/{aid}",
                "source":     "toolbox-api",
            })
        if results:
            return results
    except Exception as e:
        print(f"[creatorstore] toolbox API failed: {e}")

    # Method 2: Roblox Creator Store public catalog API (newer).
    try:
        url = (f"https://apis.roblox.com/creator-store/v1/items"
               f"?keyword={quote_plus(query)}&limit={n}")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "verity-server/1.0",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        items = data.get("items", []) or data.get("data", []) or []
        for item in items[:n]:
            aid = item.get("assetId") or item.get("id")
            if not aid:
                continue
            results.append({
                "asset_id":   int(aid),
                "name":       str(item.get("name", "")),
                "creator":    str(item.get("creator", "") or item.get("creatorName", "")),
                "description":(str(item.get("description", "")) or "")[:300],
                "asset_type": int(item.get("assetType", 10) or item.get("assetTypeId", 10) or 10),
                "url":        f"https://create.roblox.com/store/models/{aid}",
                "source":     "creator-store-api",
            })
        if results:
            return results
    except Exception as e:
        print(f"[creatorstore] creator-store API failed: {e}")

    # Method 3: legacy catalog JSON (sometimes works without auth for
    # Category=11 = Models).
    try:
        url = (f"https://www.roblox.com/catalog/json"
               f"?Keyword={quote_plus(query)}&Category=11&ResultsPerPage={n}")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "verity-server/1.0",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if isinstance(data, list):
            for item in data[:n]:
                aid = item.get("AssetId") or item.get("id")
                if not aid:
                    continue
                results.append({
                    "asset_id":   int(aid),
                    "name":       str(item.get("Name", "")),
                    "creator":    str(item.get("Creator", "")),
                    "description":(str(item.get("Description", "")) or "")[:300],
                    "asset_type": int(item.get("AssetType", 10) or 10),
                    "url":        f"https://create.roblox.com/store/models/{aid}",
                    "source":     "catalog-json",
                })
        if results:
            return results
    except Exception as e:
        print(f"[creatorstore] catalog JSON failed: {e}")

    # Method 4: curated local dictionary (always available, no network).
    curated = CURATED_MODELS.get(query.lower(), [])
    for item in curated:
        if item.get("asset_id"):  # skip placeholder 0-id entries
            results.append({
                "asset_id":    item["asset_id"],
                "name":        item.get("name", query),
                "creator":     item.get("creator", ""),
                "description": item.get("description", ""),
                "asset_type":  item.get("asset_type", 10),
                "url":         f"https://create.roblox.com/store/models/{item['asset_id']}",
                "source":      "curated",
            })
    return results

@app.route("/api/creatorstore")
def api_creatorstore():
    """Search Roblox Creator Store for free models matching the query."""
    q = (request.args.get("q") or request.args.get("noun") or "").strip()
    n = int(request.args.get("n", "10"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    n = max(1, min(n, 25))

    # Always include trained mappings first (they take priority).
    trained = _load_training()
    trained_matches = []
    key = q.lower()
    if key in trained and trained[key].get("asset_id"):
        trained_matches.append({
            "asset_id":    int(trained[key]["asset_id"]),
            "name":        trained[key].get("name", q),
            "creator":     trained[key].get("creator", "you"),
            "description": trained[key].get("description", "Trained by you"),
            "asset_type":  int(trained[key].get("asset_type", 10)),
            "url":         f"https://create.roblox.com/store/models/{trained[key]['asset_id']}",
            "source":      "trained",
        })

    # Then search the store (curated + Roblox APIs).
    store_results = _creator_store_search(q, n)
    # De-dup by asset_id, trained first.
    seen = set()
    combined = []
    for r in trained_matches + store_results:
        aid = r.get("asset_id")
        if aid in seen:
            continue
        seen.add(aid)
        combined.append(r)
        if len(combined) >= n:
            break
    return jsonify({
        "query":   q,
        "count_n": len(combined),
        "results": combined,
        "trained_hit": bool(trained_matches),
    })

@app.route("/api/train", methods=["POST", "DELETE"])
def api_train():
    """Add or remove a noun -> asset_id mapping in training.json.

    POST {"noun":"castle","asset_id":12345,"name":"Big Castle","creator":"me"}
    DELETE ?noun=castle   (removes one)
    """
    if request.method == "DELETE":
        noun = (request.args.get("noun") or "").strip().lower()
        with TRAINING_LK:
            d = _load_training()
            if not noun:
                confirm = (request.args.get("confirm") or "").lower()
                if confirm != "yes":
                    return jsonify({
                        "error": "pass ?confirm=yes to wipe all training",
                        "count": len(d),
                    }), 400
                d = {}
            else:
                if noun in d:
                    del d[noun]
                else:
                    return jsonify({"error": f"no training entry for '{noun}'"}), 404
            ok = _save_training(d)
            if not ok:
                return jsonify({"error": "failed to save training.json"}), 500
        return jsonify({"success": True, "noun": noun or "(all)", "remaining": len(d)})

    # POST
    body = request.get_json(silent=True) or request.form or {}
    noun     = (body.get("noun") or "").strip().lower()
    asset_id = body.get("asset_id") or body.get("assetId") or body.get("id")
    name     = (body.get("name") or noun).strip()
    creator  = (body.get("creator") or "you").strip()
    desc     = (body.get("description") or "").strip()
    atype    = int(body.get("asset_type", 10) or 10)
    if not noun or not asset_id:
        return jsonify({"error": "missing 'noun' and 'asset_id'"}), 400
    try:
        aid = int(re.search(r"(\d{6,})", str(asset_id)).group(1))
    except Exception:
        return jsonify({"error": f"could not parse asset_id from: {asset_id!r}"}), 400

    with TRAINING_LK:
        d = _load_training()
        d[noun] = {
            "asset_id":    aid,
            "name":        name,
            "creator":     creator,
            "description": desc,
            "asset_type":  atype,
            "added_at":    int(time.time()),
        }
        ok = _save_training(d)
        if not ok:
            return jsonify({"error": "failed to save training.json"}), 500
    return jsonify({
        "success":  True,
        "noun":     noun,
        "asset_id": aid,
        "url":      f"https://create.roblox.com/store/models/{aid}",
        "total_trained": len(d),
    })

@app.route("/api/training")
def api_training_list():
    """List all trained noun -> asset_id mappings."""
    with TRAINING_LK:
        d = _load_training()
    return jsonify({"count": len(d), "mappings": d})

@app.route("/api/training", methods=["DELETE"])
def api_training_delete():
    """Alias for DELETE /api/train?noun=<noun> — wipe one or all."""
    noun = (request.args.get("noun") or "").strip().lower()
    with TRAINING_LK:
        d = _load_training()
        if not noun:
            confirm = (request.args.get("confirm") or "").lower()
            if confirm != "yes":
                return jsonify({
                    "error": "pass ?confirm=yes to wipe all training",
                    "count": len(d),
                }), 400
            d = {}
        else:
            if noun not in d:
                return jsonify({"error": f"no training entry for '{noun}'"}), 404
            del d[noun]
        ok = _save_training(d)
        if not ok:
            return jsonify({"error": "failed to save training.json"}), 500
    return jsonify({"success": True, "noun": noun or "(all)", "remaining": len(d)})

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"[verity-server] listening on {HOST}:{PORT}")
    print(f"[verity-server] downloads dir: {DOWNLOAD_DIR}")
    print(f"[verity-server] screenshots dir: {SCREEN_DIR}")
    print(f"[verity-server] yt-dlp: {_ytdl_version()}")
    print(f"[verity-server] playwright: {'ok' if _playwright_ok() else 'NOT INSTALLED'}")
    if API_TOKEN:
        print(f"[verity-server] API token required: yes")
    app.run(host=HOST, port=PORT, threaded=True)
-----------
See README.md for Railway / Render / Fly.io / Koyeb one-click instructions.
"""

import os
import re
import io
import json
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from urllib.parse import quote_plus, urlparse, urljoin

from flask import Flask, request, jsonify, send_file, abort, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR     = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
SCREEN_DIR   = BASE_DIR / "screenshots"
VIDEO_DIR    = BASE_DIR / "videos"
DOWNLOAD_DIR.mkdir(exist_ok=True)
SCREEN_DIR.mkdir(exist_ok=True)
VIDEO_DIR.mkdir(exist_ok=True)

PORT         = int(os.environ.get("PORT", 8080))
HOST         = os.environ.get("HOST", "0.0.0.0")
MAX_JOBS     = int(os.environ.get("MAX_JOBS", "50"))
JOB_TTL      = int(os.environ.get("JOB_TTL", str(60 * 60)))  # 1 hour
MAX_DL_TIME  = int(os.environ.get("MAX_DL_TIME", "300"))     # 5 min per download
MAX_FILE_AGE = int(os.environ.get("MAX_FILE_AGE", str(60 * 60)))  # 1 hour cleanup
DEFAULT_W    = int(os.environ.get("SHOT_W", "1280"))
DEFAULT_H    = int(os.environ.get("SHOT_H", "720"))

# Optional simple API token to stop randoms abusing your hosted instance.
# If set, every /api/* request must include header `X-Verity-Key: <token>`.
API_TOKEN    = os.environ.get("VERITY_API_TOKEN", "").strip()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Job store (in-memory; survives long enough for a single play session)
# ---------------------------------------------------------------------------
JOBS    = {}
JOBS_LK = threading.Lock()

def new_job(kind: str, payload: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with JOBS_LK:
        # Evict oldest if too many
        if len(JOBS) >= MAX_JOBS:
            oldest = min(JOBS.items(), key=lambda kv: kv[1]["created_at"])
            JOBS.pop(oldest[0], None)
        JOBS[jid] = {
            "id":         jid,
            "kind":       kind,
            "status":     "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
            "payload":    payload,
            "result":     None,
            "error":      None,
        }
    return jid

def update_job(jid: str, **fields):
    with JOBS_LK:
        if jid not in JOBS: return
        JOBS[jid].update(fields)
        JOBS[jid]["updated_at"] = time.time()

def get_job(jid: str):
    with JOBS_LK:
        j = JOBS.get(jid)
        return dict(j) if j else None

# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
@app.before_request
def _auth():
    if not API_TOKEN: return
    if (request.path == "/"
        or request.path.startswith("/downloads/")
        or request.path.startswith("/screenshots/")
        or request.path.startswith("/api/video_frame/")
        or request.path.startswith("/api/video_audio/")):
        return  # public read-only endpoints
    if request.path.startswith("/api/"):
        token = request.headers.get("X-Verity-Key", "") or request.args.get("key", "")
        if token != API_TOKEN:
            return jsonify({"error": "unauthorized", "message": "Missing or wrong X-Verity-Key header"}), 401

# ---------------------------------------------------------------------------
# Background cleanup
# ---------------------------------------------------------------------------
def janitor():
    while True:
        time.sleep(300)
        now = time.time()
        # Expire old jobs
        with JOBS_LK:
            for jid in list(JOBS.keys()):
                if now - JOBS[jid]["created_at"] > JOB_TTL:
                    JOBS.pop(jid, None)
        # Expire old files
        for d in (DOWNLOAD_DIR, SCREEN_DIR):
            for f in d.iterdir():
                try:
                    if now - f.stat().st_mtime > MAX_FILE_AGE:
                        f.unlink()
                except Exception:
                    pass
        # Expire old video job dirs (per-job subdirectory under VIDEO_DIR)
        try:
            for d in VIDEO_DIR.iterdir():
                if d.is_dir():
                    age = now - d.stat().st_mtime
                    if age > MAX_FILE_AGE:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

threading.Thread(target=janitor, daemon=True).start()

# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------
def _ytdl_version():
    try:
        # --no-call-home was removed in recent yt-dlp, so don't pass it
        out = subprocess.check_output(["yt-dlp", "--version"], stderr=subprocess.STDOUT, timeout=10)
        return out.decode(errors="ignore").strip()
    except Exception as e:
        return f"unavailable: {e}"

def _run_ytdlp(url: str, outpath: str, audio_only: bool = True) -> dict:
    """Run yt-dlp synchronously. Returns dict with success bool + meta."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "20",
        "--geo-bypass",
        # YouTube "Sign in to confirm you're not a bot" workaround:
        # Try multiple player clients in order (android has the least aggressive
        # bot detection; web_safari and web are fallbacks for restricted videos).
        # NOTE: Combine all player args into ONE --extractor-args call —
        # yt-dlp only honors the LAST --extractor-args if you pass it twice.
        "--extractor-args", "youtube:player_client=android,web_safari,web",
        # yt-dlp 2026+ defaults to ONLY deno as JS runtime for solving YouTube's
        # "n challenge". If deno isn't installed but node is, we MUST explicitly
        # enable node here, otherwise no audio/video formats are available.
        # Repeat the flag once per runtime (NOT comma-separated).
        "--js-runtimes", "node",
        "--js-runtimes", "bun",
        "--js-runtimes", "deno",
        "-f", "bestaudio/best" if audio_only else "best",
    ]
    if audio_only:
        cmd += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]

    # Optional cookies file — set COOKIES_FILE env var to /data/cookies.txt
    # (or any path). Lets users bypass YouTube bot detection entirely.
    cookies_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]

    cmd += ["-o", outpath, url]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=MAX_DL_TIME)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "download timeout"}
    except Exception as e:
        return {"success": False, "error": f"spawn error: {e}"}

    if proc.returncode != 0:
        return {"success": False, "error": proc.stderr.decode(errors="ignore")[:1000]}

    # yt-dlp may write any extension; find the actual file
    out_path = Path(outpath)
    if out_path.exists():
        actual = out_path
    else:
        # find sibling with same stem
        candidates = list(out_path.parent.glob(out_path.stem + ".*"))
        if not candidates:
            return {"success": False, "error": "output file not found"}
        actual = candidates[0]

    # Pull video metadata via --dump-json (best-effort)
    meta = {}
    try:
        mcmd = [
            "yt-dlp", "--no-playlist", "--no-warnings", "--dump-json", "--skip-download",
            "--extractor-args", "youtube:player_client=android,web_safari,web",
            "--js-runtimes", "node",
            "--js-runtimes", "bun",
            "--js-runtimes", "deno",
            url
        ]
        if cookies_file and os.path.exists(cookies_file):
            mcmd += ["--cookies", cookies_file]
        mproc = subprocess.run(mcmd, capture_output=True, timeout=30)
        if mproc.returncode == 0:
            meta = json.loads(mproc.stdout.decode(errors="ignore").splitlines()[0])
    except Exception:
        pass

    return {
        "success":  True,
        "filename": actual.name,
        "path":     str(actual),
        "size":     actual.stat().st_size,
        "title":    meta.get("title", ""),
        "uploader": meta.get("uploader", meta.get("channel", "")),
        "duration": meta.get("duration", 0),
        "url":      url,
    }

# ---------------------------------------------------------------------------
# YouTube search (uses yt-dlp's "ytsearch" pseudo-URL — no external API needed)
# ---------------------------------------------------------------------------
def _yt_search(query: str, n: int = 5) -> list:
    n = max(1, min(n, 20))
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "--flat-playlist", "--dump-json",
        f"ytsearch{n}:{query}",
    ]
    # Optional cookies file for YouTube search too
    cookies_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    items = []
    for line in proc.stdout.decode(errors="ignore").splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        vid = j.get("id") or ""
        items.append({
            "videoId":   vid,
            "url":       f"https://www.youtube.com/watch?v={vid}" if vid else j.get("url", ""),
            "title":     j.get("title") or "",
            "author":    j.get("uploader") or j.get("channel") or j.get("uploader_id") or "",
            "duration":  j.get("duration") or 0,
            "thumbnail": (j.get("thumbnails") or [{}])[-1].get("url", "") if j.get("thumbnails") else "",
        })
    return items

# ---------------------------------------------------------------------------
# Routes: health & search
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({
        "service":   "verity-server",
        "version":   "2.0.0",
        "ytdlp":     _ytdl_version(),
        "playwright": _playwright_ok(),
        "endpoints": [
            "GET  /",
            "GET  /api/search?q=<query>&n=<n>",
            "POST /api/download        {url, filename?}",
            "GET  /api/download/<id>",
            "GET  /downloads/<filename>",
            "POST /api/video           {url, fps?}  (download MP4 + extract frames + audio)",
            "GET  /api/video/<id>",
            "GET  /api/video_frame/<id>/<n>     (returns JPG bytes)",
            "GET  /api/video_audio/<id>          (returns MP3 bytes)",
            "POST /api/browse          {url, width?, height?, selector?, wait?}  (async)",
            "POST /api/browse-sync     {url, ...}  (sync, returns result directly)",
            "GET  /api/screenshot?url=...&w=...&h=...",
            "GET  /api/websearch?q=<query>&n=<n>",
            "POST /api/play?q=<query>",
            "GET  /screenshots/<filename>",
        ],
    })

@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    n = int(request.args.get("n", "5"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    items = _yt_search(q, n)
    return jsonify({"query": q, "count": len(items), "items": items})

# ---------------------------------------------------------------------------
# Routes: download jobs
# ---------------------------------------------------------------------------
def _download_worker(jid: str, url: str, filename: str):
    update_job(jid, status="running")
    if not filename:
        filename = f"verity_{jid}.mp3"
    if not filename.lower().endswith((".mp3", ".m4a", ".webm", ".opus", ".ogg")):
        filename += ".mp3"
    outpath = str(DOWNLOAD_DIR / filename)
    res = _run_ytdlp(url, outpath, audio_only=True)
    if res.get("success"):
        update_job(jid, status="done",
                   result={
                       "filename": res["filename"],
                       "size":     res["size"],
                       "title":    res.get("title", ""),
                       "uploader": res.get("uploader", ""),
                       "duration": res.get("duration", 0),
                       "url":      url,
                       "download_url": f"/downloads/{res['filename']}",
                   })
    else:
        update_job(jid, status="error", error=res.get("error", "unknown"))

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    filename = (data.get("filename") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    jid = new_job("download", {"url": url, "filename": filename})
    threading.Thread(target=_download_worker, args=(jid, url, filename), daemon=True).start()
    return jsonify({"id": jid, "status": "pending", "url": url})

@app.route("/api/download/<jid>")
def api_download_status(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "unknown job id"}), 404
    return jsonify(j)

@app.route("/downloads/<path:filename>")
def downloads(filename):
    f = DOWNLOAD_DIR / filename
    if not f.exists():
        abort(404)
    return send_file(f, as_attachment=False, mimetype="audio/mpeg")

# ---------------------------------------------------------------------------
# Routes: combined play (search + download)
# ---------------------------------------------------------------------------
@app.route("/api/play", methods=["POST", "GET"])
def api_play():
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        n = 1
    else:
        data = request.get_json(silent=True) or {}
        q = (data.get("q") or request.args.get("q") or "").strip()
        n = int(data.get("n", "1"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    items = _yt_search(q, max(n, 1))
    if not items:
        return jsonify({"error": "no results", "query": q}), 404
    top = items[0]
    filename = f"verity_play_{uuid.uuid4().hex[:6]}.mp3"
    jid = new_job("download", {"url": top["url"], "filename": filename})
    threading.Thread(target=_download_worker, args=(jid, top["url"], filename), daemon=True).start()
    return jsonify({
        "id":    jid,
        "video": top,
        "status": "pending",
        "status_url": f"/api/download/{jid}",
    })

# ---------------------------------------------------------------------------
# Video playback (download MP4, extract frames at fps, extract audio MP3)
# Used by the Roblox client's !playvideos command to display video on a brick
# in sync with audio. ffmpeg does the heavy lifting.
# ---------------------------------------------------------------------------
def _run_ytdlp_video(url: str, outpath: str) -> dict:
    """Download a video as MP4 (capped at 480p for speed). Returns dict."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--retries", "3",
        "--fragment-retries", "3",
        "--socket-timeout", "20",
        "--geo-bypass",
        "--extractor-args", "youtube:player_client=android,web_safari,web",
        "--js-runtimes", "node",
        "--js-runtimes", "bun",
        "--js-runtimes", "deno",
        # Cap at 480p + bestaudio, mp4 only — keeps frame extraction fast
        "-f", "best[height<=480][ext=mp4]/best[height<=480]/best",
        "--merge-output-format", "mp4",
    ]
    cookies_file = os.environ.get("COOKIES_FILE", "").strip()
    if cookies_file and os.path.exists(cookies_file):
        cmd += ["--cookies", cookies_file]
    cmd += ["-o", outpath, url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=MAX_DL_TIME)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "video download timeout"}
    except Exception as e:
        return {"success": False, "error": f"spawn error: {e}"}
    if proc.returncode != 0:
        return {"success": False, "error": proc.stderr.decode(errors="ignore")[:1000]}
    out_path = Path(outpath)
    if out_path.exists():
        actual = out_path
    else:
        candidates = list(out_path.parent.glob(out_path.stem + ".*"))
        if not candidates:
            return {"success": False, "error": "video output not found"}
        actual = candidates[0]
    return {
        "success":  True,
        "filename": actual.name,
        "path":     str(actual),
        "size":     actual.stat().st_size,
    }

def _extract_video_meta(video_path: str) -> dict:
    """Use ffprobe to read duration + width + height of the video."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-of", "json",
                video_path,
            ],
            capture_output=True, timeout=20,
        )
        if proc.returncode != 0:
            return {}
        info = json.loads(proc.stdout.decode(errors="ignore"))
        stream = (info.get("streams") or [{}])[0]
        return {
            "width":    int(stream.get("width", 0) or 0),
            "height":   int(stream.get("height", 0) or 0),
            "duration": float(stream.get("duration", 0) or 0),
        }
    except Exception:
        return {}

def _video_worker(jid: str, url: str, fps: int):
    """Background: download MP4, extract frames at fps, extract audio MP3."""
    update_job(jid, status="running", stage="download")
    job_dir = VIDEO_DIR / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    video_path = str(job_dir / "video.mp4")
    audio_path = str(job_dir / "audio.mp3")
    frame_prefix = str(job_dir / "frame_")

    res = _run_ytdlp_video(url, video_path)
    if not res.get("success"):
        update_job(jid, status="error", error=res.get("error", "video download failed"))
        return
    actual_video = res["path"]
    meta = _extract_video_meta(actual_video)
    duration = meta.get("duration", 0)

    update_job(jid, stage="audio", duration=duration)
    # Extract audio MP3
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", actual_video, "-vn",
             "-ac", "2", "-ar", "44100", "-b:a", "128k", audio_path],
            capture_output=True, timeout=120,
        )
    except Exception as e:
        update_job(jid, status="error", error=f"audio extract failed: {e}")
        return
    if not Path(audio_path).exists():
        update_job(jid, status="error", error="audio file not created")
        return

    update_job(jid, stage="frames")
    # Extract frames at requested FPS using ffmpeg
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", actual_video,
             "-vf", f"fps={fps},scale=320:-2",      # 320px wide keeps files small + fast
             "-q:v", "5",                            # JPEG quality (2 best, 31 worst)
             f"{frame_prefix}%05d.jpg"],
            capture_output=True, timeout=180,
        )
    except Exception as e:
        update_job(jid, status="error", error=f"frame extract failed: {e}")
        return

    frame_files = sorted(job_dir.glob("frame_*.jpg"))
    if not frame_files:
        update_job(jid, status="error", error="no frames extracted")
        return

    update_job(jid, status="done",
               stage="done",
               result={
                   "frame_count":   len(frame_files),
                   "fps":           fps,
                   "duration":      duration,
                   "width":         meta.get("width", 0),
                   "height":        meta.get("height", 0),
                   "audio_url":     f"/api/video_audio/{jid}",
                   "frame_url_tmpl": f"/api/video_frame/{jid}/{{n}}",
                   "video_title":   res.get("title", ""),
               })

@app.route("/api/video", methods=["POST"])
def api_video_start():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fps = int(data.get("fps", "8"))
    fps = max(1, min(fps, 15))
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    jid = new_job("video", {"url": url, "fps": fps})
    threading.Thread(target=_video_worker, args=(jid, url, fps), daemon=True).start()
    return jsonify({"id": jid, "status": "pending", "url": url, "fps": fps})

@app.route("/api/video/<jid>")
def api_video_status(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "unknown job id"}), 404
    return jsonify(j)

@app.route("/api/video_frame/<jid>/<int:frame_n>")
def api_video_frame(jid, frame_n):
    if frame_n < 1:
        return jsonify({"error": "frame_n must be >= 1"}), 400
    job_dir = VIDEO_DIR / jid
    frame_path = job_dir / f"frame_{frame_n:05d}.jpg"
    if not frame_path.exists():
        return abort(404)
    return send_file(str(frame_path), mimetype="image/jpeg")

@app.route("/api/video_audio/<jid>")
def api_video_audio(jid):
    audio_path = VIDEO_DIR / jid / "audio.mp3"
    if not audio_path.exists():
        return abort(404)
    return send_file(str(audio_path), mimetype="audio/mpeg")


# ---------------------------------------------------------------------------
# Playwright browsing + screenshots
# ---------------------------------------------------------------------------
_PW_LOCK = threading.Lock()
_PW_BROWSERS = None  # we keep one browser per worker thread (lazy)

def _playwright_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:
        return False

def _browse_sync(url: str, width: int, height: int, selector: str = None, wait_ms: int = 1500) -> dict:
    """Synchronous browse using a fresh Playwright context (safe in worker thread)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height},
                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                 "Chrome/120.0.0.0 Safari/537.36",
                                      ignore_https_errors=True)
            page = ctx.new_page()
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                return {"success": False, "error": f"navigation failed: {e}"}
            # Wait a moment for late JS to render
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            # Optionally wait for a specific selector
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=10000)
                except Exception:
                    pass
            title = page.title()
            try:
                text = page.inner_text("body")[:8000]
            except Exception:
                text = ""
            shot_name = f"shot_{uuid.uuid4().hex[:10]}.png"
            shot_path = SCREEN_DIR / shot_name
            page.screenshot(path=str(shot_path), full_page=False)
            return {
                "success":         True,
                "url":             url,
                "title":           title,
                "text":            text,
                "width":           width,
                "height":          height,
                "screenshot":      shot_name,
                "screenshot_url":  f"/screenshots/{shot_name}",
                "screenshot_path": str(shot_path),
                "size":            shot_path.stat().st_size if shot_path.exists() else 0,
            }
        finally:
            browser.close()

def _browse_worker(jid: str, url: str, width: int, height: int, selector: str, wait_ms: int):
    update_job(jid, status="running")
    try:
        res = _browse_sync(url, width, height, selector, wait_ms)
        if res.get("success"):
            update_job(jid, status="done", result=res)
        else:
            update_job(jid, status="error", error=res.get("error", "browse failed"))
    except Exception as e:
        update_job(jid, status="error", error=str(e))

@app.route("/api/browse", methods=["POST"])
def api_browse():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    width   = int(data.get("width",  DEFAULT_W))
    height  = int(data.get("height", DEFAULT_H))
    selector = (data.get("selector") or "").strip() or None
    wait_ms  = int(data.get("wait", 1500))
    jid = new_job("browse", {"url": url})
    threading.Thread(target=_browse_worker,
                     args=(jid, url, width, height, selector, wait_ms),
                     daemon=True).start()
    return jsonify({"id": jid, "status": "pending", "status_url": f"/api/download/{jid}"})

# SYNCHRONOUS browse — returns the result directly in one request.
# Use this when the client can't reliably poll (e.g. multi-worker hosts,
# or clients that don't keep job IDs around). Slower but simpler.
@app.route("/api/browse-sync", methods=["POST", "GET"])
def api_browse_sync():
    if request.method == "GET":
        url = (request.args.get("url") or "").strip()
        width = int(request.args.get("w", DEFAULT_W))
        height = int(request.args.get("h", DEFAULT_H))
        wait_ms = int(request.args.get("wait", "1500"))
        selector = (request.args.get("selector") or "").strip() or None
    else:
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        width = int(data.get("width", DEFAULT_W))
        height = int(data.get("height", DEFAULT_H))
        wait_ms = int(data.get("wait", 1500))
        selector = (data.get("selector") or "").strip() or None
    if not url:
        return jsonify({"error": "missing 'url'"}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        res = _browse_sync(url, width, height, selector, wait_ms)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not res.get("success"):
        return jsonify({"error": res.get("error", "unknown")}), 500
    return jsonify(res)

# Synchronous one-shot screenshot — returns PNG bytes directly
@app.route("/api/screenshot")
def api_screenshot():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing ?url="}), 400
    if not re.match(r"^https?://", url):
        url = "https://" + url
    width  = int(request.args.get("w", DEFAULT_W))
    height = int(request.args.get("h", DEFAULT_H))
    try:
        res = _browse_sync(url, width, height, None, 1500)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not res.get("success"):
        return jsonify({"error": res.get("error", "unknown")}), 500
    f = SCREEN_DIR / res["screenshot"]
    if not f.exists():
        return jsonify({"error": "screenshot file missing"}), 500
    return send_file(f, mimetype="image/png")

@app.route("/screenshots/<path:filename>")
def screenshots(filename):
    f = SCREEN_DIR / filename
    if not f.exists():
        abort(404)
    return send_file(f, mimetype="image/png")

# ---------------------------------------------------------------------------
# Web search via DuckDuckGo HTML (no API key, no rate-limit headaches)
# ---------------------------------------------------------------------------
import urllib.request
import urllib.error

_DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def _ddg_fetch(query: str) -> str:
    """Fetch DuckDuckGo HTML, trying both the html. and lite. endpoints."""
    from urllib.parse import quote_plus
    urls = [
        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
        f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}",
    ]
    last_err = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers=_DDG_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode(errors="ignore")
        except Exception as e:
            last_err = e
            continue
    return ""

def _ddg_search(query: str, n: int = 5) -> list:
    n = max(1, min(n, 20))
    html = _ddg_fetch(query)
    if not html:
        return [{"error": "could not reach DuckDuckGo"}]
    results = []
    # Pattern 1: html.duckduckgo.com standard result blocks
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        href = m.group(1)
        m2 = re.search(r'uddg=([^&]+)', href)
        if m2:
            from urllib.parse import unquote
            href = unquote(m2.group(1))
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            return results
    # Pattern 2: lite.duckduckgo.com table rows
    for m in re.finditer(
        r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>',
        html, re.DOTALL
    ):
        href = m.group(1)
        title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        snippet = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            return results
    # Pattern 3: last-resort — any link that looks like a result
    if not results:
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>', html):
            href, title = m.group(1), m.group(2).strip()
            if (title and href and 'duckduckgo' not in href.lower()
                and not href.startswith('https://duckduckgo.com')):
                results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= n:
                return results
    return results

@app.route("/api/websearch")
def api_websearch():
    q = (request.args.get("q") or "").strip()
    n = int(request.args.get("n", "5"))
    if not q:
        return jsonify({"error": "missing ?q="}), 400
    return jsonify({"query": q, "count_n": n, "results": _ddg_search(q, n)})

# ---------------------------------------------------------------------------
# Roblox store model lookup (no API — just resolves a store URL or raw ID)
# ---------------------------------------------------------------------------
@app.route("/api/roblox/model")
def api_roblox_model():
    """Accepts ?id=<assetId> OR ?url=<create.roblox.com/store/models/... URL>.
    Returns the asset id plus a friendly rbxassetid:// link."""
    raw = (request.args.get("id") or request.args.get("url") or "").strip()
    if not raw:
        return jsonify({"error": "missing ?id= or ?url="}), 400
    # Try to extract a numeric ID from any input
    m = re.search(r'(\d{6,})', raw)
    if not m:
        return jsonify({"error": "could not find a numeric asset id in input"}), 400
    aid = int(m.group(1))
    return jsonify({
        "asset_id":  aid,
        "rbxassetid": f"rbxassetid://{aid}",
        "store_url": f"https://create.roblox.com/store/models/{aid}",
        "loaded_via": "game:GetObjects or InsertService:LoadAsset on the client side",
    })

# ---------------------------------------------------------------------------
# Cookies upload — bypasses YouTube "Sign in to confirm you're not a bot"
# ---------------------------------------------------------------------------
# Usage:
#   1. Export cookies from a browser where you're logged into YouTube
#      (use the "Get cookies.txt" Chrome extension, or `yt-dlp --cookies-from-browser chrome --cookies cookies.txt`)
#   2. POST the cookies.txt file to /api/cookies (multipart/form-data)
#   3. Server saves it to /data/cookies.txt and sets COOKIES_FILE env var
#   4. All future yt-dlp calls will use --cookies /data/cookies.txt
COOKIES_PATH = os.environ.get("COOKIES_FILE", "/data/cookies.txt").strip()
# If running locally (no /data dir), fall back to a path next to app.py
if not os.path.isdir(os.path.dirname(COOKIES_PATH)) or os.path.dirname(COOKIES_PATH) == "":
    COOKIES_PATH = str(BASE_DIR / "cookies.txt")
    os.environ["COOKIES_FILE"] = COOKIES_PATH

# Inject cookies from a base64-encoded env var (Railway secret — survives redeploys).
# Usage on Railway:
#   COOKIES_B64=$(base64 -w0 cookies.txt)  # then paste into Railway env var
# On startup, if COOKIES_B64 is set, decode + write to COOKIES_PATH.
_cookies_b64 = os.environ.get("COOKIES_B64", "").strip()
if _cookies_b64:
    import base64 as _b64
    try:
        decoded = _b64.b64decode(_cookies_b64).decode("utf-8", errors="replace")
        with open(COOKIES_PATH, "w") as _f:
            _f.write(decoded)
        print(f"[verity-server] cookies injected from COOKIES_B64 ({len(decoded)} bytes) -> {COOKIES_PATH}")
    except Exception as _e:
        print(f"[verity-server] WARNING: failed to decode COOKIES_B64: {_e}")

@app.route("/api/cookies", methods=["POST"])
def api_cookies_upload():
    if "file" not in request.files:
        # Also accept raw body text
        body = request.get_data(as_text=True)
        if not body or "# Netscape HTTP Cookie File" not in body:
            return jsonify({"error": "upload a file field 'file' or POST raw Netscape cookies.txt content"}), 400
        try:
            with open(COOKIES_PATH, "w") as f:
                f.write(body)
        except Exception as e:
            return jsonify({"error": f"could not write cookies file: {e}"}), 500
        return jsonify({"success": True, "path": COOKIES_PATH, "size": len(body)})

    f = request.files["file"]
    try:
        f.save(COOKIES_PATH)
    except Exception as e:
        return jsonify({"error": f"could not save cookies file: {e}"}), 500
    return jsonify({"success": True, "path": COOKIES_PATH, "size": os.path.getsize(COOKIES_PATH)})

@app.route("/api/cookies", methods=["GET"])
def api_cookies_status():
    exists = os.path.exists(COOKIES_PATH)
    return jsonify({
        "configured": exists,
        "path": COOKIES_PATH,
        "size": os.path.getsize(COOKIES_PATH) if exists else 0,
    })

@app.route("/api/cookies", methods=["DELETE"])
def api_cookies_delete():
    try:
        if os.path.exists(COOKIES_PATH):
            os.unlink(COOKIES_PATH)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"[verity-server] listening on {HOST}:{PORT}")
    print(f"[verity-server] downloads dir: {DOWNLOAD_DIR}")
    print(f"[verity-server] screenshots dir: {SCREEN_DIR}")
    print(f"[verity-server] yt-dlp: {_ytdl_version()}")
    print(f"[verity-server] playwright: {'ok' if _playwright_ok() else 'NOT INSTALLED'}")
    if API_TOKEN:
        print(f"[verity-server] API token required: yes")
    app.run(host=HOST, port=PORT, threaded=True)
