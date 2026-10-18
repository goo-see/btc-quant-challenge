# ROUND3_INITIALIZED — Research Round 3 (BTC Trend Following Timeframe & Breakout Horizon Study)

- **Mode**: INITIALIZE (no Round 3 config/DB/launchd existed at 2026-09-27 00:10 KST; before 2026-09-29 23:00 KST)
- **Start**: experiment `started_at` = **2026-09-27 00:00 KST** (2026-09-26T15:00:00Z); runner started 00:33:37 KST
- **Experiment ID**: `round3_tf_horizon_20260927` · config hash `22e3da0429ed6fcc` · git HEAD 9245f43 (paper-d-validation)

## 1. State snapshot before initialisation (`docs/research/round3_snapshot_2026-09-27.json`)
D0 `trend_following_4h_v1` hash `c0e59de4f7b70c0c`, epoch 2026-09-25T14:09:28Z · bot RUNNING / PAPER /
ENTRY_AND_EXIT · paper_approved=false · live_approved=false · live_gate.enabled=false · TRADING_MODE=PAPER ·
D0 position 0 BTC / ₩100,000 · trader pid 64658, api 64582, watch 66729 · candles 1m → 2026-09-26 15:09Z,
1h → 14:00Z, 4h → 08:00Z, 1d → 2026-09-25 · git: 9 uncommitted files from the previous step (watcher/slippage/docs).
Reference values re-verified against artifacts: WF OOS +200.35% / 6.72% / 3.45 / 119 / 14 of 15 (round2_D.json) ✓ ·
Frozen 9Y +129.52% / 11.28% / 3.75 / 116 (backtest_runs.id=11) ✓ · Recent 2Y +9.39% / 4.07% / 2.34 (id=10) ✓.

## 2. Strategy hashes (parameter sha256[:16], same formula as the trader epoch)
| id | TF | horizon | entry/exit/ATR | version | hash |
|---|---|---|---|---|---|
| D0 | 4h | 5d | 30/20/14 | trend_following_4h_v1 | `c0e59de4f7b70c0c` (= live PAPER epoch hash) |
| D1 | 2h | 5d | 60/40/28 | trend_following_2h_r3_h5 | `36f1f2dbff0399f7` |
| D2 | 1h | 5d | 120/80/56 | trend_following_1h_r3_h5 | `afcb18bd77d1302f` |
| D3 | 2h | 3d | 36/24/28 | trend_following_2h_r3_h3 | `3e7d609bb6df6910` |
| D4 | 1h | 3d | 72/48/56 | trend_following_1h_r3_h3 | `991025f4c7beead3` |
| D5 | 2h | 2d | 24/16/28 | trend_following_2h_r3_h2 | `7875f76076f700f1` |
| D6 | 1h | 2d | 48/32/56 | trend_following_1h_r3_h2 | `f24be3b888549af3` |

## 3. Shadow process
- launchd `com.quant.shadow-round3` (RunAtLoad, KeepAlive, venv python `-m app.research.round3.runner`) — **pid 8523**, state running
- DB path: `data/research_round3.db` (WAL) — the only write target; log `logs/shadow_round3.log`
- Market DB `data/quant.db` opened `?mode=ro` (lsof: fd 3 **r**); research DB fd 4 **u**; **no TCP/UDP sockets**; no Claude/LLM/network code
- Replay on start: 9,585 1h bars (2025-08-22 → 2026-09-26 00:00Z) + 932 1m bars, 3.0 s; 7 virtual portfolios ₩100,000 each
- Tick every 60 s: new closed 1m bars → 7 engines → decisions/fills/trades/portfolio → forward tracking → D0 consistency → heartbeat
- Next evaluations (bar closes, +≈1 min for the next-1m-open virtual fill): D2/D4/D6 every hour (next 01:00 KST) · D1/D3/D5 every even
  UTC hour = odd KST hour (next 01:00 KST) · D0 replica at 01/05/09/13/17/21 KST (next 01:00 KST). First live decisions already
  recorded at 00:00 KST: D2/D4/D6 HOLD INSIDE_CHANNEL (close 113,971,000; uppers 117.60M / 116.78M / 115.92M; EMA100 103,229,834).

