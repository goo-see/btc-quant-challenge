# Research Round 3 — BTC Trend Following: Timeframe & Breakout Horizon Study
## PRE-REGISTRATION (frozen 2026-09-27 00:25 KST — written BEFORE any Round 3 backtest was run)

Experiment ID: `round3_tf_horizon_20260927` · config: `config/research/round3.yaml` (hash in research DB)

### Research question
Does evaluating a BTC trend-following breakout on 1H/2H bars (instead of 4H) and/or shortening the
breakout horizon (5d → 3d → 2d) improve entry timing WITHOUT degrading long-run robustness
(walk-forward OOS return / MDD / PF / cost sensitivity)?

### Control
D0 = `trend_following_4h_v1` (frozen params, hash `c0e59de4f7b70c0c`) — the live PAPER strategy.
Never modified. Its Round 3 replica must hash to the same value and reproduce the frozen 9Y result.

### Hypotheses (fixed parameters, no optimisation in this round)
| id | TF | horizon | entry_n | exit_n | atr_period | hypothesis |
|---|---|---|---|---|---|---|
| D0 | 4h | 5d | 30 | 20 | 14 | CONTROL |
| D1 / H1 | 2h | 5d | 60 | 40 | 28 | same ≈5d horizon evaluated on 2h bars → faster entry/exit improves results |
| D2 / H2 | 1h | 5d | 120 | 80 | 56 | 1h evaluation adds further reaction speed at the same horizon |
| D3 / H3 | 2h | 3d | 36 | 24 | 28 | 2h + ≈3d breakout catches the start of up-trends earlier than D0 |
| D4 / H4 | 1h | 3d | 72 | 48 | 56 | 1h + ≈3d captures fast BTC trends while absorbing more false breakouts |
| D5 / H5 | 2h | 2d | 24 | 16 | 28 | aggressive 2h + ≈2d: extra return compensates higher costs/MDD |
| D6 / H6 | 1h | 2d | 48 | 32 | 56 | fastest 1h + ≈2d: meaningfully earlier entries, or only noise/fake breakouts |

Independent variables: (1) evaluation timeframe, (2) breakout horizon. Nothing else changes.
Controlled: daily (1d) EMA100 trend filter; ATR stop 3.0×; trailing 4.0× (ratchet only);
exit on trend loss; channel exit on close < lowest low of previous exit_n bars; no pyramiding;
closed candles only; current candle excluded from the Donchian channel; ATR length ≈ 56 hours on
every TF (4h→14, 2h→28, 1h→56); identical sizing (30% of equity, cap ₩30,000, min ₩5,000, 1
position), fee 0.05%/side, slippage 3 bps, next-base-bar fill, STOP-first intrabar rule.

### Dataset
Upbit KRW-BTC 1h candles 2017-09-25 03:00Z → 2026-09-25 13:00Z (open_time; last bar closes
14:00Z) — the same range as the frozen 9Y D0 run (`backtest_runs.id=11`). Base bars = 1h
(intrabar stops at 1h resolution); 2h/4h/1d are built by the same `CandleAggregator` (UTC buckets).

### Walk-forward protocol
train 540d / test 180d / roll 180d / warm-up prefix 240d (indicator + channel state only, no
trades). Parameters are FIXED, so "train" is not used for selection — the OOS windows are simply
the stitched 180-day test segments (identical window boundaries to Round 2, ₩100,000 reset per
window). Parameter-neighbourhood stability is not evaluated (nothing is selected).
Additional fixed-parameter runs: Full period (same as above), Recent 2Y (2024-09-25 → end, 240d
warm-up prefix), 2026 YTD (2026-01-01 → end, 240d warm-up prefix). Cost stress: WF at 1.5× and
2.0× costs (fee and slippage both scaled).

### Forward shadow protocol
Separate launchd process `com.quant.shadow-round3` (venv python, KeepAlive, no network, no LLM).
Reads `data/quant.db` read-only (`?mode=ro`), writes only `data/research_round3.db`. Seven
independent virtual ₩100,000 portfolios (D0 replica + D1..D6) driven by the canonical 1m stream
(1h history warm-up 400d before the UTC-midnight boundary preceding the start). Fills are virtual
(next 1m open + slippage + fee); stops are checked on every 1m bar (STOP-first). Decisions are
recorded per closed strategy-TF bar at/after `started_at = 2026-09-27 00:00 KST`, idempotent on
(strategy_id, market_bar_time). Every BUY_SHADOW is tracked forward (+4/8/12/24h returns, MFE/MAE,
stop hit, exit, PnL, D0 decision on the same bar, lead hours vs the nearest D0 entry within ±10d).
Forward data through 2026-09-29 23:00 KST is ~3 days: it is for execution verification, reaction-
speed comparison and signal-behaviour observation ONLY — never for ranking strategies by return.

### Pre-registered promotion gate (next-round candidate status only; never a PAPER/LIVE promotion)
Core (all required, else REJECT): WF OOS return > 0 · OOS PF ≥ 1.20 · OOS MDD ≤ 10% ·
profitable WF windows ≥ 60% · OOS return at 1.5× costs > 0.
PASS additionally requires: OOS trades ≥ 50 · OOS return at 2.0× costs > 0 · no trade
concentration risk (return ex-top-1 and ex-top-3 stay > 0) · Recent 2Y return > 0 ("not collapsed").
REVIEW = core passed but at least one of the additional conditions failed (incl. 30–49 trades).
REJECT = any core criterion failed, or OOS trades < 30.
Informational (not gating): fake-breakout rate = losing trades closed within 48h of entry;
entry timing vs D0 = for each D0 full-period entry, the earliest Dk entry within [D0−5d, D0+1d].
Pairwise reads: D0/D1/D2 (timeframe effect at 5d), D1/D3/D5 (horizon effect at 2h),
D2/D4/D6 (horizon effect at 1h). Results will not change these criteria.
