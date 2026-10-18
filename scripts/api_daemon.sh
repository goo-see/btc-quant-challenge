#!/bin/zsh
# Dashboard API (FastAPI, loopback only). Reach it via `tailscale serve`.
# usage: scripts/api_daemon.sh start|stop|status|log
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
PIDFILE="$ROOT/logs/api_daemon.pid"
LOG="$ROOT/logs/api_daemon.log"
mkdir -p "$ROOT/logs"
is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
case "${1:-status}" in
  start)
    if is_running; then echo "already running pid=$(cat "$PIDFILE")"; exit 0; fi
    cd "$BACKEND" || exit 1
    nohup "$BACKEND/.venv/bin/python" -m app.main_api >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"; sleep 2
    if is_running; then echo "started pid=$(cat "$PIDFILE") log=$LOG"; else echo "failed; see $LOG"; exit 1; fi ;;
  stop) if is_running; then kill -TERM "$(cat "$PIDFILE")"; sleep 2; rm -f "$PIDFILE"; echo stopped; else echo "not running"; fi ;;
  status) if is_running; then echo "running pid=$(cat "$PIDFILE")"; else echo "not running"; exit 1; fi ;;
  log) tail -n "${2:-40}" "$LOG" ;;
  *) echo "usage: $0 start|stop|status|log"; exit 2 ;;
esac
