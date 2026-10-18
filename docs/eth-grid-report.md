# BTC D0 + ETH Micro-Grid parallel fast-track — result: GRID_RESEARCH_FAILED (2026-10-05 01:35 KST)

## LIVE protection (before 01:10 KST → after 01:34 KST)
BTC trader pid **66796** unchanged (LIVE / RUNNING / ENTRY_AND_EXIT); position 0.00025795 BTC @ 116,300,000
unchanged; stop moved 113,713,838 → 114,804,125 by the LIVE trader's own 4H trailing ratchet (not by this
work); exchange KRW ₩70,042.91 / BTC 0.00025795 / ETH 0; open orders BTC 0 / ETH 0; reconciliation OK; frozen
hash c0e59de4f7b70c0c; D0 code/yaml md5 unchanged; orders table 3 rows (no new orders). E1 shadow
(com.quant.shadow-e1 pid 14384) kept running, 0 errors. Only `com.quant.api` was restarted (new read-only routes).

## 1. Strategy sleeves (implemented; effective for the trader on its next (re)start)
- `config.yaml` `portfolio.sleeves`: **BTC_D0 ₩50,000** (LIVE) · **ETH_GRID ₩50,000** (PLANNED — research only).
- `app/portfolio/sleeves.py`: `sizing_caps` (equity for sizing = min(book, allocation); spendable cash =
  min(book cash, allocation − position value)) → D0 can never spend the ETH reserve; `sleeve_snapshot`
  (allocated / cash_reserved / position_value / realized / unrealized / fees / equity / available /
  next_max_entry / `grandfathered_position`); `reconcile_multi_asset` (exchange KRW = Σ LIVE ledger cash,
  BTC = D0 qty, ETH = grid qty — normal cross-strategy trades are never external flows).
- Trader: `_risk_context` applies the caps (next D0 entry = 30 % × ₩50,000 = **₩15,000**), `_persist_sleeve`
  writes `bot_state.strategy_sleeves`; Reconciler adds other LIVE ledgers; `Balances.others` + LiveGateway
  report all coins. The running LIVE trader still executes the pre-sleeve code — activation needs a
  restart (active-epoch restart gate makes it safe) at a moment the user chooses; the open ₩29.8k BTC
  position is **grandfathered** (never reduced) in both code paths.
- Dashboard: "전략별 자산" (BTC 추세 / ETH 반복매매 / GLOBAL) + `/api/portfolio/sleeves`.

## 2. ETH dataset
Upbit KRW-ETH **5m**, 2017-09-30 → 2026-10-05, **941,921 bars**, coverage 99.36 % (missing 6,081 bars, mostly
2017-10-18 exchange outage), duplicates 0, OHLC violations 0 → `data/eth_market.db` (separate from quant.db).
Exchange terms re-queried live: fee 0.05 %/0.05 %, min order ₩5,000, market active, ETH ≈ ₩3,657,000.

## 3. Candidates (pre-registered, no grid search) — total sleeve equity, pessimistic 5m feed
| | G0 0.75 % | G1 1.0 % | G2 1.5 % | G3 ATR 0.75–2.0 % |
|---|---|---|---|---|
| 9Y return / CAGR | +53.6 % / 4.9 % | +38.2 % / 3.7 % | +87.6 % / 7.2 % | +10.0 % / 1.1 % |
| **MDD** | **25.7 %** | **20.0 %** | **25.3 %** | **15.4 %** |
| PF (equity, daily) | 2.65 | 4.63 | 1.41 | 1.60 |
| cycles / trades / fees | 871 / 1,742 / ₩4,374 | 424 / 848 / ₩2,130 | 599 / 1,198 / ₩3,016 | 59 / 118 / ₩298 |
| fee / gross | 0.14 | 0.10 | 0.06 | 0.06 |
| max inventory | ₩38,584 | ₩43,036 (> 40k) | ₩44,083 (> 40k) | ₩39,879 |
| Recent 2Y ret / MDD | +17.6 % / 42.3 % | +10.5 % / 44.9 % | +6.7 % / 17.2 % | +5.4 % / 17.1 % |
| Recent 6M ret / MDD | +9.5 % / 2.2 % | +6.6 % / 3.5 % | +3.2 % / 1.4 % | +8.4 % / 2.2 % |
| cost 1.5× / 2× return | +44.9 / +36.1 % | +33.1 / +28.9 % | +81.5 / +71.5 % | +9.4 / +8.8 % |
| MDD-halt fired | 2017-11-30 (12.4 %) | 2017-12-06 (20.0 %) | 2018-01-11 (12.1 %) | 2017-11-06 (15.4 %) |
| gate | FAIL (MDD, crash) | FAIL (MDD, crash, inventory) | FAIL (MDD, crash, inventory) | FAIL (MDD, crash) |

**SELECTED_GRID = NONE → GRID_RESEARCH_FAILED.** No ETH shadow was started (§23 requires a passing candidate),
no ETH LIVE preparation, no parameter re-exploration (§22).

## Interpretation
- The failure is structural, not a tuning issue: a ₩25,000 ETH seed (50 % of the sleeve) plus up to ₩40,000
  inventory is a long ETH exposure. In every ETH drawdown > ~25 % (2017-11/2018, 2022, 2025) the sleeve MDD
  breaches 12 % regardless of gap; the daily-EMA100 guard only stops *new* buys, it does not protect the
  inventory already held. Even the gap-neutral metric (Recent 2Y MDD 17–45 %) confirms it.
- The 12 % MDD halt fired within the first 2–4 months of the 9-year run for every candidate, so the full-period
  returns are mostly "frozen inventory riding the 2020–21 bull" rather than grid income — not a valid basis
  for a LIVE decision. Only the recent 6-month sideways window looks benign (+3–10 %, MDD 1–3 %), which is
  exactly the regime a grid is designed for and exactly the one that cannot be relied on.
- Grid income itself is real but small: 100 % cycle win rate by construction, ₩31–84 net per cycle,
  fee/gross 6–14 %. The edge is dwarfed by inventory mark-to-market.
- Portfolio combination with D0 was not evaluated because no candidate passed the gate.

## What would be needed for a next round (NOT done — needs approval)
Inventory-side protection (an ETH inventory stop / max-loss per unit, or a far smaller seed), or restricting
the grid to a regime filter, are new independent variables — a new pre-registered round, not a tweak of G0–G3.
