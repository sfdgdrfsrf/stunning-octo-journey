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

# Install OS deps yt-dlp sometimes needs (ca-certificates, ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install Chromium for Playwright (the base image already has it, but be explicit)
RUN python -m playwright install chromium

# App code
COPY app.py .

# Persist downloads + screenshots to /data so volumes can be attached
RUN mkdir -p /data/downloads /data/screenshots
ENV DOWNLOAD_DIR_OVERRIDE=/data/downloads
ENV SCREEN_DIR_OVERRIDE=/data/screenshots

EXPOSE 8080

# Gunicorn: 1 worker (in-memory job store must be shared across requests,
# so we can't scale horizontally without external state like Redis).
# Threaded=True inside Flask handles concurrent requests within the worker.
# 180s timeout because yt-dlp downloads can be slow.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--timeout", "180", "app:app"]
