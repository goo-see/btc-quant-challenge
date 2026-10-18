# Strategy research round 2 — low-frequency trend following on 1h history

- dataset: Upbit REST GET /v1/candles/minutes/60 (unit=60), UTC-hour buckets; 2017-09-25T03:00:00+00:00 → 2026-09-25T10:00:00+00:00; 78,763 bars, coverage 99.831% (missing 133, dup 0, ohlc violations 0); largest gaps [('2018-08-21T17:00:00+00:00', '2018-08-22T01:00:00+00:00', 7), ('2023-12-03T15:00:00+00:00', '2023-12-03T21:00:00+00:00', 5), ('2019-01-23T17:00:00+00:00', '2019-01-23T22:00:00+00:00', 4), ('2023-04-23T17:00:00+00:00', '2023-04-23T22:00:00+00:00', 4), ('2023-12-31T16:00:00+00:00', '2023-12-31T21:00:00+00:00', 4)]
- base bars 1h (intrabar stop at 1h resolution); 4h/1d built by CandleAggregator (UTC buckets); fills at next 1h open; STOP-first rule
- walk-forward: train 540d / test 180d rolled by 180d, 240d warm-up prefix (indicator/channel state only — no trades, no equity); selection = train return / max(MDD,1) with >=3 trades
- costs: fee 0.0005/side, slippage 3 bps; stress 1.5x / 2.0x
- sizing: 0.3 of equity, cap ₩30,000, min ₩5,000, 1 position
- criteria (unchanged): OOS return>0, PF>=1.20, MDD<=10%, profitable windows>=60%, OOS trades>=50 (30..49 -> REVIEW only), neighbour stability, 1.5x cost, no concentration risk
## D. trend_following_4h — 1d close > EMA(trend) filter; 4h close above N-bar high entry; ATR stop/trail; exit below M-bar low or trend loss
TF 4h / trend 1d · base {'rsi_period': 14, 'atr_period': 14, 'volume_sma_period': 20, 'ema_fast': 20, 'ema_slow': 50, 'trend_ema': 50, 'entry_n': 30, 'exit_n': 15, 'atr_stop_mult': 3.0, 'atr_trail_mult': 4.0, 'exit_on_trend_loss': 1}
grid {'entry_n': [20, 30, 42], 'trend_ema': [50, 100], 'atr_stop_mult': [2.0, 3.0], 'exit_n': [10, 20]} (24 combos) · WF train 540d / test 180d / warm-up 240d · 15 windows

| metric | FULL (base params) | OOS (stitched) | Buy & Hold |
|---|---|---|---|
| Net return | +161.67% | +200.35% | +2631.60% |
| Max drawdown | 6.82% | 6.72% | 87.49% |
| Trades | 133 | 119 | — |
| Profit factor | 4.24 | 3.45 | — |
| Win rate | 45.9% | 44.5% | — |
| Expectancy | ₩1,200 | ₩950 | — |
| Fees | ₩4,087 | ₩3,653 | — |
| Exposure | 22.6% | 21.5% | 100% |
| Profitable OOS windows | — | 14/15 | — |

