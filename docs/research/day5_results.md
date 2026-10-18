# DAY 5 strategy research — identical conditions

- dataset: KRW-BTC 1m 2024-09-24 → 2026-09-25 (1,049,964 bars) resampled to 15m (70,019 bars)
- walk-forward: train 180d / test 60d, rolled by 60d; selection score = train return / max(MDD,1) with >=5 trades
- costs: fee 0.0005 per side, slippage 3 bps; stress 1.5x / 2.0x on both
- sizing: 0.3 of equity, cap ₩30,000, min ₩5,000, 1 position; fills at next 15m open; STOP-first intrabar rule
- criteria: OOS return>0, PF>=1.20, MDD<=10%, profitable windows>=60%, OOS trades>=50, neighbour positive-expectancy>=60% & no cliff, 1.5x costs keep >=50% of return
## A. trend_pullback — 1h close > EMA200 trend; 15m RSI pullback below threshold then EMA-fast re-cross entry; ATR stop/trail; exit on close < EMA slow
TF 15m / ref 1h · base {'rsi_period': 14, 'atr_period': 14, 'volume_sma_period': 20, 'trend_ema': 200, 'ema_fast': 20, 'ema_slow': 50, 'pullback_rsi': 40, 'arm_bars': 8, 'rsi_exit_low': 45, 'atr_stop_mult': 2.5, 'atr_trail_mult': 3.5}
grid {'pullback_rsi': [35, 40, 45], 'ema_slow': [50, 100], 'atr_stop_mult': [2.0, 3.0], 'atr_trail_mult': [3.0, 5.0]} (24 combos)

| metric | FULL backtest (base params) | OOS (walk-forward stitched) | Buy & Hold |
|---|---|---|---|
| Net return | -0.21% | -2.71% | +35.75% |
| Max drawdown | 0.27% | 6.30% | 50.85% |
| Trades | 2 | 79 | — |
| Profit factor | 0.00 | 0.71 | — |
| Win rate | 0.0% | 24.1% | — |
| Expectancy | ₩-104 | ₩-34 | — |
| Fees | ₩60 | ₩2,370 | — |
| Exposure | 0.0% | 4.3% | 100% |
| Profitable OOS windows | — | 3/9 | — |

Walk-forward windows (train→test):

| train | test_end | chosen | train% | test% | testMDD% | trades |
|---|---|---|---|---|---|---|
| 2024-09-24→03-23 | 2025-05-22 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 3.0, 'atr_trail_mult': 3.0} | +1.29 | +0.83 | 0.67 | 9 |
| 2024-11-23→05-22 | 2025-07-21 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 3.0, 'atr_trail_mult': 5.0} | +1.32 | +1.96 | 0.73 | 9 |
| 2025-01-22→07-21 | 2025-09-19 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 2.0, 'atr_trail_mult': 5.0} | +2.41 | -0.54 | 0.57 | 4 |
| 2025-03-23→09-19 | 2025-11-18 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 2.0, 'atr_trail_mult': 5.0} | +1.83 | -0.53 | 1.15 | 10 |
| 2025-05-22→11-18 | 2026-01-17 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 3.0, 'atr_trail_mult': 5.0} | +1.19 | -0.92 | 1.96 | 10 |
| 2025-07-21→01-17 | 2026-03-18 | {'pullback_rsi': 45, 'ema_slow': 50, 'atr_stop_mult': 3.0, 'atr_trail_mult': 3.0} | -0.76 | -0.59 | 0.78 | 4 |
| 2025-09-19→03-18 | 2026-05-17 | {'pullback_rsi': 45, 'ema_slow': 100, 'atr_stop_mult': 3.0, 'atr_trail_mult': 5.0} | -3.30 | -2.87 | 3.16 | 28 |
| 2025-11-18→05-17 | 2026-07-16 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 3.0, 'atr_trail_mult': 5.0} | -2.62 | +0.10 | 0.69 | 4 |
| 2026-01-17→07-16 | 2026-09-14 | {'pullback_rsi': 40, 'ema_slow': 100, 'atr_stop_mult': 3.0, 'atr_trail_mult': 5.0} | -1.62 | -0.11 | 0.12 | 1 |

Parameter stability: neighbours positive-expectancy ratio 21% over 43 evaluations, cliff windows 5

Cost stress (OOS):

| costs | return% | PF | MDD% | trades |
|---|---|---|---|---|
| 1.0x | -2.71 | 0.71 | 6.30 | 79 |
| 1.5x | -4.54 | 0.57 | 7.65 | 79 |
| 2.0x | -6.50 | 0.47 | 8.98 | 79 |

Approval matrix:

- FAIL  oos_return_gt_0
- FAIL  oos_pf_ge_1.20
- PASS  oos_mdd_le_10
- FAIL  profitable_windows_ge_60pct
- PASS  oos_trades_ge_50
- FAIL  parameter_stability
- PASS  cost_stress_1.5x

**NOT APPROVED** — parameter cliff in 5 window(s): overfit risk

## B. donchian_breakout — 15m close above N-bar Donchian high (optional 1h EMA200 filter); exit below M-bar low; ATR stop/trail
TF 15m / ref 1h · base {'rsi_period': 14, 'atr_period': 14, 'volume_sma_period': 20, 'trend_ema': 200, 'ema_fast': 20, 'ema_slow': 50, 'entry_n': 40, 'exit_n': 20, 'atr_stop_mult': 3.0, 'atr_trail_mult': 4.0, 'use_trend_filter': 1}
grid {'entry_n': [20, 40, 80], 'exit_n': [10, 20], 'atr_stop_mult': [2.0, 3.0], 'use_trend_filter': [0, 1]} (24 combos)

| metric | FULL backtest (base params) | OOS (walk-forward stitched) | Buy & Hold |
|---|---|---|---|
| Net return | -22.45% | -17.54% | +35.75% |
| Max drawdown | 27.59% | 19.89% | 50.85% |
| Trades | 484 | 344 | — |
| Profit factor | 0.61 | 0.53 | — |
| Win rate | 24.4% | 23.0% | — |
| Expectancy | ₩-46 | ₩-57 | — |
| Fees | ₩12,820 | ₩10,206 | — |
| Exposure | 16.6% | 13.8% | 100% |
| Profitable OOS windows | — | 0/9 | — |

Walk-forward windows (train→test):

| train | test_end | chosen | train% | test% | testMDD% | trades |
|---|---|---|---|---|---|---|
| 2024-09-24→03-23 | 2025-05-22 | {'entry_n': 80, 'exit_n': 20, 'atr_stop_mult': 3.0, 'use_trend_filter': 1} | -4.46 | -1.37 | 2.58 | 36 |
| 2024-11-23→05-22 | 2025-07-21 | {'entry_n': 80, 'exit_n': 20, 'atr_stop_mult': 3.0, 'use_trend_filter': 1} | -5.85 | -0.50 | 1.34 | 27 |
| 2025-01-22→07-21 | 2025-09-19 | {'entry_n': 80, 'exit_n': 20, 'atr_stop_mult': 2.0, 'use_trend_filter': 1} | -2.57 | -2.53 | 2.53 | 31 |
| 2025-03-23→09-19 | 2025-11-18 | {'entry_n': 40, 'exit_n': 20, 'atr_stop_mult': 2.0, 'use_trend_filter': 0} | -12.09 | -4.30 | 4.52 | 65 |
| 2025-05-22→11-18 | 2026-01-17 | {'entry_n': 80, 'exit_n': 20, 'atr_stop_mult': 3.0, 'use_trend_filter': 1} | -5.85 | -2.28 | 3.72 | 28 |
| 2025-07-21→01-17 | 2026-03-18 | {'entry_n': 40, 'exit_n': 10, 'atr_stop_mult': 3.0, 'use_trend_filter': 1} | -6.21 | -0.83 | 2.57 | 21 |
| 2025-09-19→03-18 | 2026-05-17 | {'entry_n': 40, 'exit_n': 10, 'atr_stop_mult': 3.0, 'use_trend_filter': 1} | -5.83 | -2.91 | 3.50 | 48 |
| 2025-11-18→05-17 | 2026-07-16 | {'entry_n': 80, 'exit_n': 10, 'atr_stop_mult': 3.0, 'use_trend_filter': 0} | -5.60 | -4.14 | 4.63 | 43 |
| 2026-01-17→07-16 | 2026-09-14 | {'entry_n': 80, 'exit_n': 20, 'atr_stop_mult': 3.0, 'use_trend_filter': 0} | -6.36 | -0.14 | 2.75 | 45 |

Parameter stability: neighbours positive-expectancy ratio 0% over 39 evaluations, cliff windows 0

Cost stress (OOS):

| costs | return% | PF | MDD% | trades |
|---|---|---|---|---|
| 1.0x | -17.54 | 0.53 | 19.89 | 344 |
| 1.5x | -22.44 | 0.45 | 24.24 | 320 |
| 2.0x | -26.21 | 0.40 | 27.72 | 313 |

Approval matrix:

- FAIL  oos_return_gt_0
- FAIL  oos_pf_ge_1.20
- FAIL  oos_mdd_le_10
- FAIL  profitable_windows_ge_60pct
- PASS  oos_trades_ge_50
- FAIL  parameter_stability
- PASS  cost_stress_1.5x

