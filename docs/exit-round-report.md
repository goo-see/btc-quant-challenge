# D0 EXIT RESEARCH RESULT — 2026-10-03 01:12 KST

LIVE safety: trader pid **66796** before and after (LIVE / RUNNING / ENTRY_AND_EXIT); open position
0.00025795 BTC @ 116,300,000, stop 113,713,838 unchanged; exchange KRW 70,042.91 / BTC 0.00025795,
open orders 0; reconciliation OK (16:06Z → 16:11Z); orders table 3 rows before/after; production
hashes unchanged (trend_following.py f21c27e1…, frozen yaml 72eed242…, risk_manager, order_manager).
Research code lives only in `backend/app/research/exit_round/` + `scripts/research_exit_round.py`;
artifacts in `docs/research/exit_round_*`. No production module, DB, yaml or LIVE state touched.

Full tables: `docs/research/exit_round_results.md`; design/interpretation: `docs/research/exit_round_note.md`.

| | E0 (control) | E1 break-even | E2 3×ATR | E3 partial 50%@2R | E4 lock 0.5R@1.5R |
|---|---|---|---|---|---|
| WF OOS return | +226.3% | +238.3% | +136.5% | +138.4% | +230.4% |
| OOS MDD | 7.23% | 7.02% | 6.77% | 6.46% | 7.23% |
| OOS PF | 4.06 | 4.56 | 2.95 | 3.17 | 4.13 |
| expectancy | ₩1,172 | ₩1,213 | ₩672 | ₩832 | ₩1,178 |
| P→L (MFE>0) / meaningful ≥0.5R | 63 / 32 | 50 / 19 | 72 / 29 | 63 / 32 | 64 / 31 |
| +1R→loss / +2R→loss | 13 / 0 | 0 / 0 | 4 / 0 | 13 / 0 | 12 / 0 |
| recent 2Y | +19.5% PF 3.55 | +19.5% PF 3.55 | +12.5% PF 2.15 | +17.4% PF 3.24 | +17.3% PF 2.85 |
| WF windows | 13/15 | 13/15 | 13/15 | 13/15 | 13/15 |
| cost 2.0× OOS | +211% | +230% | +122% | +126% | +215% |
| winner capture (top-10 ΔPnL) | — | 1.00 (₩0) | 0.49 (−₩70k, 10/10 cut) | 0.82 (−₩39k) | 1.00 (+₩43) |
| verdict | control | **PROMISING** | REJECT | REJECT | PROMISING (marginal) |

Recommended next step: E1 as a D0 v2 exit-rule candidate → shadow/PAPER validation (separate
version file, separate epoch) before any approval. LIVE D0 v1 modification: **NO**.
