# ---- Verity All-in-One Server ----
# Stays small (Alpine Chromium refuses to run Playwright reliably),
# so we use the official Playwright Python image which already ships
# every browser + system dependency.

FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

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
