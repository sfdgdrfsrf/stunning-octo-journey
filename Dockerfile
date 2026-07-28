# ---- Verity All-in-One Server ----
# Uses Microsoft's official Playwright Python image (ships Chromium + system deps).
# Tag pinned to v1.49+ which is built on Python 3.12 (yt-dlp 2026 dropped 3.10 support).

FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Don't write .pyc files & flush stdout immediately so logs show up in Render/Railway
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /app

# Install OS deps:
#  - ffmpeg: audio extraction for yt-dlp
#  - nodejs: REQUIRED by yt-dlp 2026+ to solve YouTube's "n challenge" (JS)
#            Without it, only storyboard images are available — no audio/video.
#  - curl, ca-certificates: general HTTP hygiene
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python deps (yt-dlp-ejs is the challenge-solver script distribution
# that yt-dlp 2026+ loads at runtime when a JS runtime is available)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install Chromium for Playwright (the base image already has it, but be explicit)
RUN python -m playwright install chromium

# Smoke-test: confirm node + yt-dlp-ejs are usable inside the container.
# This will print "node-<version>" if everything is wired up correctly.
RUN node --version && \
    python -c "import yt_dlp_ejs; print('yt_dlp_ejs OK')"

# App code
COPY app.py .

# Persist downloads + screenshots to /data so volumes can be attached
RUN mkdir -p /data/downloads /data/screenshots
ENV DOWNLOAD_DIR_OVERRIDE=/data/downloads
ENV SCREEN_DIR_OVERRIDE=/data/screenshots
# Optional cookies file path — set COOKIES_B64 in Railway env to inject it
ENV COOKIES_FILE=/data/cookies.txt

EXPOSE 8080

# Gunicorn: 1 worker (in-memory job store must be shared across requests,
# so we can't scale horizontally without external state like Redis).
# Threaded=True inside Flask handles concurrent requests within the worker.
# 180s timeout because yt-dlp downloads can be slow.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--timeout", "180", "app:app"]
