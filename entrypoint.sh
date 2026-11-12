#!/bin/sh
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA="${YTQ_DATA_DIR:-/data}"
ME="$(id -u):$(id -g)"

if [ ! -w "$DATA" ]; then
  echo "ERROR: $DATA is not writable by $ME."
  echo "       On the host, run: sudo chown -R $ME <the folder mounted at $DATA>"
  exit 1
fi
mkdir -p "$DATA/cache"

for d in "${YTQ_DOWNLOAD_DIR:-/downloads}" $(echo "${YTQ_SHORTCUTS:-}" | tr ',' ' '); do
  if [ -d "$d" ] && [ ! -w "$d" ]; then
    echo "WARNING: $d is not writable by $ME — downloads there will fail."
    echo "         On the host, run: sudo chown -R $ME <the folder mounted at $d>"
  fi
done

# Sites change often, so optionally pull the newest yt-dlp at start-up.
# It is installed into the data volume, so it persists and needs no root.
if [ "${YTQ_AUTO_UPDATE:-0}" = "1" ]; then
  echo "Checking for yt-dlp updates..."
  if pip install -q --upgrade --target "$DATA/pylib" "yt-dlp[default]" >"$DATA/pip-update.log" 2>&1; then
    echo "yt-dlp is up to date."
  else
    echo "yt-dlp update failed (see $DATA/pip-update.log); using the bundled version."
  fi
fi
if [ -d "$DATA/pylib/yt_dlp" ]; then
  export PYTHONPATH="$DATA/pylib${PYTHONPATH:+:$PYTHONPATH}"
fi

echo "yt-dlp $(python -c 'import yt_dlp.version as v; print(v.__version__)')"
exec python "$APP_DIR/app.py" "$@"
