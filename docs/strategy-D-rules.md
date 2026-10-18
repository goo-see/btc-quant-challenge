# Strategy D — `trend_following_4h_v1` 의사결정 규칙 (코드 기준, 2026-09-25)

근거 파일: `backend/app/strategy/trend_following.py`(전략) · `app/market/candle_engine.py`(버킷) ·
`app/market/indicators.py`(EMA/ATR) · `app/services/trader_service.py`(실시간 파이프라인, 틱 스탑) ·
`app/portfolio/portfolio_manager.py`(스탑 ratchet/persist) · `app/risk/risk_manager.py`(주문 승인) ·
`config/strategies/trend_following_4h_v1.yaml`(동결 파라미터).

동결 파라미터: `entry_n=30, trend_ema=100, exit_n=20, atr_period=14, atr_stop_mult=3.0, atr_trail_mult=4.0, exit_on_trend_loss=1`, 전략 TF **4h**, 추세 TF **1d**.

## 0. 데이터·지표 정의 (코드)
- 버킷: `bucket_start(ts, tf) = epoch + floor((ts−epoch)/tf)·tf` → **UTC 정각 기준**. 4h 바 = UTC 00/04/08/12/16/20 시작, 1d 바 = UTC 00:00 시작.
- 4h/1d 바는 1m 캔들에서 `CandleAggregator`가 합성. 바는 **다음 버킷의 첫 1m 캔들이 도착할 때 "closed"** 로 확정된다(마지막 1m은 `Live1mTracker`가 다음 1m 메시지를 보고 닫음).
- `daily_ema_100` = 1d 종가의 EMA(100), k=2/101, 첫 100개는 SMA로 시드. `daily_close` = **마지막으로 닫힌 1d 바의 종가**(현재 진행 중인 날의 가격이 아님).
- `atr14_4h` = 4h 바의 Wilder ATR(14): TR=max(H−L, |H−prevC|, |L−prevC|), 첫 14개 평균 후 `ATR=(ATR·13+TR)/14`.
- `channel_upper` = **직전 30개 닫힌 4h 바의 최고가(high) 최댓값** — 현재 바 **제외**(`upper = max(_highs)`를 계산한 뒤에 현재 바를 append).
- `channel_lower` = **직전 20개 닫힌 4h 바의 최저가(low) 최솟값** — 현재 바 제외.
- `highest_since_entry` = 진입 이후 **틱(ticker) 기준 최고 체결가**(`PortfolioState.mark(price)`가 매 틱 갱신; 시작값은 체결가).

## A. BUY 규칙 (실제 코드 순서)
```
# 전략 (on_candle, 4h 바가 닫힐 때만 호출; 1d 바는 지표만 갱신하고 None 반환)
warmup_ok  = atr14_4h 준비 AND 직전 30개 4h high 확보 AND 직전 20개 4h low 확보 AND daily_ema_100 준비
trend_ok   = daily_close > daily_ema_100          # 마지막 닫힌 1d 바 기준 (UTC 00:00에 갱신)
breakout   = close_4h > max(high of previous 30 closed 4h bars)   # 현재 바 제외, close 기준, 4h 종가 확정 시점에만
no_pos     = ctx.position is None                 # 포지션 보유 중이면 BUY 신호 자체를 만들지 않음 (추가 매수 없음)
BUY_signal = warmup_ok AND no_pos AND trend_ok AND breakout
stop_hint  = close_4h − 3.0 × atr14_4h            # 신호에 실려 체결 시 초기 스탑이 됨
```
- 판단 시점: **4h 바 종가 확정 시점에만**. 바 진행 중 고가가 채널 위를 찍어도(intra-bar) 종가가 아니면 무시.
- 1d 바와 4h 바가 동시에 닫히는 UTC 00:00에는 1d를 먼저 처리(`closed.sort(key=-minutes)`)하므로 그 순간의 4h 판단은 방금 닫힌 일봉의 새 EMA/종가를 사용한다.

