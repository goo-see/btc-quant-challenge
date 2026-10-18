# D0 LIVE preparation report — 2026-09-29 (prepared, NOT activated)

Status: **D0_LIVE_READY_FOR_USER_APPROVAL** — nothing was activated; `paper_approved`, `live_approved`,
`live_gate.enabled`, `TRADING_MODE`, `LIVE_CONFIRM` are unchanged. Zero real orders were placed.

## 1. Fixed strategy
`trend_following_4h_v1`, frozen hash `c0e59de4f7b70c0c` (= live PAPER epoch hash; D0 code/params md5 unchanged since
2026-09-25). Rules unchanged: daily close > EMA100; CLOSED 4H close > highest high of the previous 30 CLOSED 4H bars
(current bar excluded); initial stop = signal close − 3×ATR14(4H); trailing = max(prev stop, highest − 4×ATR) ratcheted
at 4H close; 20-bar channel exit; daily EMA100 trend-loss exit; tick-level stop execution; no pyramiding.

## 2. Account (private API, read-only + orders/test dry run, 23:28 KST)
KRW available **₩100,039.50**, locked ₩0 · BTC **0** (locked 0) · open orders **0** · fees bid/ask 0.05% ·
min order ₩5,000 · market state active · `challenge_capital` PASS (₩100,039 vs ₩100,000 ± 500).
Key permissions cannot be queried via API; the user confirmed 자산조회/주문하기 only (no withdrawal). Public IP
203.0.113.10 = configured whitelist IP, 103.5 h stable (IP_STABILITY_MINIMUM_PASS).

## 3. Sizing / risk (config unchanged, verified against code)
LIVE `max_position_pct 0.30`, `max_order_krw 30,000`, `max_concurrent_positions 1`, min order ₩5,000 (exchange
`min_total` applied at LIVE start when stricter), fee 0.05%. Example: ₩100,000 → BUY ≈ ₩30,000, ≈₩70,000 stays in
cash; SELL = full held quantity. `small_live_max_order_krw: 6000` remains **unwired** (user specified ₩30,000).
RiskManager order of checks matches the policy: FROZEN → reject all; EXIT_ONLY → reject BUY; UNKNOWN/INFLIGHT;
daily loss 3% / MDD 15% (kill → HALTED, BUY rejected, protective SELL continues); SELL volume = held qty ≥ min
total; BUY: consecutive losses 4, cooldown 60 m, 4 orders/h, position state, concurrency, sizing, min balance.
ExecutionMode: RUNNING → ENTRY_AND_EXIT · PAUSED / HALTED(DAILY_LOSS_LIMIT, MAX_DRAWDOWN) → EXIT_ONLY ·
MAINTENANCE / HALTED(reconcile, UNKNOWN order, DB, stale, API errors, manual kill) → FROZEN (`core/state.py`).

## 4. Changes made in this preparation (no strategy change)
| area | change |
|---|---|
| execution metrics | `execution_metrics` table (Alembic **0003**, applied to `data/quant.db`), `build_execution_record` (signal/send/fill, VWAP, slippage vs signal & vs reference at send, fee, qty; BUY: ATR, initial stop, stop distances; SELL: exit reason, trigger price, realized PnL, return %, holding), recorded in `TraderService._execute` (PAPER + LIVE), `GET /api/executions`, dashboard "Executions (slippage)" section |
| LIVE books | `_init_live_books()` on the first LIVE epoch start: exchange must show BTC 0, no open orders, KRW within tolerance; PAPER book archived to `portfolio_archive_<ts>`; LIVE book principal = exchange KRW; `challenge.live_activated_at`, `initial_challenge_capital`, `live_start_krw` recorded |
| challenge | `challenge_started_at` = first LIVE BUY fill (separate from `live_activated_at`); `/api/status.challenge` exposes both + challenge PnL + external-flow count |
| gates | **bug fix**: evidence gate enforced `min_paper_trades ≥ 10` (config says informational) → would have blocked LIVE with `PAPER_TRADES`; now informational. **bug fix**: preflight funding check would block a LIVE *restart* holding a position (`CHALLENGE_CAPITAL_NOT_FUNDED`); now enforced only before activation |
| exchange terms | `build_live_service` applies orders/chance min_total / fees to RiskManager when stricter |
| launchd | trader plist `TRADING_MODE` rendered from `$TRADING_MODE` (default PAPER); `launchd_install.sh mode`; `scripts/live_transition.sh` (gates → flat check → preflight → bootout PAPER → single LIVE trader → verify) — **only after `LIVE_APPROVE_D0`** |
| dashboard | Mode badge (`LIVE · REAL KRW`), ExecutionMode badge, challenge activated/started/PnL line, executions table; API agent restarted (trader untouched) |