| train | test_end | chosen | train% | test% | testMDD% | trades | B&H% |
|---|---|---|---|---|---|---|---|
| 2017-09-25→19-03-19 | 2019-09-15 | {'entry_n': 30, 'trend_ema': 50, 'atr_stop_mult': 3.0, 'exit_n': 20} | +29.01 | +33.57 | 5.04 | 11 | +173.5 |
| 2018-03-24→19-09-15 | 2020-03-13 | {'entry_n': 30, 'trend_ema': 50, 'atr_stop_mult': 3.0, 'exit_n': 20} | +38.60 | +1.82 | 2.56 | 5 | -47.7 |
| 2018-09-20→20-03-13 | 2020-09-09 | {'entry_n': 30, 'trend_ema': 50, 'atr_stop_mult': 3.0, 'exit_n': 20} | +37.15 | +6.29 | 4.07 | 10 | +87.4 |
| 2019-03-19→20-09-09 | 2021-03-08 | {'entry_n': 30, 'trend_ema': 50, 'atr_stop_mult': 2.0, 'exit_n': 10} | +44.53 | +25.23 | 6.26 | 17 | +384.9 |
| 2019-09-15→21-03-08 | 2021-09-04 | {'entry_n': 30, 'trend_ema': 50, 'atr_stop_mult': 3.0, 'exit_n': 10} | +37.17 | +0.10 | 6.72 | 10 | -0.1 |
| 2020-03-13→21-09-04 | 2022-03-03 | {'entry_n': 30, 'trend_ema': 100, 'atr_stop_mult': 3.0, 'exit_n': 10} | +37.99 | +6.98 | 2.36 | 5 | -8.1 |
| 2020-09-09→22-03-03 | 2022-08-30 | {'entry_n': 30, 'trend_ema': 100, 'atr_stop_mult': 3.0, 'exit_n': 10} | +38.74 | +0.95 | 1.03 | 1 | -47.9 |
| 2021-03-08→22-08-30 | 2023-02-26 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +13.11 | +4.32 | 1.56 | 3 | +11.3 |
| 2021-09-04→23-02-26 | 2023-08-25 | {'entry_n': 30, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +12.27 | +8.43 | 2.07 | 7 | +14.8 |
| 2022-03-03→23-08-25 | 2024-02-21 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +15.27 | +13.74 | 3.28 | 12 | +103.5 |
| 2022-08-30→24-02-21 | 2024-08-19 | {'entry_n': 30, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +24.48 | +4.95 | 4.46 | 11 | +12.8 |
| 2023-02-26→24-08-19 | 2025-02-15 | {'entry_n': 42, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +25.70 | +9.95 | 3.19 | 11 | +80.9 |
| 2023-08-25→25-02-15 | 2025-08-14 | {'entry_n': 42, 'trend_ema': 100, 'atr_stop_mult': 3.0, 'exit_n': 20} | +28.56 | +3.68 | 2.28 | 7 | +14.3 |
| 2024-02-21→25-08-14 | 2026-02-10 | {'entry_n': 42, 'trend_ema': 100, 'atr_stop_mult': 3.0, 'exit_n': 20} | +19.61 | +2.15 | 1.44 | 3 | -38.5 |
| 2024-08-19→26-02-10 | 2026-08-09 | {'entry_n': 30, 'trend_ema': 50, 'atr_stop_mult': 2.0, 'exit_n': 20} | +20.22 | -2.40 | 2.40 | 6 | -12.0 |

Regimes: 9 bull / 6 bear test windows (by B&H sign); avg OOS return bull +12.24% (profitable 9/9), bear +1.60% (profitable 5/6); years [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
Parameter stability: neighbour positive-expectancy 99% over 70 evaluations, cliff windows 0

| costs | OOS return% | PF | MDD% | trades |
|---|---|---|---|---|
| 1.0x | +200.35 | 3.45 | 6.72 | 119 |
| 1.5x | +192.38 | 3.31 | 6.89 | 119 |
| 2.0x | +184.62 | 3.18 | 7.06 | 119 |

Trade concentration (OOS, 119 trades): largest win ₩15,444, median ₩-175, total ₩113,050; top1/3/5 contribution 14%/37%/53%; return ex-top1/3/5 +97.61% / +70.69% / +53.37% → ok

Approval matrix:

- PASS  oos_return_gt_0
- PASS  oos_pf_ge_1.20
- PASS  oos_mdd_le_10
- PASS  profitable_windows_ge_60pct
- PASS  oos_trades_ge_50
- PASS  oos_trades_ge_30_review
- PASS  parameter_stability
- PASS  cost_stress_1.5x
- PASS  no_concentration_risk

**APPROVED**

## E. trend_following_1d — daily close > EMA(trend) filter; N-day breakout entry; ATR stop/trail; exit below M-day low or trend loss
TF 1d / trend 1d · base {'rsi_period': 14, 'atr_period': 14, 'volume_sma_period': 20, 'ema_fast': 20, 'ema_slow': 50, 'trend_ema': 100, 'entry_n': 40, 'exit_n': 20, 'atr_stop_mult': 3.0, 'atr_trail_mult': 4.0, 'exit_on_trend_loss': 1}
grid {'entry_n': [20, 40, 55], 'trend_ema': [100, 200], 'atr_stop_mult': [2.0, 3.0], 'exit_n': [10, 20]} (24 combos) · WF train 540d / test 180d / warm-up 240d · 15 windows

| metric | FULL (base params) | OOS (stitched) | Buy & Hold |
|---|---|---|---|
| Net return | +125.54% | +258.96% | +2631.60% |
| Max drawdown | 13.30% | 12.49% | 87.49% |
| Trades | 33 | 29 | — |
| Profit factor | 4.90 | 7.59 | — |
| Win rate | 51.5% | 48.3% | — |
| Expectancy | ₩3,811 | ₩4,759 | — |
| Fees | ₩1,058 | ₩997 | — |
| Exposure | 27.0% | 29.3% | 100% |
| Profitable OOS windows | — | 11/15 | — |

| train | test_end | chosen | train% | test% | testMDD% | trades | B&H% |
|---|---|---|---|---|---|---|---|
| 2017-09-25→19-03-19 | 2019-09-15 | {'entry_n': 40, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | -9.07 | +58.71 | 7.64 | 1 | +173.5 |
| 2018-03-24→19-09-15 | 2020-03-13 | {'entry_n': 40, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +54.06 | -0.83 | 3.45 | 2 | -47.7 |
| 2018-09-20→20-03-13 | 2020-09-09 | {'entry_n': 40, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +57.92 | +6.56 | 5.17 | 2 | +87.4 |
| 2019-03-19→20-09-09 | 2021-03-08 | {'entry_n': 40, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +65.62 | +56.55 | 12.49 | 2 | +384.9 |
| 2019-09-15→21-03-08 | 2021-09-04 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 3.0, 'exit_n': 10} | +70.03 | +3.26 | 7.09 | 2 | -0.1 |
| 2020-03-13→21-09-04 | 2022-03-03 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 3.0, 'exit_n': 10} | +73.04 | +1.96 | 3.99 | 2 | -8.1 |
| 2020-09-09→22-03-03 | 2022-08-30 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +69.76 | -0.16 | 2.35 | 1 | -47.9 |
| 2021-03-08→22-08-30 | 2023-02-26 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +6.07 | +4.69 | 1.72 | 1 | +11.3 |
| 2021-09-04→23-02-26 | 2023-08-25 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +8.11 | +2.35 | 4.20 | 3 | +14.8 |
| 2022-03-03→23-08-25 | 2024-02-21 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +5.07 | +9.16 | 4.96 | 3 | +103.5 |
| 2022-08-30→24-02-21 | 2024-08-19 | {'entry_n': 20, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 20} | +14.47 | +0.88 | 9.77 | 4 | +12.8 |
| 2023-02-26→24-08-19 | 2025-02-15 | {'entry_n': 55, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +15.51 | +8.75 | 6.64 | 2 | +80.9 |
| 2023-08-25→25-02-15 | 2025-08-14 | {'entry_n': 55, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +25.36 | +1.59 | 1.62 | 2 | +14.3 |
| 2024-02-21→25-08-14 | 2026-02-10 | {'entry_n': 40, 'trend_ema': 100, 'atr_stop_mult': 2.0, 'exit_n': 10} | +18.45 | -0.22 | 2.00 | 2 | -38.5 |
| 2024-08-19→26-02-10 | 2026-08-09 | {'entry_n': 40, 'trend_ema': 200, 'atr_stop_mult': 2.0, 'exit_n': 10} | +10.91 | +0.00 | 0.00 | 0 | -12.0 |

Regimes: 9 bull / 6 bear test windows (by B&H sign); avg OOS return bull +16.58% (profitable 9/9), bear +0.67% (profitable 2/6); years [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
Parameter stability: neighbour positive-expectancy 83% over 66 evaluations, cliff windows 0

| costs | OOS return% | PF | MDD% | trades |
|---|---|---|---|---|
| 1.0x | +258.96 | 7.59 | 12.49 | 29 |
| 1.5x | +256.26 | 7.45 | 12.51 | 29 |
| 2.0x | +253.58 | 7.31 | 12.54 | 29 |

Trade concentration (OOS, 29 trades): largest win ₩58,713, median ₩-93, total ₩138,001; top1/3/5 contribution 43%/90%/99%; return ex-top1/3/5 +79.29% / +13.84% / +1.25% → ok

Approval matrix:

- PASS  oos_return_gt_0
- PASS  oos_pf_ge_1.20
- FAIL  oos_mdd_le_10
- PASS  profitable_windows_ge_60pct
- FAIL  oos_trades_ge_50
- FAIL  oos_trades_ge_30_review
- PASS  parameter_stability
- PASS  cost_stress_1.5x
- PASS  no_concentration_risk

**NOT_APPROVED**

---
# Result: CANDIDATE D
