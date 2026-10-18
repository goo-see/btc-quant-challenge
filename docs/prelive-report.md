# Pre-LIVE Validation 보고 — Candidate D (2026-09-25)

**실제 LIVE 주문 없음. `live_gate.enabled=false`, `live_approved=false` 유지. PAPER 승격은 BLOCKED(원금 미입금).**

## 1. Round 2 commit/tag
- `e69a749 feat: research round2 — 1h history 2017+, trend following D/E, concentration analysis` · tag **`research-round2`**
- 이번 단계 변경분(승인 상태 모델, 파라미터 동결, epoch/카운터, 1h warm-up, preflight 원금 검사, 외부 입출금 탐지, readiness 그룹화, 25 replay 시나리오, 재시작 검증 스크립트, 스탑 persist 버그 수정)은 **미커밋**.

## 2. Candidate 상태
`config/config.yaml live_gate.strategy_status`:
| version | research_approved | paper_approved | live_approved |
|---|---|---|---|
| trend_following_4h_v1 (**D**) | **true** | false | false |
| trend_following_1d_v1 (E) | false | false | false |
| trend_momentum_v1 (baseline) | false | false | false |
`approved_strategies` 제거. `live_guard`는 `live_approved`만 읽음(테스트 `test_live_blocked_when_only_strategy_not_approved`).

## 3. D frozen parameter set — `config/strategies/trend_following_4h_v1.yaml`
| parameter | frozen | WF 선택 빈도(15 windows) | mode | median | 값별 평균 OOS(거래수) |
|---|---|---|---|---|---|
| entry_n | **30** | 20:2, 30:10, 42:3 | 30 | 30 | 20:+9.0%(15), 30:+8.6%(83), 42:+5.3%(21) |
| trend_ema (1d) | **100** | 50:6, 100:9 | 100 | 100 | 50:+10.8%(59), 100:+6.1%(60) |
| atr_stop_mult | **3.0** | 2.0:7, 3.0:8 | 3.0 | 3.0 | 2.0:+9.2%(67), 3.0:+6.9%(52) |
| exit_n | **20** | 10:7, 20:8 | 20 | 20 | 10:+8.5%(55), 20:+7.5%(64) |
| atr_period / atr_trail_mult / exit_on_trend_loss | 14 / 4.0 / 1 | 연구에서 고정 | — | — | — |
| timeframe / trend_timeframe | 4h / 1d | 고정 | — | — | — |
- 선정 근거: 마지막 윈도우 값·전체 최적값 사용 안 함. 차원별 mode=median으로 "선택 군집의 중심"을 택함(정확히 그 조합이 선택된 적은 없으나 이웃 (30,50,3,20)×3, (30,100,3,10)×2, (42,100,3,20)×2가 선택됨). 이웃 안정성 99%(70평가), cliff 0. trend_ema=50이 평균 OOS는 높지만 2019–21 강세 초기와 마지막 윈도우에 편중 → 다수 선택 100 채택.
- **고정 파라미터 재평가(선택 없이 같은 15 OOS 윈도우)**: stitched **+226.3%**, MDD **7.23%**, 105 trades, PF 4.06, 승률 44.8%, 기대값 ₩1,172, 수익 윈도우 **13/15**, ex-top3 +72.5%. WF 선택값 결과(+200%, 14/15)와 정합 → 선택 이득에 의존하지 않음. FULL 2018-05→2026-09: +134.2%, MDD 6.0%, 111 trades, PF 4.20, 노출 21.7%.

## 4. Private smoke / Challenge capital
| 단계 | 결과 |
|---|---|
| credentials | PASS |
| static_public_ip | pending(동일 IP 4.4h 관측, 24h 미달) — 읽기 전용 smoke 진행 |
| accounts | **PASS** — KRW available **₩10,039.50**, locked 0, BTC 0 (`bot_state.exchange_balance`) |
| orders/chance | **PASS** — bid/ask fee 0.0005, min_total ₩5,000, market **active** |
| orders/test (₩6,000 dry-run) | **PASS** — `state=wait`, 실주문 없음 |
| challenge_capital | **FAIL** — ₩10,039 vs 초기원금 ₩100,000(±500) → **`CHALLENGE_CAPITAL_NOT_FUNDED`** |
- `bot_state.challenge = {initial_capital: 100000, funded: false, status: NOT_STARTED}`. 입금·orders/test는 Challenge를 시작시키지 않음(첫 LIVE BUY FILLED 시에만 `STARTED`).
- 외부 입출금 탐지: LIVE Reconciler가 우리 체결로 설명되지 않는 KRW/BTC 차이를 `external_deposit_after_start` / `external_withdrawal_after_start`(+flows)에 기록(HALT 병행).