## 4. Historical summary (fixed params, 15 WF windows, determinism check IDENTICAL) — `docs/research/round3_results.md`
D0 +226.3% / MDD 7.23% / PF 4.06 / 105 trades / 13 of 15 · D1 +123.8% / 6.59% / 2.78 / 160 / 13 of 15 · D2 +79.6% / 7.48% / 1.97 / 236 / 9 of 15 ·
D3 +119.1% / 9.44% / 2.21 / 202 / 13 of 15 · D4 +79.2% / 7.99% / 1.75 / 286 / 9 of 15 · D5 +123.2% / 7.64% / 2.01 / 244 / 13 of 15 ·
D6 +77.9% / 8.52% / 1.63 / 341 / 9 of 15. All cost-stress positive, no concentration risk, Recent 2Y all positive → pre-registered gate:
**all seven PASS** (candidate status only). Interpretation (after results, `round3_note.md` Part C): faster TF / shorter horizon
lowers PF and expectancy and doubles the fake-breakout share; D1 is the only variant with a robustness argument (lower OOS MDD).
D0 Full 9Y reproduces the frozen artifact exactly (+129.52% / 11.28% / 3.75 / 116).

## 5. Tests
`ruff` clean · `mypy --strict` 134 files clean · **pytest 234 passed** (210 existing + 24 new): `test_round3_timeframe_2h` (2h member,
buckets, 1h→2h and 1m≡1h aggregation), `test_round3_config` (7 fixed hypotheses, D0 hash = frozen hash, ATR ≈ 56 h),
`test_round3_shadow_engine` (ShadowEngine ≡ BacktestEngine trade-for-trade for D0/D1/D2/D5, base switch 1h→1m consistency,
sizing cap), `test_round3_idempotency` (replay adds 0 rows, unique key, divergence logged not overwritten),
`test_round3_no_live_order` (AST: no exchange/OrderManager/PaperGateway/RiskManager/services/settings/database/network modules
reachable transitively; no order/secret identifiers; market DB handle rejects INSERT/UPDATE/DELETE; plist runs only the runner),
`test_round3_historical` (fixed WF ≡ registry WF, deterministic evaluate, gate classification).

## 6. D0 unaffected — evidence
- D0 path files unchanged (md5 = snapshot): `trend_following.py f21c27e1…`, `backtest/engine.py ec0de420…`, `trader_service.py 0c8d536b…`,
  `strategies/trend_following_4h_v1.yaml 72eed242…`; `config/config.yaml` untouched; registry untouched (D1–D6 are built directly).
- Only shared-code change: `Timeframe.H2 = "2h"` added to `core/types.py` (+ its minutes) — additive; existing members/values identical;
  D0 never subscribes 2h. `pyproject.toml` (+1 ruff per-file line-length ignore), `launchd_install.sh` (per-label install; core agents untouched).
- Live D0 after initialisation: epoch hash `c0e59de4f7b70c0c`, epoch start unchanged, paper_orders 0, orders table 2 rows (unchanged),
  portfolio ₩100,000 / 0 BTC, state RUNNING, trader pid 64658 unchanged; `com.quant.trader/api/watch` not restarted.
- The shadow runner cannot write `quant.db` (read-only URI, tested); its research DB is a separate file.
- Finding (report only, no change): the live D0 daily EMA100 (103,160,288.85) equals, to the cent, the EMA computed when the
  1h-stage warm-up **drops its last day** (2026-09-12) — the trader's staged warm-up never flushes the 1h-stage buckets at the
  boundary. Effect ≈0.06% on EMA100, none on channels/ATR, no decision changed. D0 code is frozen; fixing needs approval.

## 7. Real-order possibility: ZERO
No path from `app/research/round3` to `app.exchange.*`, `OrderManager`, `PaperGateway`, `RiskManager`, `app.services`, `app.config.settings`
(.env) or any network library (static transitive check in tests). The process holds no sockets. D1–D6 exist only as virtual portfolios in
`research_round3.db`. `live_approved=false`, `live_gate.enabled=false`, `TRADING_MODE=PAPER`, `paper_approved=false` unchanged.

## 8. Files (commit candidate — NOT committed)
New: `backend/app/research/{__init__,round3/{__init__,config,market_reader,shadow_engine,store,runner,historical}}.py`,
`backend/tests/unit/test_round3_*.py` (6), `config/research/round3.yaml`, `scripts/research_round3.py`,
`scripts/launchd/com.quant.shadow-round3.plist`, `docs/research/{round3_preregistration,round3_note,round3_results}.md`,
`docs/research/round3_D0..D6.json`, `docs/research/round3_snapshot_2026-09-27.json`, `docs/round3-report.md`.
Modified: `backend/app/core/types.py` (H2), `backend/pyproject.toml`, `scripts/launchd_install.sh`, `docs/00-architecture.md` (§19).
Runtime artefacts (not for commit): `data/research_round3.db*`, `logs/shadow_round3.log`, `logs/launchd_shadow_round3.*.log`.
Suggested message: `feat: research round3 — timeframe/horizon shadow study (D1–D6), fixed-param WF, isolated launchd runner`.
