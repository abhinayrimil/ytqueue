FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: merging video+audio and mp3 conversion
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Deno: the JavaScript runtime yt-dlp uses to solve YouTube's player challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements-docker.txt .
RUN pip install -r requirements-docker.txt

COPY app.py index.html entrypoint.sh ./
RUN chmod 755 entrypoint.sh \
 && mkdir -p /data /downloads \
 && chmod 777 /data /downloads

ENV YTQ_HEADLESS=1 \
    YTQ_HOST=0.0.0.0 \
    YTQ_PORT=8765 \
    YTQ_DB_PATH=/data/queue.db \
    YTQ_DOWNLOAD_DIR=/downloads \
    YTQ_SHORTCUTS=/downloads \
    HOME=/data \
    XDG_CACHE_HOME=/data/cache

# Runs as a non-root user; docker-compose overrides this with your own UID/GID
USER 1000:1000
EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/settings', timeout=4)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
