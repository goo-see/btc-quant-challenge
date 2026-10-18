#!/bin/zsh
# Install/remove the independent launchd user agents (trader, api, watch, shadow-round3).
# usage: scripts/launchd_install.sh install [label ...]|uninstall [label ...]|status|restart <trader|api|watch|shadow-round3|shadow-e1|shadow-eth-grid|eth-grid>
# `install`/`uninstall` without labels act on the CORE agents only (trader, api, watch); pass explicit
# labels to touch a single agent (e.g. `install com.quant.shadow-round3`) without restarting the trader.
# Agents live in ~/Library/LaunchAgents (gui domain): they start at login and are restarted by
# launchd (KeepAlive) whenever the process exits. Trader and API are independent services.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
AGENTS="$HOME/Library/LaunchAgents"
UIDN="$(id -u)"
mkdir -p "$AGENTS" "$ROOT/logs"

MODE="${TRADING_MODE:-PAPER}"   # trader mode baked into the rendered plist (PAPER unless explicitly LIVE)
case "$MODE" in PAPER|LIVE) ;; *) echo "TRADING_MODE must be PAPER or LIVE (got $MODE)"; exit 2 ;; esac
render() { sed -e "s#__BACKEND__#$BACKEND#g" -e "s#__ROOT__#$ROOT#g" -e "s#__TRADING_MODE__#$MODE#g" "$ROOT/scripts/launchd/$1.plist" > "$AGENTS/$1.plist"; plutil -lint "$AGENTS/$1.plist" >/dev/null || { echo "invalid plist $1"; exit 1; }; }

case "${1:-status}" in
  install)
    shift; LABELS="${*:-com.quant.trader com.quant.api com.quant.watch}"
    for l in $LABELS; do
      render "$l"
      launchctl bootout "gui/$UIDN/$l" 2>/dev/null || true
      launchctl bootstrap "gui/$UIDN" "$AGENTS/$l.plist" && echo "loaded $l"
    done ;;
  uninstall)
    shift; LABELS="${*:-com.quant.trader com.quant.api com.quant.watch}"
    for l in $LABELS; do launchctl bootout "gui/$UIDN/$l" 2>/dev/null && echo "unloaded $l"; rm -f "$AGENTS/$l.plist"; done ;;
  restart)
    launchctl kickstart -k "gui/$UIDN/com.quant.${2:?trader|api|watch|shadow-round3|shadow-e1|eth-grid}" && echo "kickstarted com.quant.$2" ;;
  mode)
    plutil -extract EnvironmentVariables.TRADING_MODE raw "$AGENTS/com.quant.trader.plist" 2>/dev/null ;;
  status)
    for l in com.quant.trader com.quant.api com.quant.watch com.quant.shadow-round3 com.quant.shadow-e1 com.quant.shadow-eth-grid com.quant.eth-grid; do
      printf "%-18s " "$l"; launchctl print "gui/$UIDN/$l" 2>/dev/null | grep -E "^\s+(state|pid) = " | tr -s ' ' | tr '\n' ' ' || echo -n "not loaded"; echo
    done ;;
  *) echo "usage: $0 install [label ...]|uninstall [label ...]|status|restart <trader|api|watch|shadow-round3|shadow-e1|shadow-eth-grid|eth-grid>"; exit 2 ;;
esac
