# DAY 5 보고 — 전략 연구 (2026-09-25)

## 1. DAY 4 commit / tag
- `ae0bd9d feat: day4 launchd ops, upbit private gateway, static ip gate, daily report` · tag **`day4-ops`**
- DAY 5 변경분(26개 경로: 전략 A/B/C, 연구 하네스, epoch 분리, challenge start-date, IP pending 처리, zero-volume 가드, 테스트)은 **미커밋**

## 2. Static public IP observation
- `live_gate.static_public_ip = 203.0.113.10` 설정. 관측 4건(트레이더 1h 주기 + 수동), 전부 동일. span 0.4h → `STATIC_PUBLIC_IP_NOT_VERIFIED (observed 0.4h < 24h)`, 대시보드 **PROGRESS**.
- 24h 동일 시 판정 문구는 `IP_STABILITY_MINIMUM_PASS (24h+ stable; not proof of ISP static IP)` — ISP 고정 IP 단정 아님. 관측 IP가 하나라도 달라지면 즉시 FAIL(`observed [...] != configured`).

## 3. Private API smoke (키 입력 후, 실주문 없음)
| 단계 | 결과 |
|---|---|
| credentials | PASS (존재만 확인, 값 미출력) |
| static_public_ip | PENDING (동일 IP, 24h 미달 → 읽기 전용 smoke는 진행, LIVE는 차단 유지) |
| `GET /v1/accounts` | **PASS** — 1 asset, KRW 잔고 조회 성공(금액 미기록) → 키 권한·IP 화이트리스트 정상 |
| `GET /v1/orders/chance` | **PASS** — bid/ask fee 0.0005, min_total 5,000, market state active (→ `bot_state.exchange_terms`) |
| `POST /v1/orders/test` (₩6,000 dry-run) | **FAIL** — `400 insufficient_funds_bid` ("주문 가능한 금액(KRW)이 부족합니다") |
- 결론: 인증/권한/IP는 정상. 계좌 KRW가 ₩6,000 미만이어서 dry-run이 거부됨 → `bot_state.order_test = BLOCKED reason=ORDER_TEST_REJECTED_INSUFFICIENT_FUNDS_BID`. 실주문 엔드포인트는 호출하지 않음. ₩100,000 입금 후 `scripts/private_smoke.py` 재실행 시 통과 예상.
- `live_gate.enabled=false`, `approved_strategies=[]` 유지. 시크릿은 로그/DB/API/문서 어디에도 기록되지 않음(테스트로 검증).

## 4. A/B/C 전략 정의 (동일 `BaseStrategy` 인터페이스, Strategy→Risk→Order 구조 무변경)
| | A `trend_pullback` | B `donchian_breakout` | C `volatility_breakout` |
|---|---|---|---|
| 참조 TF | 1h EMA200 추세 필터 | 1h EMA200 (옵션) | 4h 레퍼런스 바 + 4h EMA200 (옵션) |
| 진입 (15m) | 추세 중 RSI < pullback_rsi & close > EMA slow → 무장(arm_bars) → close가 EMA fast 상향 재돌파 & RSI > rsi_exit_low | close > 직전 entry_n봉 최고가 | 15m close > 4h open + k×전 4h 범위 (4h 바당 1회) |
| 청산 | close < EMA slow, ATR stop/trail | close < 직전 exit_n봉 최저가, ATR stop/trail | hold_bars 4h 경과 시 시간 청산, ATR stop/trail |
| 고정 파라미터 | RSI14, ATR14, EMA fast 20, slow 50(그리드), arm 8, exit_low 45 | EMA/ATR 동일, exit_n 그리드 | k·hold·stop·filter 그리드 |

## 5. Parameter range (coarse grid, 각 24조합 — brute-force 아님)
- A: `pullback_rsi {35,40,45}` × `ema_slow {50,100}` × `atr_stop {2,3}` × `atr_trail {3,5}`
- B: `entry_n {20,40,80}` × `exit_n {10,20}` × `atr_stop {2,3}` × `use_trend_filter {0,1}`
- C: `k {0.4,0.5,0.6}` × `hold_bars {1,2}` × `atr_stop {1.5,2.5}` × `use_trend_filter {0,1}`
- 선택은 각 WF **train 구간에서만**(score = train return / max(MDD,1), ≥5 trades) → **test(OOS)에서 평가**. 전체 데이터 최적값 사용 없음.

