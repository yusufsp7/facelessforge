#!/bin/bash
# FacelessForge launcher (Linux/VM). Instance guard + persistent data via FORGE_DATA default.
set -e
cd "$(dirname "$0")"
PIDFILE=data/.server.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "sudah jalan (PID $(cat "$PIDFILE")) — stop dulu dengan ./stop.sh"; exit 1
fi
[ -d .venv ] || { python3 -m venv .venv; .venv/bin/pip install -q -r requirements.txt; }
mkdir -p data
: "${FORGE_HEADLESS:=1}"; : "${FORGE_PORT:=8790}"; export FORGE_HEADLESS FORGE_PORT
nohup .venv/bin/python run.py >> data/server.log 2>&1 &
echo $! > "$PIDFILE"
echo "FacelessForge jalan (PID $(cat "$PIDFILE"), port $FORGE_PORT) — log: data/server.log"