In-memory gate simulation (approvals set only in memory): `live_allowed = True`, no non-PASS items.

## 4b. Restart-safety gate (2026-09-29, after the first report)
`recent_backtest` is now two gates: a **promotion gate** (first LIVE activation, new LIVE epoch,
strategy version or frozen-hash change → a frozen-param backtest ≤ 14 days is REQUIRED) and an
**active-epoch restart** (same version + hash, `challenge.live_activated_at` present, approvals
unchanged → a stale backtest only produces a WARNING in readiness/logs; the process recovers its
position, stops and reconciliation). Readiness shows "Frozen-param backtest age" (age, last time,
PASS/WARN) and "LIVE epoch restart context" (PROMOTION / ACTIVE_EPOCH_RESTART).

## 5. Findings requiring a user decision (not changed)
1. **KNOWN_D0_V1_LIMITATION — warm-up boundary flush** (documented in `docs/strategy-D-rules.md`; not fixed): the staged warm-up drops the last 1h-stage day at the UTC boundary
   (EMA100 ≈ +0.065% vs full data, decaying; channels/ATR unaffected). Over 3,192 daily bars, a +0.065% EMA shift
   would have flipped `trend_ok` on 8 days (0.25%); 0 decisions changed during Round 3. Fixing it can change a
   decision on such boundary days → treated as a D0 v1 change; recommend fixing **after** LIVE activation as a
   separate approved change (or before, with a new epoch hash unaffected — params unchanged).
2. ~~`require_recent_backtest_days: 14` restart blocker~~ — **fixed** (§4b): stale backtests no longer block a
   restart of the active LIVE epoch; they are surfaced as WARN. Periodic frozen-param backtests remain advisable
   (`scripts/run_backtest.py --base 1h --name D_frozen_full_1h_9y` records `backtest_runs`).
3. External deposit/withdrawal after start → reconciliation mismatch → HALTED (FROZEN) + flow recorded; operator
   RESUME required (conservative by design).
4. Shadow Round 3 runner: keep running for research (isolated, zero order capability) or stop; recommend keep
   until D1 review is decided (uses ~45 MB RSS, read-only market DB).

## 6. Tests
See the final answer for the exact run summary (ruff, mypy --strict, full pytest incl. replay scenarios, replay E2E,
new `test_live_preparation.py`: execution metrics, LIVE book init + 4 blocked cases, no-signal-no-order,
challenge timing, LIVE restart with open position, funding gate, plist mode rendering, transition script gates).

## 7. Activation procedure (after the user types `LIVE_APPROVE_D0`)
1. `config.yaml`: `trend_following_4h_v1.paper_approved: true`, `live_approved: true`; `live_gate.enabled: true`
2. `.env`: `TRADING_MODE=LIVE`, `LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_KRW` (user-entered)
3. `scripts/live_transition.sh` → preflight → PAPER trader bootout → single LIVE trader → dashboard shows LIVE
4. No forced order: the LIVE trader waits for the next natural D0 BUY (currently HOLD, 1.4% below the threshold).