**NOT APPROVED**

## C. volatility_breakout — 4h open + k×previous-4h-range breakout (15m trigger, once per 4h bar, optional 4h EMA200 filter); time exit after hold_bars 4h bars; ATR stop/trail
TF 15m / ref 4h · base {'rsi_period': 14, 'atr_period': 14, 'volume_sma_period': 20, 'trend_ema': 200, 'ema_fast': 20, 'ema_slow': 50, 'k': 0.5, 'hold_bars': 1, 'atr_stop_mult': 2.0, 'atr_trail_mult': 3.0, 'use_trend_filter': 1}
grid {'k': [0.4, 0.5, 0.6], 'hold_bars': [1, 2], 'atr_stop_mult': [1.5, 2.5], 'use_trend_filter': [0, 1]} (24 combos)

| metric | FULL backtest (base params) | OOS (walk-forward stitched) | Buy & Hold |
|---|---|---|---|
| Net return | -24.83% | -15.40% | +35.75% |
| Max drawdown | 27.27% | 15.68% | 50.85% |
| Trades | 746 | 411 | — |
| Profit factor | 0.54 | 0.55 | — |
| Win rate | 26.7% | 26.3% | — |
| Expectancy | ₩-33 | ₩-40 | — |
| Fees | ₩19,708 | ₩12,227 | — |
| Exposure | 7.1% | 8.0% | 100% |
| Profitable OOS windows | — | 1/9 | — |

Walk-forward windows (train→test):

| train | test_end | chosen | train% | test% | testMDD% | trades |
|---|---|---|---|---|---|---|
| 2024-09-24→03-23 | 2025-05-22 | {'k': 0.6, 'hold_bars': 1, 'atr_stop_mult': 2.5, 'use_trend_filter': 1} | -2.77 | -1.61 | 2.18 | 49 |
| 2024-11-23→05-22 | 2025-07-21 | {'k': 0.6, 'hold_bars': 1, 'atr_stop_mult': 2.5, 'use_trend_filter': 1} | -1.56 | -2.64 | 2.71 | 40 |
| 2025-01-22→07-21 | 2025-09-19 | {'k': 0.5, 'hold_bars': 1, 'atr_stop_mult': 1.5, 'use_trend_filter': 1} | -7.12 | -1.01 | 1.03 | 21 |
| 2025-03-23→09-19 | 2025-11-18 | {'k': 0.6, 'hold_bars': 2, 'atr_stop_mult': 2.5, 'use_trend_filter': 0} | -14.16 | -6.49 | 6.49 | 92 |
| 2025-05-22→11-18 | 2026-01-17 | {'k': 0.4, 'hold_bars': 2, 'atr_stop_mult': 2.5, 'use_trend_filter': 0} | -21.25 | -3.56 | 5.09 | 119 |
| 2025-07-21→01-17 | 2026-03-18 | {'k': 0.6, 'hold_bars': 2, 'atr_stop_mult': 2.5, 'use_trend_filter': 1} | -3.67 | +0.28 | 0.76 | 5 |
| 2025-09-19→03-18 | 2026-05-17 | {'k': 0.6, 'hold_bars': 1, 'atr_stop_mult': 2.5, 'use_trend_filter': 1} | -0.44 | -1.07 | 1.70 | 44 |
| 2025-11-18→05-17 | 2026-07-16 | {'k': 0.6, 'hold_bars': 1, 'atr_stop_mult': 2.5, 'use_trend_filter': 1} | -3.74 | +0.00 | 0.00 | 0 |
| 2026-01-17→07-16 | 2026-09-14 | {'k': 0.6, 'hold_bars': 1, 'atr_stop_mult': 2.5, 'use_trend_filter': 1} | -3.02 | -0.28 | 1.67 | 41 |

Parameter stability: neighbours positive-expectancy ratio 0% over 37 evaluations, cliff windows 0

Cost stress (OOS):

| costs | return% | PF | MDD% | trades |
|---|---|---|---|---|
| 1.0x | -15.40 | 0.55 | 15.68 | 411 |
| 1.5x | -29.91 | 0.34 | 30.00 | 472 |
| 2.0x | -36.89 | 0.28 | 36.91 | 468 |

Approval matrix:

- FAIL  oos_return_gt_0
- FAIL  oos_pf_ge_1.20
- FAIL  oos_mdd_le_10
- FAIL  profitable_windows_ge_60pct
- PASS  oos_trades_ge_50
- FAIL  parameter_stability
- PASS  cost_stress_1.5x

**NOT APPROVED**

---
# Result: NO_STRATEGY_APPROVED