## 5. Static IP
- 203.0.113.10, 관측 8건, **span 4.4h**(06:57Z→11:20Z), 모두 동일. 24h 도달 시 `IP_STABILITY_MINIMUM_PASS(24h+ stable; not proof of ISP static IP)`. 변경 시 즉시 FAIL.

## 6. Strategy Epoch — **미변경(BLOCKED)**
- 사용자 조건 "Private smoke PASS 이후 승격"에서 `challenge_capital` 단계가 FAIL이므로 PAPER는 baseline `trend_momentum`(epoch 2026-09-25 16:23 KST) 유지. Infrastructure epoch(14:09 KST) 리셋 없음.
- 승격 준비 완료: `strategy.version_file: strategies/trend_following_4h_v1.yaml` 설정 후 `launchd_install.sh restart trader` → 새 epoch 기록(strategy_id/version/started_at/parameter_hash/snapshot/counters: bars_processed, signals, paper_orders/fills/trades, running_sec, critical_errors), 이전 epoch는 `strategy_epoch_history`에 보존.
- 4h/1d 전략용 warm-up: 1h 히스토리 400일 재생(1d EMA100·채널) → 1m 12일 → 라이브. 리플레이 시나리오 24/25에서 검증.

## 7. PAPER D runtime / processed 4h bars / natural signals·trades
- **0 / 0 / 0** — D는 아직 PAPER에 투입되지 않음(위 BLOCKED). 자연 신호·거래는 강제하지 않으며 없으면 없다고 보고.
- baseline PAPER(참고): infra runtime **6.4h/48h**, 시그널 HOLD만, 거래 0.

## 8. Replay / synthetic E2E — **25/25 PASS** (`scripts/replay_e2e.py` → `bot_state.replay_e2e`)
실제 PAPER 파이프라인(TraderService + PaperGateway + Risk + OrderManager + Reconciler + 임시 SQLite)에 합성 1m 경로를 재생. 23개는 D 클래스를 15m/1h 버킷으로(로직 동일), 2개는 실제 4h/1d 버킷.
| # | 시나리오 | 결과 |
|---|---|---|
| 01 | warm-up HOLD only | 39 HOLD, 0 orders |
| 02 | breakout → BUY → risk APPROVED → order → fill → position | ₩30,000, stop = close − 2×ATR |
| 03 | 포지션 중 재돌파 → 두 번째 BUY 없음 | orders 1 |
| 04 | ATR stop (ticker) | STOP@ 청산, 손실 기록, 연패 카운트 |
| 05 | 트레일링 스탑 ratchet(상향만) | 102.9M→109.2M, 트레일 청산 +₩1,551 |
| 06 | 채널 하단 이탈 청산 | CHANNEL_EXIT |
| 07 | 추세 상실 청산(1h EMA 하향) | TREND_LOSS_EXIT |
| 08 | 갭 하락으로 스탑 관통 | 스탑가 아닌 갭 가격 체결 |
| 09 | 동일 바 stop/high 동시 → STOP 우선(저가 틱 선처리) | STOP |
| 10 | 포지션 보유 중 재시작 | 장부/스탑/원장/trade id 복원 → 스탑 청산 → reconcile OK |
| 11 | 포지션 보유 reconcile | OK |
| 12 | reconcile 불일치 → HALTED, 이후 돌파에도 주문 없음 | ✓ |
| 13 | 중복 주문 방지(같은 client id 1회 호출) + 롱 중 BUY RISK_REJECTED | ✓ |
| 14 | daily loss limit → 거부 + HALTED | ✓ |
| 15 | PAUSED 중 BUY 거부 | ✓ |
| 16 | MAINTENANCE 중 주문 없음 → 자동 복구 → 다음 돌파 체결 | ✓ |
| 17 | KILL + 포지션: 스탑 관통에도 매도 없음(거래중지) | ✓ |
| 18 | 거래소 거부(market_offline) → REJECTED 기록, 장부 불변, 다음 돌파 1회 체결 | ✓ |
| 19 | 전송 후 네트워크 오류 → UNKNOWN → identifier 조회로 FILLED, 체결 1회 | ✓ |
| 20 | 소액 equity → MIN_ORDER 거부 | ✓ |
| 21 | 매도 부분체결→취소, 잔량은 다음 틱에 청산, 장부==원장 | ✓ |
| 22 | 손실 후 쿨다운 재진입 거부 | ✓ |
| 23 | 시간당 주문 한도(SELL 허용, BUY 차단) | ✓ |
| 24 | 실제 4h/1d 버킷: 1h 히스토리 warm-up → 4h 마감 돌파 매수 | 4h bars 2 |
| 25 | 실제 4h/1d: 재시작 후 1h+1m 히스토리로 재워밍, epoch 보존 | ✓ |
- **시나리오 10이 실제 결함 발견**: 트레일링 스탑 상향이 메모리에서만 갱신되어 재시작 시 오래된(낮은) 스탑이 복원됨 → `update_stop` 변경 시 즉시 persist로 수정.

