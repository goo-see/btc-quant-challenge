#!/bin/zsh
# D0 LIVE transition — run ONLY after the user typed LIVE_APPROVE_D0 (never automatically).
# Order: gates -> flat PAPER check -> stop PAPER trader -> single LIVE trader via launchd -> verify.
# It never places an order itself; the LIVE trader waits for the next natural D0 signal.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
UIDN="$(id -u)"
API=http://127.0.0.1:8787
step() { printf "\n== %s\n" "$1"; }
fail() { echo "ABORT: $1"; exit 1; }

step "1. static gates (config + .env)"
grep -Eq "trend_following_4h_v1:.*live_approved: true" "$ROOT/config/config.yaml" || fail "live_approved is not true in config.yaml"
grep -Eq "^\s+enabled: true" "$ROOT/config/config.yaml" || fail "live_gate.enabled is not true"
grep -Eq "^TRADING_MODE=LIVE" "$ROOT/.env" || fail ".env TRADING_MODE is not LIVE"
grep -Eq "^LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_KRW" "$ROOT/.env" || fail ".env LIVE_CONFIRM phrase missing"
echo "gates present"

step "2. PAPER trader must be flat with no in-flight orders"
curl -s "$API/api/portfolio" | python3 -c "import json,sys;d=json.load(sys.stdin);assert d['btc']==0 and not d['has_position'], d;print('paper position 0 OK')" || fail "paper position not flat"
curl -s "$API/api/decision" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('risk_status') or {};assert r.get('inflight',0)==0 and r.get('unknown',0)==0, r;print('no inflight/unknown OK')" || fail "in-flight orders"

step "3. LIVE preflight (read-only + orders/test dry run)"
(cd "$BACKEND" && uv run python ../scripts/private_smoke.py 2>/dev/null | tail -8 | tee /tmp/live_preflight.txt)
grep -q "result: OK" /tmp/live_preflight.txt || fail "preflight not OK"

step "4. stop the PAPER trader (single trader process rule)"
launchctl bootout "gui/$UIDN/com.quant.trader" 2>/dev/null || true
for i in $(seq 1 20); do sleep 1; pgrep -f "app.main_trader" >/dev/null || break; done
pgrep -f "app.main_trader" >/dev/null && fail "a trader process is still running"
echo "no trader process running"

step "5. start the single LIVE trader"
TRADING_MODE=LIVE "$ROOT/scripts/launchd_install.sh" install com.quant.trader || fail "launchd install failed"
sleep 15
"$ROOT/scripts/launchd_install.sh" status
[[ "$("$ROOT/scripts/launchd_install.sh" mode)" == "LIVE" ]] || fail "rendered plist is not LIVE"
[[ "$(pgrep -f 'app.main_trader' | wc -l | tr -d ' ')" == "1" ]] || fail "expected exactly one trader process"

step "6. verify LIVE via API"
for i in $(seq 1 30); do sleep 2; M=$(curl -s "$API/api/status" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['mode'], d['state'], d['online'])" 2>/dev/null); [[ "$M" == LIVE* ]] && break; done
echo "status: $M"
tail -5 "$ROOT/logs/launchd_trader.err.log" 2>/dev/null | grep -i "LIVE_BLOCKED" && fail "LIVE_BLOCKED (see logs/launchd_trader.err.log)"
echo "LIVE trader running; challenge starts at the first natural D0 BUY fill (no forced order)."