BUY 신호가 나면 **RiskManager**가 아래 순서로 검사(하나라도 걸리면 `RISK_REJECTED`, 주문 없음):
```
1  bot_state == HALTED                         → KILL_SWITCH
2  bot_state != RUNNING (PAUSED/MAINTENANCE)   → BOT_STATE_*
3  UNKNOWN 상태 주문 존재                       → UNKNOWN_ORDER_PENDING
4  in-flight 주문 존재                          → INFLIGHT_ORDER
5  daily_pnl ≤ −3% × day_start_equity           → DAILY_LOSS_LIMIT  (+Kill Switch)
6  drawdown ≥ 15% (from HWM)                    → MAX_DRAWDOWN      (+Kill Switch)
7  consecutive_losses ≥ 4                       → MAX_CONSECUTIVE_LOSSES
8  마지막 손실 후 60분 미경과                    → COOLDOWN
9  최근 1시간 주문 ≥ 4                          → ORDERS_PER_HOUR
10 이미 포지션 보유                              → POSITION_STATE_ALREADY_LONG
11 동시 포지션 ≥ 1                               → MAX_CONCURRENT_POSITIONS
12 order_krw = min(equity×0.30, 30,000, (krw−1,000)/(1+fee)) 정수화; < 5,000 → MIN_ORDER
13 잔여 KRW < 1,000                             → MIN_BALANCE
APPROVED → OrderManager: 시장가 매수 ord_type=price, price=order_krw (identifier=client_order_id)
```
- 체결 즉시 `PortfolioManager.apply_order(stop=stop_hint)` → `stop_price = close_4h − 3×ATR` 로 저장(DB persist).
- 실시간 체결 시점: 4h 종가 확정 직후 **즉시 시장가**(PAPER: 최신 ticker±3bps; 백테스트는 다음 base bar 시가). 백테스트보다 수 초~수 분 이른 가격이다.

## B. SELL 규칙 (포지션 보유 시)
### 1. 초기 ATR 스탑
- `stop = close_4h(신호 바) − 3.0 × ATR14(4h)`; ATR은 4h 바 기준. 체결가가 아니라 **신호 바 종가** 기준(체결가는 보통 그보다 약간 위/아래).
- 집행: `on_tick` — **매 ticker(체결) 메시지마다** `price ≤ stop_price` 이면 즉시 `SELL` 신호(reason `STOP@...`) → Risk → 시장가 매도(전량). 4h 종가를 기다리지 않는다.

### 2. 트레일링 스탑
```
trail    = highest_since_entry(틱 최고가) − 4.0 × ATR14(4h)     # 4h 바 종가 확정 시점에 계산
new_stop = max(current_stop, trail)                              # 위로만 이동
```
- 계산·갱신 시점: **4h 바가 닫힐 때만**(HOLD/SELL 신호의 `stop_price`에 실어 보냄 → `PortfolioManager.update_stop`이 ratchet). 틱마다 재계산하지 않는다(단 스탑 **집행**은 틱마다).
- 저장: 값이 바뀔 때 즉시 `bot_state.portfolio`에 persist(오늘 수정된 결함). 재시작 시 `PortfolioManager.load()`로 복원되고, warm-up 리플레이 중에는 갱신하지 않으므로 복원값이 유지된다(리플레이 시나리오 10).

### 3. 채널 청산
- `close_4h < min(low of previous 20 closed 4h bars)` — 현재 바 제외, **종가 기준**(저가가 아니라). 4h 종가 확정 시점에만.

### 4. 추세 상실 청산
- `exit_on_trend_loss=1`: 4h 바가 닫힐 때 `daily_close ≤ daily_ema_100` 이면 SELL(`TREND_LOSS_EXIT`).
- 1d 바는 UTC 00:00(KST 09:00)에만 닫히므로, 추세 상실은 사실상 **KST 09:00의 4h 종가 확정 시점**에 발동(그 시각에 1d→4h 순으로 처리). 즉시 시장가.

### 5. 충돌·우선순위
- 전략 `on_candle` 내부 순서: **채널 청산 → 추세 상실 → HOLD(트레일 갱신)**. 둘 다 참이면 reason은 `CHANNEL_EXIT`.
- 틱 스탑 vs 4h 종가 청산: 실시간에서는 **먼저 도착한 이벤트가 전량 매도**하고, 뒤의 신호는 포지션이 없어 `RISK_REJECTED(POSITION_STATE_NO_POSITION)`으로 기록된다. 명시적 STOP-first 정렬은 **백테스트 엔진에만** 구현(같은 base bar에서 스탑 우선, 갭은 시가 체결). 실시간은 틱이 시간순으로 오므로 스탑이 4h 종가 처리보다 먼저 확인되는 것이 자연스러운 순서다(리플레이 09가 이를 검증). ⚠ 제안 1 참조.
- SELL은 RiskManager에서 연패/쿨다운/시간당 한도에 막히지 않는다. 단 `DAILY_LOSS_LIMIT`/`MAX_DRAWDOWN`은 SELL도 거부하고 HALT한다(포지션 유지, 수동 판단). ⚠ 제안 2 참조.

