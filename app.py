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

from flask import Flask, request, jsonify, send_file, abort, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR     = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
SCREEN_DIR   = BASE_DIR / "screenshots"
DOWNLOAD_DIR.mkdir(exist_ok=True)
SCREEN_DIR.mkdir(exist_ok=True)

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
    if request.path == "/" or request.path.startswith("/downloads/") or request.path.startswith("/screenshots/"):
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
    app.run(host=HOST, port=PORT, threaded=True)-----------
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
DOWNLOAD_DIR.mkdir(exist_ok=True)
SCREEN_DIR.mkdir(exist_ok=True)

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
    if request.path == "/" or request.path.startswith("/downloads/") or request.path.startswith("/screenshots/"):
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
        # Use the android player client first (less aggressive bot detection),
        # fall back to web_safari, then web. Order matters.
        "--extractor-args", "youtube:player_client=android,web_safari,web",
        # Use the new PO token provider if available
        "--extractor-args", "youtube:player_skip=webpage,configs",
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
