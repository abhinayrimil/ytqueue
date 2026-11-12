# ytqueue

Small local app that queues video links and downloads them one by one with [yt-dlp](https://github.com/yt-dlp/yt-dlp). Runs as a desktop window, or in your browser.

## Run locally

Needs Python 3 and `ffmpeg` on your PATH.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py              # desktop window
python app.py --browser    # http://127.0.0.1:8765
```

On Linux, the desktop window needs `pip install "pywebview[qt]"`. Without it, the app opens in your browser.

## Run with Docker

```bash
cp .env.example .env       # set PUID/PGID to your `id -u` / `id -g`
mkdir -p data downloads
docker compose up -d --build
```

Then open http://127.0.0.1:8765. Downloads land in `downloads/`.

## Features

- Persistent queue that survives restarts
- Live progress, speed and ETA
- Quality presets: best, 1080p, 720p, 480p, mp3
- Choose a save folder per item
- Disk-space checks before and during each download
- Reorder, cancel, retry, pause

## Config

Optional environment variables: `YTQ_DOWNLOAD_DIR`, `YTQ_PORT` (8765), `YTQ_WORKERS` (1), `YTQ_RESERVE_MB` (500). See the top of `app.py` for the rest.

> No login. It binds to `127.0.0.1` by default, so keep it that way unless you trust your network.

If downloads start failing, update yt-dlp: `pip install -U yt-dlp`.

## License

[MIT](LICENSE)