## 6. 동일 조건
데이터 KRW-BTC 1m 2024-09-24→2026-09-25 (1,049,964봉) → 15m 70,019봉 · WF train 180d / test 60d, 60d 롤링, 9윈도우 · fee 0.05%/side, slippage 3bps · 사이징 equity 30% / cap ₩30,000 / min ₩5,000 / 1포지션 · 체결 다음 15m 시가 · STOP-first 인트라바 규칙 · 동일 metrics 코드. 세 전략에 예외 없음.

## 7. 결과 (`docs/research/day5_results.md`, `day5_{A,B,C}.json`)
| 지표 | A trend_pullback | B donchian_breakout | C volatility_breakout | Buy & Hold |
|---|---|---|---|---|
| FULL backtest (base params) return | -0.21% (2 trades) | -22.45% (484) | -24.83% (746) | **+35.75%** |
| **OOS return (WF stitched)** | **-2.71%** | **-17.54%** | **-15.40%** | +35.75% |
| OOS MDD | 6.30% | 19.89% | 15.68% | **50.85%** |
| OOS trades | 79 | 344 | 411 | — |
| OOS Profit Factor | 0.71 | 0.53 | 0.55 | — |
| OOS Win rate | 24.1% | 23.0% | 26.3% | — |
| OOS Expectancy | ₩-34 | ₩-57 | ₩-40 | — |
| OOS Fees | ₩2,370 | ₩10,206 | ₩12,227 | — |
| Exposure | 4.3% | 13.8% | 8.0% | 100% |
| 수익 OOS 윈도우 | 3/9 | 0/9 | 1/9 | — |

- A는 OOS MDD 6.3%로 B&H 50.9%보다 훨씬 낮은 낙폭이지만 기대값이 음수(첫 2윈도우 +0.83%/+1.96% 후 6윈도우 연속 손실). 선택 파라미터는 `pullback_rsi 40, ema_slow 100`이 7/9로 안정적이나 이웃 파라미터 양의 기대값 비율 **21%**, cliff 5윈도우 → 과적합 위험 표시.
- B/C는 모든 윈도우에서 train 자체가 손실(엣지 없음). 수수료가 손실의 큰 부분(B ₩10,206, C ₩12,227 vs 초기 ₩100,000).

## 8. Parameter stability
| | 이웃 평가 수 | 이웃 양의 기대값 비율 | cliff 윈도우 | 판정 |
|---|---|---|---|---|
| A | 43 | 21% | 5 | FAIL (고립된 최적점) |
| B | 39 | 0% | 0 | FAIL |
| C | 37 | 0% | 0 | FAIL |

## 9. Cost stress (OOS return / PF / MDD)
| | 1.0x | 1.5x | 2.0x |
|---|---|---|---|
| A | -2.71% / 0.71 / 6.3% | -4.54% / 0.57 / 7.7% | -6.50% / 0.47 / 9.0% |
| B | -17.54% / 0.53 / 19.9% | -22.44% / 0.45 / 24.2% | -26.21% / 0.40 / 27.7% |
| C | -15.40% / 0.55 / 15.7% | -29.91% / 0.34 / 30.0% | -36.89% / 0.28 / 36.9% |
- 매트릭스의 `cost_stress_1.5x PASS`는 기준 수익이 ≤0이라 **정의상 자동 통과(N/A)** — 의미 있는 통과가 아님. C는 비용 1.5x에서 손실이 2배로 커지는 비용 민감 구조.

