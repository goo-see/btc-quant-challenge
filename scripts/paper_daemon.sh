#!/bin/zsh
# Persistent PAPER trader (LIVE-fidelity sizing). Detached from the invoking shell, sleep-proof.
# usage: scripts/paper_daemon.sh start|stop|status|log
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
PIDFILE="$ROOT/logs/paper_daemon.pid"
LOG="$ROOT/logs/paper_daemon.log"
mkdir -p "$ROOT/logs"

is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-status}" in
  start)
    if is_running; then echo "already running pid=$(cat "$PIDFILE")"; exit 0; fi
    cd "$BACKEND" || exit 1
    # caffeinate -i: prevent idle sleep while the trader runs. nohup+setsid: survive shell exit.
    TRADING_MODE=PAPER nohup caffeinate -i "$BACKEND/.venv/bin/python" -m app.main_trader >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if is_running; then echo "started pid=$(cat "$PIDFILE") log=$LOG"; else echo "failed to start; see $LOG"; exit 1; fi
    ;;
  stop)
    if is_running; then kill -TERM "$(cat "$PIDFILE")"; sleep 3; is_running && kill -KILL "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo stopped; else echo "not running"; fi
    ;;
  status)
    if is_running; then echo "running pid=$(cat "$PIDFILE")"; else echo "not running"; exit 1; fi
    ;;
  log) tail -n "${2:-40}" "$LOG" ;;
  *) echo "usage: $0 start|stop|status|log [n]"; exit 2 ;;
esac
