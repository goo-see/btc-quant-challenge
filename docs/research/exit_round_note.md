# D0 Exit-Rule Research — E0 control vs E1–E4 (2026-10-03)

Research only. LIVE D0 v1 (`trend_following_4h_v1`, hash `c0e59de4f7b70c0c`) was not modified; the
open LIVE position, its stop, approvals and processes were untouched (before/after snapshot in
`docs/exit-round-report.md`). Code: `backend/app/research/exit_round/` (isolated engine using the
production `TrendFollowing4h` class for entries; overlays applied in the research engine only);
artifacts `docs/research/exit_round_E{0..4}.json`, `exit_round_results.md`.

## Pre-registered design
- Question: can an exit overlay reduce profit-to-loss reversals while keeping D0's return / PF /
  MDD / expectancy and not cutting large winners?
- Entries identical to D0 (frozen params). Exit overlays are the only independent variable:
  E0 control · E1 break-even at MFE ≥ +1R (fee/slippage-adjusted) · E2 trail 3×ATR · E3 50% partial
  at +2R · E4 stop ≥ entry+0.5R at MFE ≥ +1.5R (fee/slippage-adjusted). Fixed values, no grid.
- Dataset / protocol: Upbit KRW-BTC 1h 2017-09-25 → 2026-09-25 (78,766 bars, same last bar as the
  frozen 9Y run), 4h/1d via the production aggregator, WF 540/180/180/240 (15 windows), fee 0.05%,
  slippage 3 bps, 30% / ₩30,000 / 1 position, next-1h-open fill, STOP-first. Overlays act from the
  bar after the trigger bar (conservative at 1h resolution). R = entry fill − initial stop.
- Reading of the §15 criteria (fixed before results): MDD not worse (+0.25pp tol.), PF ≥ 85% of E0,
  expectancy ≥ 85%, meaningful P→L (MFE ≥ 0.5R) reduced, recent 2Y > 0 and ≥ 50% of E0, profitable
  WF windows ≥ E0−1, 2.0× cost OOS > 0, top-10 E0 winners' PnL ≥ 85% kept, OOS return ≥ 70% of E0.
  PROMISING = all pass; MIXED = ≤ 2 fails incl. reversal reduced + 2Y + cost kept; else REJECT.

## Results (details in exit_round_results.md)
| | E0 | E1 BE | E2 3ATR | E3 partial | E4 lock |
|---|---|---|---|---|---|
| WF OOS return / MDD / PF | +226.3% / 7.23% / 4.06 | **+238.3% / 7.02% / 4.56** | +136.5% / 6.77% / 2.95 | +138.4% / 6.46% / 3.17 | +230.4% / 7.23% / 4.13 |
| OOS trades / win / expectancy | 105 / 44.8% / ₩1,172 | 105 / 52.4% / ₩1,213 | 136 / 50.0% / ₩672 | 105 / 44.8% / ₩832 | 107 / 45.8% / ₩1,178 |
| Full 9Y / Recent 2Y / YTD | +129.5% / +19.5% / +2.4% | +133.9% / +19.5% / +2.4% | +93.0% / +12.5% / +1.3% | +97.9% / +17.4% / +2.9% | +131.7% / +17.3% / +2.4% |
| Cost 1.5× / 2.0× | +218.7 / +211.2 | +239.0 / +230.4 | +129.3 / +122.3 | +133.6 / +126.4 | +222.5 / +215.1 |
| P→L (MFE>0) / meaningful (≥0.5R) / ≥1R→loss (full) | 63 / 32 / 13 | 50 / 19 / **0** | 72 / 29 / 4 | 63 / 32 / 13 | 64 / 31 / 12 |
| giveback R mean / p90 after 1R | 1.80 / 2.56 | 1.74 / 2.56 | 1.36 / 2.14 | 2.36 / 5.23 | 1.77 / 2.56 |
| trade-matched upside / winner capture | — | 1.03 / 1.00 | 0.42 / 0.49 | 0.75 / 0.82 | 1.01 / 1.00 |
| top-10 E0 winners ΔPnL (cut early) | — | ₩0 (0/10) | −₩70,385 (10/10) | −₩38,866 (0/10) | +₩43 (0/10) |
| verdict | control | **PROMISING** | REJECT | REJECT | PROMISING |

Notes: one position (2020-08-02) filled below its initial stop (R ≈ −1.9%), which distorts R-averages
only (avg realized R excluding it: E0 1.07, E1 1.10, E4 1.05; avg MFE R ≈ 2.5); reversal counts are
unaffected (its MFE is 0). E1 produced no extra entries (116/116 matched); E2 added 32 re-entries;
E4 changed 3 entries. +2R→loss was already 0 under E0.

## Interpretation (after results)
1. Does D0 give back too much? Partly: 54% of all positions were in profit at some point and 28%
   had reached +0.5R before ending in a loss; 13 of 116 reached +1R and still lost. Average giveback
   after a +1R excursion is 1.8R (p90 2.6R) — that is the price of the 4×ATR trail in a trend
   system, and it buys the large winners (top-10 trades = 58% of OOS profit).
2. Break-even (E1) removes exactly those 13 "+1R→loss" trades (now ≈₩0 each), cuts meaningful
   reversals 32 → 19, raises win rate 44.8 → 52.4%, PF 4.06 → 4.56, OOS return +226 → +238%, with
   identical entries, identical top-10 winners, 8 slightly earlier exits and the same recent-2Y
   result (no recent trade crossed the BE line and reversed). Cost: none visible in this dataset —
   the only risk is the untested case where price returns to BE and then resumes the trend (none of
   the 65 ≥1R trades did so here because the 4×ATR trail was already above BE in most of them).
3. 3×ATR (E2) is clearly worse: it is the only candidate that changes entries (32 re-entries), it
   cut all top-10 winners early (−₩70k), halved upside capture (0.42), PF 2.95 — tighter trailing
   does not pay in this breakout system.
4. Partial TP (E3) lowers total return (+226 → +138%) and PF; it does not reduce reversals at all
   (the remaining half still rides to the same exits) and increases giveback dispersion (p90 5.2R).
5. Profit lock (E4) sits between E0 and E1: slightly better OOS return/PF, 12 vs 13 +1R→loss
   (the lock triggers at 1.5R, so most reversals from 1.0–1.5R are untouched), recent-2Y PF drops
   3.55 → 2.85 (two trades locked out early in 2025). It passes the pre-registered gate but adds
   little over E1 while being more complex.
6. Nothing here justifies touching LIVE D0 v1. E1 is a credible **next PAPER/SHADOW validation
   candidate** (D0 v2 exit rule), not a promotion.

## Limitations
- 1h base resolution: overlays act from the next bar and stops fill at 1h granularity; LIVE uses
  tick-level stops (favourable for BE in practice — the lock would engage within the bar).
- D0 entry parameters carry Round-2 selection bias (same for every candidate; relative comparison).
- 116 positions over 9 years; the +1R→loss set is 13 trades — small-sample, direction is clear but
  the magnitude (+12pp OOS return) should be treated as indicative.
- E1's break-even price depends on the fee/slippage model (0.05% + 3 bps); real slippage differs.
