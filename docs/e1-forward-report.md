# D0 V2 E1 FORWARD INITIALIZATION — 2026-10-03 01:46 KST

LIVE D0 v1 (`trend_following_4h_v1`, hash `c0e59de4f7b70c0c`) untouched: trader pid 66796 before/after,
LIVE/RUNNING/ENTRY_AND_EXIT, position 0.00025795 BTC @ 116,300,000, stop 113,713,838, exchange KRW
70,042.91 / BTC 0.00025795, open orders 0, reconciliation OK, orders table 3 rows, production hashes
unchanged, approvals/TRADING_MODE unchanged. Only `com.quant.api` was restarted (dashboard route).

## Candidate / baseline
- **S1 = E1_BREAK_EVEN_1R**: D0 v1 rules + break-even stop once `highest_tick >= entry_fill + 1R`
  (R = virtual entry fill − initial stop, fixed at entry; `be_stop = ceil(entry_fill·(1+fee) /
  ((1−slip)(1−fee)))`, fee 0.05 %, slip 3 bps → net PnL ≥ 0 after both fees; no extra margin).
  Effective stop = max(initial, mirrored LIVE trailing, BE if activated); stop never decreases;
  stop source (INITIAL / TRAILING / BREAK_EVEN) stored on every update and exit.
- **S0 = S0_D0_V1**: same entry, same virtual fill, mirrors the LIVE D0 trailing ratchet
  (`bot_state.portfolio.stop_price`) and LIVE SELL decisions (channel / trend loss); tick stops
  processed by the shadow itself.
- Entry source: confirmed LIVE D0 BUY fills (`orders` mode=LIVE side=bid executed>0, read-only) →
  virtual fill at the first 1m open at/after the signal close + slippage + fee, same KRW amount as
  the LIVE order; the real LIVE fill is stored alongside for consistency checks.
- Ticks: public Upbit WebSocket ticker; per tick: stop check first, then excursion / +1R detection
  (BE in force from the next tick). Restart catch-up from closed 1m bars is pessimistic (LOW then HIGH).
- Persistence: full book state per (book, entry) JSON in `data/d0_v2_e1_forward.db` (positions,
  entries, events incl. BE_ACTIVATED with trigger tick/seq, STOP_UPDATE with source, EXIT, trades,
  be_followups +4/8/12/24/48/72h + S0 final, observation, health, runner_log); persisted every ≤5 s
  and on every event.
- Process: launchd `com.quant.shadow-e1` (KeepAlive, RunAtLoad, venv python, no TRADING_MODE,
  no .env secrets) — pid 14384; independent of `com.quant.trader`.
- Current LIVE trade (order 3): **OBSERVATION_ONLY** — entry 116,300,000, initial stop 113,374,924,
  R 2,925,076, hypothetical +1R 119,225,076, hypothetical BE stop 116,451,294, MFE so far 0.46R;
  excluded from the primary sample. Primary forward start = 2026-10-02T16:46:26Z (01:46 KST).

## Pre-registered sample / gate (unchanged from the request)
Min observation 4 weeks; primary conclusions only with ≥ 5 matched completed trades and ≥ 3 +1R
triggers, else INSUFFICIENT_FORWARD_SAMPLE. Promotion-candidate criteria A–J (entry 100 % match,
persistence PASS, 0 actual orders, meaningful P→L and +1R→loss reduced, winner truncation limited,
BE-exit trend-restart not excessive, PF/expectancy/MDD not worse, no conflict with historical).

## Tests
`tests/unit/test_e1_forward.py` (12): S0/S1 same entry & fixed R · exact +1R trigger (S1 only, acts
from next tick) · +1R not reached ⇒ identical books · BE price covers both fees + slippage with no
margin (116,451,294 for entry 116,300,000; one won lower loses) · trailing > BE / BE > trailing ·
stop never decreases · same-tick ordering (stop before activation) · pessimistic catch-up · mirrored
channel / trend-loss / trailing exits · persistence round-trip incl. BE state and restart-after-BE ·
no order path / no secrets (AST) + plist · production DB handle read-only · poll-loop crash isolation.
