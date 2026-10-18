# Strategy D PAPER 승격 보고 — 2026-09-25 23:09 KST

**실제 Upbit 주문 없음. `live_approved=false`, `live_gate.enabled=false` 유지.**

## 1. Git
- `b669bbc feat: pre-live validation — approval states, frozen D params, 24h paper gate, replay e2e, capital gate` · tag **`prelive-validation`**
- 이후 변경분(실행 정책, signal/execution 분리, decision snapshot, D 승격 설정, 리플레이 26–28, 테스트) 17개 경로 **미커밋**.

## 2. D PAPER 승격 결과
| 항목 | 값 |
|---|---|
| Strategy epoch | `trend_following_4h_v1` · started **2026-09-25 14:09:28Z (23:09 KST)** · parameter_hash `c0e59de4f7b70c0c` (= frozen 파일 해시) |
| 이전 epoch | `trend_momentum_v1`(16:23 KST~) → `strategy_epoch_history`에 보관, 그 기간 기록(자연 거래 1건 −₩205 등) 보존 |
| Infrastructure epoch | 유지 (05:09Z / 14:09 KST 시작, RUNNING 누적 9.4h) |
| PAPER 가상 원금 | 장부·원장 **₩100,000**으로 리셋(`paper_books_reset`) |
| Warm-up | 1h 9,585봉 + 1m 18,099봉 → 4h/1d 지표 ready → 복구 체크 6/6 → RUNNING |
| 승인 상태 | research_approved **true** / paper_approved false / live_approved false / live_gate.enabled false |
| 24h operational validation | 23:09 KST 시작 → **2026-09-26 23:09 KST** 이후 `paper_approval_ready` 판정 |

## 3. Initial stop 기준 검증 (전 모드)
| 경로 | 기준 |
|---|---|
| Backtest (`engine.py`: `stop_price = pending.stop_price`) | **신호 4h 종가 − 3×ATR14(4h)** |
| Replay / PAPER (`trader_service._execute` → `apply_order(stop=sig.stop_price)`) | 동일 |
| LIVE (같은 `TraderService` 경로, `LiveGateway`만 교체) | 동일 |
- **동일하며 fill price 기준이 아님.** 9년 연구도 같은 기준 → 변경하지 않음. 체결가 기준으로 바꾸려면 연구 재실행 필요(시장가 체결은 신호 종가 대비 수 bp~수 십 bp 차이라 리스크 차이는 작지만 규칙 일관성을 위해 현행 유지 권고). PAPER 재시작/리셋 불필요.

## 4. Risk state 정책 — 구현 (기존 4-state machine과 충돌 없음, 파생 정책으로 계층화)
`ExecutionMode = execution_mode(state, reason)`:
| state / reason | ExecutionMode | Entry | 자동 Exit |
|---|---|---|---|
| RUNNING | ENTRY_AND_EXIT | 허용 | 허용 |
| PAUSED (사용자 Pause) | **EXIT_ONLY** | 금지 `EXIT_ONLY_NO_NEW_ENTRY` | 허용 |
| HALTED · DAILY_LOSS_LIMIT / MAX_DRAWDOWN | **EXIT_ONLY** | 금지 | 허용 (ATR/트레일 스탑, 채널, 추세상실) |
| MAINTENANCE (거래소 신뢰 불가) | **FROZEN** | 금지 | 금지 |
| HALTED · RECONCILE_FAILED / ORDER_UNKNOWN_UNRESOLVED / DATABASE_FAILURE / UNEXPLAINED_STALE* / API_ERRORS / MANUAL_KILL | **FROZEN** | 금지 | 금지 `FROZEN_NO_AUTO_EXECUTION` (수동 개입) |
- 보호성 SELL: `order_volume = ctx.btc`(보유량), 포지션 없으면 거부 → 절대 증가/초과 없음. 틱 스탑은 `exits_allowed`(≠FROZEN)에서 실행.
- 검증: 단위 12건(`test_risk_manager`, `test_state_machine`), 리플레이 **26**(손실한도 HALT 중 스탑 청산·재진입 없음) **27**(PAUSED 중 스탑 청산·수량≤보유) 통과.

## 5. Signal / Execution 분리
- 포지션이 없는데 발생한 SELL 신호(예: 틱 스탑 청산 뒤 같은 4h 마감의 채널/추세 SELL)는 Risk를 거치지 않고 `risk_decisions=SUPPRESSED_ALREADY_FLAT` 기록, `RISK_REJECTED` 이벤트 없음(리플레이 **28**).
- `/api/signals.risk.execution ∈ {EXECUTED, BLOCKED, SUPPRESSED}`, RISK_REJECTED 이벤트에 `signal/execution/execution_mode`. UI "Recent signals" 열: Signal | Execution(EXECUTED ₩… / BLOCKED · 사유 / SUPPRESSED).

