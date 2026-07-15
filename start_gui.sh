#!/bin/sh
# Launcher for the ScreenTime GUI (the reader window). This does NOT track
# time — the background daemon (started via autostart / start_screentime.sh)
# does that. This window just displays the data and lets you change settings,
# so it needs no Wayland wait.
set -e

PROJECT_DIR="/home/gabri/screentime"

export PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}"

exec /usr/bin/python3 "$PROJECT_DIR/screentime_cosmic.py"
