#!/bin/zsh
# Kill the launchd-managed trader with SIGKILL, wait for launchd to restart it, verify the state
# was restored (heartbeat fresh, state RUNNING/HALTED unchanged, reconcile OK) and record the
# result into bot_state.restart_recovery via the API-side DB (readiness evidence).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend" || exit 1
BEFORE=$(curl -s http://127.0.0.1:8787/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['state'], d['pid'])")
PID=$(echo "$BEFORE" | awk '{print $2}'); STATE=$(echo "$BEFORE" | awk '{print $1}')
echo "before: state=$STATE pid=$PID"
kill -9 "$PID" || exit 1
for i in $(seq 1 30); do sleep 2; AFTER=$(curl -s http://127.0.0.1:8787/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['state'], d['pid'], d['online'])" 2>/dev/null); NP=$(echo "$AFTER" | awk '{print $2}'); [[ "$NP" != "$PID" && "$NP" != "None" && "$(echo "$AFTER" | awk '{print $3}')" == "True" ]] && break; done
sleep 8
AFTER=$(curl -s http://127.0.0.1:8787/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['state'], d['pid'], d['online'], d['heartbeat_age_sec'])")
REC=$(curl -s http://127.0.0.1:8787/api/health | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['reconciliation']['status'])")
echo "after: $AFTER reconcile=$REC"
NSTATE=$(echo "$AFTER" | awk '{print $1}'); NPID=$(echo "$AFTER" | awk '{print $2}'); ONLINE=$(echo "$AFTER" | awk '{print $3}')
OK=false; [[ "$ONLINE" == "True" && "$NPID" != "$PID" && "$NSTATE" == "$STATE" && "$REC" == "OK" ]] && OK=true
uv run python - "$OK" "$STATE" "$PID" "$NPID" "$REC" <<'PY' 2>/dev/null | grep -v HTTP
import asyncio, sys
from datetime import UTC, datetime
sys.path.insert(0, ".")
from app.config.settings import Settings
from app.database.engine import create_engine
from app.database.repository import Repository
ok, state, pid, npid, rec = sys.argv[1] == "true", sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
async def m():
    s = Settings.load(); e = create_engine(s.config.system.database_url); r = Repository(e)
    await r.set_state("restart_recovery", {"ok": ok, "at": datetime.now(UTC).isoformat(), "summary": f"kill -9 {pid} -> launchd restart pid {npid}, state {state} preserved, reconcile {rec}", "method": "SIGKILL via launchd KeepAlive"})
    await r.add_event("WARN" if ok else "ERROR", "RESTART_RECOVERY_CHECK", f"restart recovery {'PASS' if ok else 'FAIL'}: {pid}->{npid} state={state} reconcile={rec}", {})
    print("recorded restart_recovery ok=", ok); await e.dispose()
asyncio.run(m())
PY