## C. HOLD 규칙
| 상황 | 반환 | reason |
|---|---|---|
| 지표/채널 미준비(ATR14, 30·20봉 채널, 1d EMA100 중 하나라도) | HOLD | `WARMUP` |
| 포지션 없음 + `daily_close ≤ EMA100` | HOLD | `TREND_DOWN` |
| 포지션 없음 + 추세 OK + `close_4h ≤ channel_upper` | HOLD | `INSIDE_CHANNEL` |
| 포지션 보유 + 채널/추세상실 미충족 | HOLD(+새 trail) | `IN_POSITION_TRAIL` |
| 4h 종가 확정이 아닌 시점(틱, 1m/1h 마감) | 전략 호출 없음(`on_candle`은 4h 아닌 바에 `None`) | — |
| MAINTENANCE / PAUSED / HALTED | 전략은 정상적으로 신호를 만들지만 BUY/SELL은 Risk가 `BOT_STATE_*`/`KILL_SWITCH`로 거부; 틱 스탑도 `trading_allowed`가 아니면 실행 안 함 | RISK_REJECTED |
| Risk reject | 신호 테이블에는 **BUY/SELL**로 기록, `risk_decisions`에 거부 사유, 이벤트 `RISK_REJECTED`; UI "Recent signals"에 `BUY` + 빨간 거부 사유로 표시 | — |

## D. 판단 주기
- Market data: WebSocket `ticker`(체결마다, 초당 수 회) + `candle.1m`(체결이 있는 초마다 현재 1m 바 갱신). 1m 바 확정 = 다음 1m 첫 메시지 도착 시.
- 1h 바: 실시간에서는 1m→`CandleAggregator`(base 1m)로 합성해 `candles` 테이블에 저장(이번 수정으로 상시). 과거는 REST 60m 캔들(2017-09~)이며 warm-up 시 tail을 REST로 보충.
- 4h 바 마감(UTC 00/04/08/12/16/20) = **KST 09:00 / 13:00 / 17:00 / 21:00 / 01:00 / 05:00**. 1d 마감 = UTC 00:00 = **KST 09:00**.
- 판단: BUY/채널·추세 SELL/트레일 갱신은 **4h 마감마다(하루 6회)**; **스탑 집행은 매 틱**; 1h 마감·1m 마감에는 판단 없음(저장만); 1d 마감 시 별도 판단은 없고 그 직후 4h 판단이 새 일봉 정보를 쓴다.
- 실제 이벤트 시각은 경계 직후 첫 1m 캔들 메시지 도착(보통 +1~10초). 무체결 구간이면 그만큼 지연.

## E. 현재 상태 (2026-09-25 20:35 KST, 저장된 실데이터로 동결 파라미터를 재생해 계산; **D는 아직 PAPER 미투입**, 실행 중인 PAPER는 baseline)
| 항목 | 값 |
|---|---|
| current BTC price (ticker) | ₩114,870,000 (20:35:36 KST) |
| latest 4h close (17:00 KST 마감, 04:00 UTC 바) | ₩114,720,000 |
| latest 1d close (09-24 UTC 일봉, 09-25 09:00 KST 마감) | ₩115,122,000 |
| daily EMA100 | ₩102,735,548 |
| trend filter | **PASS** (115,122,000 > 102,735,548) |
| Donchian upper (직전 30개 4h 고가 최댓값) | **₩117,000,000** |
| exit channel lower (직전 20개 4h 저가 최솟값) | ₩114,069,000 |
| ATR14 (4h) | ₩1,162,999 |
| breakout | **FAIL** (114,720,000 ≤ 117,000,000; +1.99% 필요) |
| current position / stop / trailing | 없음 / — / — |
| exit conditions | 해당 없음(포지션 없음) |
| **final action** | **HOLD — `INSIDE_CHANNEL`** |
| 다음 판단 | 21:00 KST 4h 마감. 그 바의 **종가 > ₩117,000,000** 이고 일봉 추세 유지 시 BUY 후보(수식상 초기 스탑 = 종가 − ₩3,488,997) |
(직전 30개 4h 고가의 최댓값은 이후 바가 닫히며 바뀔 수 있음 — 이는 고정 규칙의 현재 임계값이지 예측이 아님.)

## F. Dashboard "Why HOLD/BUY/SELL" 패널 제안 (미구현, 제안)
- API `GET /api/decision`: 트레이더가 4h 마감마다 `strategy_epoch.decision`에 저장한 스냅샷을 반환 — `action, reason, trend_ok(daily_close, ema100), channel_upper, channel_lower, atr, position(stop, trail, highest), next_4h_close_kst, thresholds`.
- Trading 탭 상단 카드:
```
Current Decision: HOLD (INSIDE_CHANNEL)   as of 17:00 KST 4h close · next 21:00 KST
✓ Daily trend   115,122,000 > EMA100 102,735,548
✗ 30-bar breakout   4h close 114,720,000 ≤ 117,000,000  (needs +1.99%)
✓ No position
✓ Risk pipeline RUNNING (no in-flight/unknown, daily −0.00%, dd 0.00%)
Trigger (rule, not forecast): BUY candidate if the 21:00 KST 4h bar closes above ₩117,000,000 → initial stop = close − 3×ATR(₩3.49M)
```
포지션 보유 시:
```
Current Decision: HOLD (IN_POSITION_TRAIL)
Exit rules:   ATR/Trailing stop ₩… (tick-executed)  |  20-bar channel exit: 4h close < ₩…  |  Trend EMA100: PASS (daily close ₩… vs ₩…)
```
- 표현 원칙: 예측 문구 없이 "고정 규칙의 현재 임계값"만. 값은 4h 마감 시점 스냅샷이므로 "as of" 시각을 항상 표기.

