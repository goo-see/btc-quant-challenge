# ETH/KRW micro-grid fast-track research — G0..G3 (generated)

- dataset KRW-ETH 5m 2017-09-30 → 2026-10-05 (3291 days); sleeve ₩50,000, unit ₩5,000, seed ₩25,000, max inventory ₩40,000, min cash ₩10,000; fee 0.05%/side (orders/chance), slip+half-spread 5 bps/side; daily EMA100 buy guard; daily loss −5% pause; MDD 12% halt; pessimistic bar feed (no same-bar buy→sell)

## Total sleeve equity (cash + ETH mark-to-market − fees)

| id | gap | return | CAGR | MDD | PF | cycles | trades | win | exp/cycle | avg cycle h | fees | fee/gross | max inv | avg inv | underwater d | min cash | trades/day mean/med | best/worst day | halted | recenters |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| G0 | fixed 0.75% gap | +53.60% | +4.88% | 25.74% | 2.65 | 871 | 1742 | 100% | ₩31 | 8.0 | ₩4,374 | 0.14 | ₩38,584 | ₩454 | 12 | ₩17,053 | 34.2/23 | ₩4,270/₩-8,257 | True | 1201 |
| G1 | fixed 1.0% gap | +38.18% | +3.65% | 19.97% | 4.63 | 424 | 848 | 100% | ₩45 | 10.1 | ₩2,130 | 0.10 | ₩43,036 | ₩213 | 3 | ₩11,239 | 19.3/12 | ₩3,476/₩-3,138 | True | 677 |
| G2 | fixed 1.5% gap | +87.59% | +7.23% | 25.25% | 1.41 | 599 | 1198 | 100% | ₩73 | 237.9 | ₩3,016 | 0.06 | ₩44,083 | ₩9,041 | 1129 | ₩12,893 | 17.9/9 | ₩4,375/₩-5,257 | True | 306 |
| G3 | ATR-adaptive gap = clamp(1h ATR14 %, 0.75%, 2.0%) | +9.97% | +1.06% | 15.42% | 1.60 | 59 | 118 | 100% | ₩84 | 79.5 | ₩298 | 0.06 | ₩39,879 | ₩112 | 15 | ₩12,026 | 5.9/5 | ₩2,680/₩-2,142 | True | 420 |

## Recent windows & cost stress

| id | Recent 2Y ret/MDD/PF/cycles | Recent 6M ret/MDD/PF/cycles | 1.5x cost ret/PF | 2.0x cost ret/PF |
|---|---|---|---|---|
| G0 | +17.61% / 42.28% / 1.11 / 296 | +9.52% / 2.18% / 3.60 / 158 | +44.86% / 2.32 | +36.10% / 2.01 |
| G1 | +10.53% / 44.94% / 1.07 / 115 | +6.56% / 3.46% / 1.85 / 69 | +33.08% / 3.83 | +28.93% / 3.26 |
| G2 | +6.68% / 17.23% / 6.62 / 35 | +3.22% / 1.44% / 2.27 / 13 | +81.52% / 1.38 | +71.45% / 1.33 |
| G3 | +5.36% / 17.08% / 14.40 / 53 | +8.40% / 2.15% / 2.95 / 133 | +9.37% / 1.56 | +8.77% / 1.51 |

## Regimes (90-day windows; avg sleeve return / worst window / worst MDD / cycles)

| id | BULL | SIDEWAYS | HIGH_VOL | BEAR | CRASH |
|---|---|---|---|---|---|
| G0 | 10w avg +5.2% min +0.0% mdd 10.9% cyc 871 | 8w avg +0.0% min +0.0% mdd 0.0% cyc 0 | — | — | 18w avg +0.0% min +0.0% mdd 0.0% cyc 0 |
| G1 | 10w avg +3.7% min +0.0% mdd 4.7% cyc 424 | 8w avg +0.0% min +0.0% mdd 0.0% cyc 0 | — | — | 18w avg +0.0% min +0.0% mdd 0.0% cyc 0 |
| G2 | 10w avg +10.2% min +0.0% mdd 4.1% cyc 477 | 8w avg +0.0% min +0.0% mdd 0.0% cyc 0 | — | — | 18w avg -1.2% min -13.6% mdd 19.5% cyc 122 |
| G3 | 10w avg +0.9% min +0.0% mdd 5.7% cyc 59 | 8w avg +0.0% min +0.0% mdd 0.0% cyc 0 | — | — | 18w avg +0.0% min +0.0% mdd 0.0% cyc 0 |

## Pre-registered gate

- **G0: FAIL** — failed: ['mdd_le_12', 'bear_crash_survived']
- **G1: FAIL** — failed: ['mdd_le_12', 'bear_crash_survived', 'max_inventory_within_policy']
- **G2: FAIL** — failed: ['mdd_le_12', 'bear_crash_survived', 'max_inventory_within_policy']
- **G3: FAIL** — failed: ['mdd_le_12', 'bear_crash_survived']

**SELECTED_GRID = NONE**

## Portfolio: D0 only (100k) vs BTC D0 50k + ETH grid 50k

- portfolio combination not available (insufficient overlap)
