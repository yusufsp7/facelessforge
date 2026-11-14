#!/bin/bash
cd "$(dirname "$0")"
PIDFILE=data/.server.pid
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE"); kill "$PID" 2>/dev/null && echo "stop PID $PID" || echo "PID $PID tidak hidup"
  rm -f "$PIDFILE"
else echo "pidfile tidak ada — coba: pkill -f 'python run.py'"; fi
