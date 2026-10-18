#!/bin/zsh
# Controlled BTC trader restart for the ETH LIVE EXPERIMENT (Phase 1) — run ONLY after the user's
# explicit approval. The trader stops gracefully (SIGTERM -> state flush) and launchd (KeepAlive)
# starts it again on the new multi-sleeve code; the open BTC position is never touched.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
UIDN="$(id -u)"
API=http://127.0.0.1:8787
step() { printf "\n== %s\n" "$1"; }
fail() { echo "ABORT: $1"; exit 1; }

step "1. persisted position/state before restart"
BEFORE=$(curl -s "$API/api/portfolio" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['btc'], d['avg_entry'], d['stop_price'], d['krw'])")
PID=$(curl -s "$API/api/status" | python3 -c "import json,sys;print(json.load(sys.stdin)['pid'])")
echo "pid $PID  btc/entry/stop/krw: $BEFORE"
curl -s "$API/api/decision" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('risk_status') or {};assert r.get('inflight',0)==0 and r.get('unknown',0)==0, r;print('no inflight/unknown OK')" || fail "in-flight orders"
[[ "$("$ROOT/scripts/launchd_install.sh" mode)" == "LIVE" ]] || fail "rendered trader plist is not LIVE"

step "2. graceful stop + launchd restart (kickstart -k = SIGTERM, then KeepAlive relaunch)"
launchctl kickstart -k "gui/$UIDN/com.quant.trader" || fail "kickstart failed"
for i in $(seq 1 45); do
  sleep 2
  NP=$(curl -s "$API/api/status" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['pid'] if d['online'] else 'None')" 2>/dev/null)
  [[ "$NP" != "None" && "$NP" != "$PID" && -n "$NP" ]] && break
done
[[ "$NP" != "None" && "$NP" != "$PID" ]] || fail "new trader pid not observed"
echo "new pid $NP"
[[ "$(pgrep -fl 'app.main_trader' | grep -vc caffeinate | tr -d ' ')" == "1" ]] || fail "expected exactly one trader process (caffeinate wrapper excluded)"

step "3. wait for RUNNING / ENTRY_AND_EXIT"
for i in $(seq 1 60); do
  sleep 2
  ST=$(curl -s "$API/api/status" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['mode'], d['state'], d.get('execution_mode'))" 2>/dev/null)
  [[ "$ST" == "LIVE RUNNING ENTRY_AND_EXIT" ]] && break
done
echo "status: $ST"
[[ "$ST" == "LIVE RUNNING ENTRY_AND_EXIT" ]] || fail "trader did not reach LIVE RUNNING ENTRY_AND_EXIT"
grep -i "LIVE_BLOCKED" "$ROOT/logs/launchd_trader.err.log" | tail -1 | grep -q "$(date -u +%Y-%m-%d)" && fail "LIVE_BLOCKED today (see logs/launchd_trader.err.log)"

step "4. position / stop after restart"
AFTER=$(curl -s "$API/api/portfolio" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['btc'], d['avg_entry'], d['stop_price'], d['krw'])")
echo "btc/entry/stop/krw: $AFTER"
python3 - "$BEFORE" "$AFTER" <<'PY' || fail "position/stop changed unexpectedly"
import sys
b = sys.argv[1].split(); a = sys.argv[2].split()
assert b[0] == a[0], ("btc changed", b[0], a[0])
assert b[1] == a[1], ("entry changed", b[1], a[1])
assert float(a[2]) >= float(b[2]), ("stop lowered", b[2], a[2])
print("BTC quantity / entry unchanged; stop not lowered; D0 book cash", b[3], "->", a[3], "(ETH sleeve carve-out expected: -50000)")
PY
echo "controlled restart complete"