## 10. 승인 기준 매트릭스
| 기준 | A | B | C |
|---|---|---|---|
| OOS return > 0 | FAIL | FAIL | FAIL |
| OOS PF ≥ 1.20 | FAIL | FAIL | FAIL |
| OOS MDD ≤ 10% | PASS | FAIL | FAIL |
| 수익 윈도우 ≥ 60% | FAIL (33%) | FAIL (0%) | FAIL (11%) |
| OOS trades ≥ 50 | PASS | PASS | PASS |
| Parameter stability | FAIL | FAIL | FAIL |
| Cost stress 1.5x | N/A(PASS) | N/A(PASS) | N/A(PASS) |

## 11. 최종: **NO_STRATEGY_APPROVED**
세 후보 모두 최소 기준(OOS 수익·PF)에서 탈락. 어떤 전략도 승인하지 않으며 `approved_strategies`는 빈 상태, PAPER는 baseline(`trend_momentum`)으로 인프라 증거 누적 계속.

## 12. Paper infrastructure 누적
- Infrastructure epoch 2026-09-25 14:09 KST 시작: RUNNING 누적 **2.4h / 48h**, reconcile 51회 OK, CRITICAL 0. launchd `com.quant.trader`/`com.quant.api` running.
- Strategy epoch(신설, 분리): `trend_momentum v1` since 16:23 KST — paper trades 0/10, 시그널은 strategy epoch 기준. 전략 교체 시 strategy epoch만 리셋되고 runtime/critical/reconcile는 유지.
- `challenge.start_date`: 첫 LIVE FILLED 매수의 KST 날짜로 자동 기록(`bot_state.challenge`, `live_activated_at` 포함). 현재 null.

## 13. 품질 게이트
`ruff` ✅ · `mypy --strict` ✅ 112 files · `pytest` ✅ **175 passed** (20.3s)

## 14. 발견된 문제
1. **FillModel 0-수량 결함**: 합성 데이터에서 가격이 폭증하면 ₩30,000 매수 수량이 1e-8 BTC 미만으로 0이 되어 `avg_entry` 계산에서 0/0 예외 → `ZeroVolumeError` 가드(엔진은 `ZERO_VOLUME` 거부, PaperGateway는 `under_min_volume` 거부) 추가. 실데이터(₩1.15억)에서는 발생하지 않지만 실제 결함.
2. 연구 하네스의 cost-stress 기준은 기준 수익이 음수면 자동 통과 → 보고서에 N/A로 명시(승인 결과에는 영향 없음: 다른 기준에서 이미 탈락).
3. A의 base params는 FULL backtest에서 2거래(노출 0%)로 너무 보수적 — WF가 선택한 `ema_slow 100`이 실질 파라미터. base params는 문서용 참고값.
4. 계좌 KRW 부족으로 `orders/test` dry-run 미통과 — 입금 후 재실행 필요(코드 변경 없음).
5. `readiness`의 Static Public IP는 재시작 직후 API 프로세스가 구 코드를 캐시해 잠시 FAIL로 보였음 → kickstart 후 PROGRESS 정상.

## 15. 다음 단계 (결정 필요)
1. **DAY 5 커밋** 승인: `feat: day5 strategy research (A/B/C), epochs, challenge start, zero-volume guard` + tag `day5-research`
2. ₩100,000 입금 후 `scripts/private_smoke.py` 재실행 → Order Test PASS 확인(실주문 없음)
3. 전략 연구 2차 방향(모두 15m 진입·0.1% 왕복 비용 구조에서 실패):
   - (a) 거래 빈도를 더 낮추는 방향 — 4h/1D 시간축 추세추종(예: 1D Donchian 20/10 + 4h 확인), 목표 거래 수 20~60/2y, 비용 비중 축소
   - (b) 시장 국면 필터(변동성 레짐, 거래량) 추가 후 A 재평가 — 단 파라미터 수 증가 = 과적합 위험, WF 필수
   - (c) 현 조건에서 엣지가 없다고 결론짓고 PAPER 인프라 증거만 누적하며 관찰 기간 연장
4. 승인 기준 유지 여부(OOS trades ≥ 50은 저빈도 전략(a)와 충돌 → 2y 기준 ≥ 30으로 조정 검토)
5. PAPER 전략 교체 없음(후보 미승인). baseline 그대로 운영 지속.