## 6. Why BUY/SELL/HOLD — Decision snapshot
- 전략 TF 평가·틱 스탑마다 `bot_state.decision_PAPER` 저장(strategy_version, evaluated_at, market_bar_time, decision, reason, daily_close, daily_ema, trend_ok, close_4h, donchian_upper, breakout_ok, donchian_lower, atr, position_quantity, entry_price, initial_stop, current_stop, trailing_stop, channel_exit, trend_loss, risk_status, current_price). `GET /api/decision`(+`next_evaluation`).
- Trading 탭 상단 "Current decision" 카드: ✓/✗ 조건(일봉 추세, 30봉 돌파, 포지션, 리스크 상태), 4H close·돌파 임계값·ATR·청산 채널 / 포지션 시 ATR stop·Trailing·20봉 채널·EMA100. "as of · next evaluation" 표기, 예측 문구 없음. **첫 스냅샷은 승격 후 첫 4h 마감(2026-09-26 01:00 KST)에 생성**되며 그 전까지 "pending" 표시.

## 7. 24h PAPER gate 현황 (`/api/readiness` Strategy PAPER, 23:25 KST)
```
Strategy version      trend_following_4h_v1 (hash c0e59de4f7b70c0c)
Strategy runtime      0.25 / 24h  PROGRESS
Critical errors       0           PASS
Reconciliation        PASS (143/143 OK 누적)
WebSocket / REST / DB PASS
1h ingest             PASS (last 1h bar 1.3h ago; 1h 상시 합성·저장 추가)
4h / 1d aggregation   PROGRESS (첫 마감 01:00 / 09:00 KST 대기)
Indicator state       PASS (4h ready, 1d ready)
Frozen parameter hash PASS
Restart / recovery    PASS (epoch 이후: kill -9 64562 → pid 64658, RUNNING 유지, reconcile OK)
Replay E2E            PASS 28/28 (이 버전으로 기록)
Trailing persistence  PASS (scenario 10)
Natural signals/trades INFO 0 / 0
paper_approval_ready  false (runtime·집계 대기)  → PAPER approval NO (사람만 변경)
```
- Backtest evidence: 동결 파라미터 2y 1m 기반 +9.39% / PF 2.34 / MDD 4.07% / 20 trades(run 10), 9y 1h 기반 **+129.5% / PF 3.75 / MDD 11.28% / 116 trades**(run 11, 복리 전체 구간·warm-up prefix 없음 — 연구 OOS 7.2%는 윈도우별 ₩100k 리셋 기준이라 값이 다름).

## 8. Static IP
- 203.0.113.10 관측 15건 동일, span **7.3h**(06:57Z~14:14Z) → 24h 도달 예상 **2026-09-26 16:00 KST** 이후.

## 9. Private API / Challenge capital
- accounts PASS(₩10,039), chance PASS, orders/test PASS(dry-run), **challenge_capital FAIL**(₩10,039 ≠ ₩100,000±500) → LIVE blocker. Challenge `NOT_STARTED`.

## 10. 품질 게이트
`ruff` ✅ · `mypy --strict` ✅ 118 files · `pytest` ✅ **208 passed**(4m12s, 28 replay 포함) · `npm run build` ✅

## 11. 발견된 문제
1. config를 D로 바꾸자 baseline을 가정한 테스트 4건이 깨짐 → 테스트가 활성 전략과 무관하게 baseline을 고정하도록 수정.
2. 새 epoch의 PAPER 장부 리셋이 리플레이 20(소액 계좌)을 깨뜨림 → 시나리오 수정.
3. 4h/1d 전략 warm-up에서 1h 상시 합성 추가로 KeyError → 수정(리플레이 25 검출).
4. Decision snapshot은 4h 마감 시점에만 갱신 → 승격 직후 최대 4h 동안 카드가 비어 있음(의도된 동작, "pending" 표시).
5. `run_backtest.py --base 1h` 옵션 추가(9y 히스토리 직접 사용).

## 12. 다음 보고 트리거 (계속 실행)
- 첫 D 자연 BUY/SELL · critical error · Risk state 이벤트 · Static IP 24h(≈09-26 16:00 KST) · D PAPER 24h(09-26 23:09 KST) 완료 시 즉시 보고.