## 9. Restart recovery (실 운영, launchd)
- `scripts/restart_recovery_check.sh`: `kill -9 59456` → launchd 재기동 pid **61882**, 상태 RUNNING 유지, heartbeat 3.4s, reconcile OK → `bot_state.restart_recovery.ok=true`.

## 10. Reconciliation
- 실 PAPER 누적 **99회 전부 OK**, 리플레이 11/12/21에서 OK·불일치·부분체결 경로 검증.

## 11. LIVE readiness (그룹화, 대시보드 반영)
```
[Research]        FAIL      (활성 전략이 baseline이라 FAIL — D 승격 시 PASS)
[Infrastructure]  PROGRESS  runtime 6.4/48h · critical 0 · reconcile PASS · restart PASS · static IP PROGRESS(4.4h)
[Private API]     FAIL      credentials PASS · accounts PASS · chance PASS · orders/test PASS · challenge capital FAIL
[Strategy PAPER]  FAIL      version trend_momentum · runtime 0/72h · bars 0 · signals 0 · trades 0/10 · replay E2E PASS 25/25 · paper approval NO
[LIVE]            BLOCKED   live_gate.enabled false · LIVE approval NO · challenge NOT_STARTED
LIVE BLOCKED — STRATEGY_NOT_APPROVED
```

## 12. 품질 게이트
`ruff` ✅ · `mypy --strict` ✅ 118 files · `pytest` ✅ **204 passed** (3m34s, 25 replay 포함) · `npm run build` ✅

## 13. 발견된 문제
1. **[수정] 트레일링 스탑 persist 누락** — 재시작 시 스탑 후퇴(시나리오 10에서 발견).
2. Upbit 계좌 잔고 ₩10,039 — 원금 ₩100,000 미입금 → PAPER 승격 BLOCKED(사용자 절차 준수).
3. 셸 cwd가 예상과 달라 스크립트 경로 실행이 한 번 실패 → 절대 경로 사용.
4. Readiness "Research" 그룹은 **활성** 전략 기준이라 baseline 실행 중엔 FAIL로 표시됨 — D 승격 시 자동 PASS(의도된 동작이나 혼동 여지, UI 설명 필요).
5. 리플레이 시나리오 대부분이 15m/1h 버킷 사용(속도) — 실제 4h/1d 버킷은 2개. 4h/1d에서의 장기 상태(채널 30봉=5일) 검증은 PAPER 운영에서 자연 관측.

## 14. 실제 LIVE 전 남은 blocker (순서)
1. **₩100,000 입금** → `private_smoke.py` → `challenge_capital PASS`
2. 사용자 승인 → D PAPER 승격(`version_file` 설정 + trader 재기동) → 새 Strategy Epoch
3. Strategy PAPER 게이트: D runtime ≥72h, 4h bar 처리·시그널·사이징·스탑 상태·재시작 복원 정상(자연 신호/거래는 있는 그대로 보고)
4. Infrastructure: runtime ≥48h(현재 6.4h), static IP 24h(현재 4.4h), critical 0 유지
5. 사용자 `paper_approved=true` 결정 → 이후 별도 명시 승인으로 `live_approved=true` + `live_gate.enabled=true` + `LIVE_CONFIRM` → Small LIVE(₩6,000 cap) 단계
6. 한계 재확인: D 연환산 ≈16%(₩100k 기준), ₩1M 목표는 비현실적 — 콘텐츠 목표로만 유지