## 제안(코드 수정은 승인 후)
1. **동시 청산 신호 정리**: 틱 스탑 매도 직후 같은 4h 마감의 채널/추세 SELL이 `RISK_REJECTED(POSITION_STATE_NO_POSITION)`로 남아 이벤트 로그가 지저분함 → 전략 호출 전에 포지션이 없으면 SELL 계열 신호를 `HOLD(ALREADY_FLAT)`로 낮추거나, Risk 단계에서 "informational" 레벨로 기록.
2. **DAILY_LOSS/MAX_DD 시 SELL 거부**: 현재 정책은 HALT 후 포지션 유지(수동). 저빈도 전략에서 스탑까지 막히면 손실이 커질 수 있음 → "HALT 상태에서도 보호성 스탑 매도는 허용" 옵션을 검토(정책 결정 필요, 기본값은 현행 유지).
3. **트레일링 갱신 주기**: 트레일은 4h 마감에만 계산되고 틱 최고가를 쓴다 → 바 도중 급등 후 급락 시 마감 전에는 초기/이전 스탑만 유효. 연구 결과와 동일한 동작이므로 유지 권장(변경 시 재검증 필요).
4. **BUY 체결 타이밍 차이**: 실시간은 4h 마감 직후 즉시 시장가, 백테스트는 다음 1h 시가 → PAPER 관측으로 슬리피지/차이를 기록해 비교(수정 아님, 관측 항목 추가).
5. **1d EMA 시드**: 첫 100일 SMA 시드는 warm-up 400일로 충분하나, 1h 히스토리 결측(99.83%)이 4h/1d 합성에 미치는 영향은 무시 가능 — 결측 구간은 바가 없을 뿐 값이 섞이지 않음.


## 부록 — 평가 방식별 지표 (서로 다른 방식이므로 하나로 덮어쓰지 않음, 2026-09-25 승인 기록)
| 평가 방식 | 기간·기준 | Return | **MDD** | Trades | PF | 비고 |
|---|---|---|---|---|---|---|
| Walk-Forward OOS (train 선택→test, 윈도우별 ₩100k 리셋, stitched) | 2019-03→2026-08, 15 windows | +200.35% | **6.72%** | 119 | 3.45 | Research 승인 근거 |
| Frozen 고정 파라미터, 동일 15 OOS 윈도우(선택 없음) | 동일 | +226.3% | 7.23% | 105 | 4.06 | 선택 이득 무의존 확인 |
| Frozen Full-period 9Y (복리, warm-up prefix 없음, 1h base) | 2017-09→2026-09 | +129.5% | **11.3%** | 116 | 3.75 | ⚠ Small LIVE 전 Risk Review **warning** 항목(자동 승인 취소 아님) |
| Recent 2Y 1m base | 2024-08→2026-09 | +9.4% | **4.1%** | 20 | 2.34 | 최근 국면 참고 |
- Initial stop 로직(신호 4h 종가 − 3×ATR14)은 v1에서 변경하지 않음.
- Small LIVE 측정 준비: `app/monitoring/slippage.py` — signal_price / actual_fill_price / slippage_bps / initial_stop / stop_distance_from_signal / stop_distance_from_fill (트레이더 배선은 24h 검증 종료 후, 변경 보고 뒤 적용).


## KNOWN_D0_V1_LIMITATION — staged warm-up boundary flush (recorded 2026-09-29)

`TraderService._warmup` replays 1h history through a 1h-based aggregator up to the UTC-midnight
boundary before the 1m window, then switches to the 1m aggregator. The 1d/4h buckets that END
exactly at that boundary are never flushed, so the daily bar of the day before the boundary is
missing from the EMA100 seed after every process start. Measured effect: daily EMA100 ≈ +0.06–0.07%
vs full data (decays by (1 − 2/101) per day); Donchian channels, ATR and stops are unaffected.
Over 3,192 daily bars a shift of this size would have flipped `trend_ok` on 8 days (0.25%); during
Round 3 (18 D0 evaluations) 0 decisions differed from the full-data shadow replica.
Status: **KNOWN_D0_V1_LIMITATION** — D0 v1 frozen behaviour is kept as-is for LIVE. The fix is a
D0 v2 change: implement, re-run the historical evaluation and the PAPER validation before promotion.
