# Strategy Research Round 2 보고 — 2026-09-25

## 1. DAY 5 commit/tag
- `7f64fe1 feat: day5 strategy research (A/B/C), epochs, challenge start, zero-volume guard` · tag **`day5-research`**. Round 2 변경분(1h 히스토리 수집 일반화, `Timeframe.D1`, 엔진 warm-up `start_at`, WF 트레이드 보존, 전략 D/E, `research2.py`, 테스트)은 **미커밋**.

## 2. Historical dataset 확장
- 출처: Upbit `GET /v1/candles/minutes/60`(UTC 시간 버킷), `scripts/fetch_history.py --tf 1h --start 2017-09-25`
- 범위 **2017-09-25 03:00 → 2026-09-25 10:00 UTC**, **78,763봉**(약 9.0년), coverage **99.831%**(결측 133봉; 최대 갭 7시간 2018-08-21, 나머지 ≤5시간 — 점검/무체결), duplicates 0, out_of_order 0, OHLC 위반 0
- 연도별 봉수: 2017 2,333 / 2018~2025 각 8,739~8,779 / 2026 6,412
- 교차검증: 최근 2y 구간 REST 1h vs 1m 리샘플 1h **18,170봉** 중 OHLC 불일치 2건(양 끝 부분 시간대), 거래량 0.1% 초과 불일치 3건 → 동일 소스로 판단. 기존 1m 2y 데이터 보존(1m은 2024-08-28까지 소폭 확장됨).
- 결함 수정: 2019-03 이전 캔들의 `timestamp: null` → `UpbitCandle.timestamp` Optional.

## 3. D / E 전략 정의 (동일 `BaseStrategy`, Strategy→Risk→Order 구조 무변경)
| | D `trend_following_4h` | E `trend_following_1d` |
|---|---|---|
| 전략 TF / 추세 TF | 4h / 1d | 1d / 1d |
| 추세 필터 | 1d close > EMA(trend_ema) | 1d close > EMA(trend_ema) |
| 진입 | 4h close > 직전 entry_n봉 최고가 | 1d close > 직전 entry_n일 최고가 |
| 초기 스탑 / 트레일 | close − ATR14×atr_stop_mult / 최고가 − ATR14×4.0 (ratchet) | 동일 |
| 청산 | close < 직전 exit_n봉 최저가 **또는** 추세 상실(1d close < EMA) | 동일 |

## 4. Parameter grid (coarse, 각 24조합)
- D: `entry_n {20,30,42}` × `trend_ema {50,100}` × `atr_stop_mult {2,3}` × `exit_n {10,20}`
- E: `entry_n {20,40,55}` × `trend_ema {100,200}` × `atr_stop_mult {2,3}` × `exit_n {10,20}`
- 고정: ATR14, atr_trail 4.0, exit_on_trend_loss 1. 추가 지표 없음.

## 5. WF setup (동일 조건)
1h base(스탑 1h 해상도, STOP-first) → 4h/1d는 `CandleAggregator`(UTC 버킷) · train **540d** / test **180d** / roll 180d · **warm-up 240d prefix**(지표·채널 상태만, 주문·equity 없음, 과거 데이터만 사용) · **15 윈도우**(test 2019-03 → 2026-08) · 선택 = train return / max(MDD,1), ≥3 trades · 체결 다음 1h 시가 · fee 0.05%/side, slippage 3bps · 사이징 equity 30% / cap ₩30,000 / 1포지션 · 각 test 윈도우는 ₩100,000에서 시작해 곱으로 stitch.

## 6. 결과
| 지표 | **D (4h)** | **E (1d)** | Buy & Hold (동일 OOS 기간) |
|---|---|---|---|
| OOS return (stitched) | **+200.35%** | +258.96% | +2631.60% |
| OOS MDD | **6.72%** | 12.49% | 87.49% |
| OOS PF | 3.45 | 7.59 | — |
| OOS trades | **119** | 29 | — |
| Win rate | 44.5% | 48.3% | — |
| Expectancy | ₩950 | ₩4,759 | — |
| Exposure | 21.5% | 29.3% | 100% |
| Fees (OOS) | ₩3,653 | ₩997 | — |
| 수익 WF 윈도우 | **14/15** | 11/15 | — |
| FULL backtest (base params) | +161.67% / MDD 6.82% / 133 trades | +125.54% / MDD 13.30% / 33 trades | — |

- Regime: D 강세 9/9 수익(평균 +12.24%), 약세 5/6 수익(평균 +1.60%); E 강세 9/9, 약세 2/6.
- D 윈도우별 OOS: +33.6, +1.8, +6.3, +25.2, +0.1, +7.0, +1.0, +4.3, +8.4, +13.7, +5.0, +10.0, +3.7, +2.2, **−2.4**(최근 2026-02→08). 선택 파라미터는 `entry_n 30, trend_ema 50→100, stop 2~3, exit 10~20`으로 완만하게 이동.

## 7. Parameter stability
| | 이웃 평가 | 이웃 양의 기대값 | cliff |
|---|---|---|---|
| D | 70 | **99%** | 0 |
| E | 66 | 83% | 0 |

## 8. Cost stress (OOS return / PF / MDD)
| | 1.0x | 1.5x | 2.0x |
|---|---|---|---|
| D | +200.35% / 3.45 / 6.72% | +192.38% / 3.31 / 6.89% | +184.62% / 3.18 / 7.06% |
| E | +258.96% / 7.59 / 12.49% | +256.26% / 7.45 / 12.51% | +253.58% / 7.31 / 12.54% |
- 저빈도라 비용 민감도가 낮음(D: 2.0x에서도 +185%).

## 9. Trade concentration (OOS)
| | trades | 최대 승리 | 중앙값 | top1 / top3 / top5 기여 | ex-top1 / ex-top3 / ex-top5 return | 판정 |
|---|---|---|---|---|---|---|
| D | 119 | ₩15,444 | ₩-175 | 14% / 37% / 53% | +97.6% / +70.7% / +53.4% | ok |
| E | 29 | ₩58,713 | ₩-93 | 43% / 90% / 99% | +79.3% / +13.8% / +1.3% | ok(수학적) — 단 top5가 99%: 사실상 5거래 의존 |
- 주의: 두 전략 모두 중앙값 트레이드가 **음수** — 전형적 추세추종(많은 소액 손절 + 소수 큰 추세). D는 top5 제외해도 +53%로 분산 양호, E는 top5 제외 시 +1.25%로 집중.

## 10. Approval matrix
| 기준 | D | E |
|---|---|---|
| OOS return > 0 | PASS | PASS |
| OOS PF ≥ 1.20 | PASS (3.45) | PASS (7.59) |
| OOS MDD ≤ 10% | PASS (6.72%) | **FAIL** (12.49%) |
| 수익 WF 윈도우 ≥ 60% | PASS (93%) | PASS (73%) |
| OOS trades ≥ 50 | PASS (119) | **FAIL** (29) |
| OOS trades ≥ 30 (REVIEW 하한) | PASS | **FAIL** (29) |
| Parameter stability | PASS | PASS |
| Cost stress 1.5x | PASS | PASS |
| Concentration risk 없음 | PASS | PASS |
| **판정** | **APPROVED (research)** | **NOT_APPROVED** |

## 11. 최종: **CANDIDATE D** (`trend_following_4h`)
- 연구 단계 승인. **`live_gate.approved_strategies`는 변경하지 않았고 PAPER 전략도 교체하지 않았습니다** — 사용자 최종 승인 후 새 Strategy Paper Epoch를 시작하는 것이 절차.
- 솔직한 한계:
  1. B&H +2,632% 대비 +200%: 노출 21%·사이징 30%(cap ₩30k) 구조상 상승장의 일부만 취함. 기준상 B&H 초과는 요구하지 않음.
  2. 연환산 ≈ 16%/yr(각 윈도우 ₩100k 리셋 기준). **₩100k→₩1M(10배) 콘텐츠 목표는 이 전략·이 사이징으로는 수년 단위로도 도달 불가**. 목표는 콘텐츠용이며 시스템은 목표 달성을 위해 위험을 올리지 않음.
  3. 마지막 OOS 윈도우(2026-02→08) −2.4%: 최근 국면에서 부진. 거래 빈도 ~16/yr.
  4. 시장가 ₩30k 체결의 슬리피지 3bps 가정은 BTC 호가 깊이상 타당하나, 1h 해상도 스탑은 실거래(틱 단위 스탑)보다 보수적일 수도 낙관적일 수도 있음 — PAPER에서 검증 대상.

## 12. PAPER infra runtime
- Infrastructure epoch 2026-09-25 14:09 KST 이후 RUNNING **5.5h / 48h**, reconcile 마지막 OK, CRITICAL 0. launchd trader/api running.
- Strategy epoch: `trend_momentum` v1 since 2026-09-25T07:23 — paper trades 0/10. **미변경**.

## 13. Private smoke (재실행)
- credentials PASS · static IP pending(동일 IP, 관측 지속) · accounts **PASS** · orders/chance **PASS**(fee 0.05%, min ₩5,000) · orders/test **FAIL `insufficient_funds_bid`** → `ORDER_TEST_REJECTED_INSUFFICIENT_FUNDS_BID`. 계좌 KRW가 ₩6,000 미만(₩100,000 입금 전). 실주문 미호출. 입금 확인 후 재실행 필요.

## 14. 품질 게이트
`ruff` ✅ · `mypy --strict` ✅ 115 files · `pytest` ✅ 179 passed

## 15. 발견된 문제
1. 최초 `--tf 1h` 수집이 sync 본문 미반영으로 1m을 2017년까지 당기기 시작 → 중단·수정(1m은 2024-08-28까지만 확장됨). 
2. Upbit 2019-03 이전 60m 캔들 `timestamp` null → 모델 Optional.
3. Round 2가 부분 데이터(2019-03~)로 한 번 시작됨 → 즉시 중단 후 전체 데이터로 재실행(보고 결과는 전체 데이터 기준).
4. 저빈도 전략과 LIVE 게이트 충돌: `min_paper_trades 10`은 D(연 ~16회)에서 **약 7개월** 소요. 기준을 낮추지 않고 사용자 결정 필요(§16).
5. E는 29거래로 REVIEW 하한(30) 1건 미달 + MDD 12.5% → NOT_APPROVED. 데이터가 더 길어도 구조적으로 거래 수 확보 어려움.

## 16. 다음 단계 (결정 필요)
1. Round 2 커밋: `feat: research round2 — 1h history 2017+, trend following D/E, concentration analysis` + tag `research-round2`
2. **D 최종 승인 여부** (연구 승인 ≠ LIVE 승인). 승인 시: PAPER 전략을 `trend_following_4h`(WF 최근 선택값 `entry_n 30, trend_ema 100, atr_stop 2~3, exit_n 10~20` 중 고정값 필요)로 교체 → 새 Strategy Paper Epoch, infra epoch 유지, `approved_strategies`는 PAPER 검증 후 별도 승인.
3. 저빈도 전략용 PAPER 증거 정책: (a) 10거래 유지(~7개월) / (b) 시그널·주문 파이프라인 검증(리스크 승인 → 페이퍼 체결 → reconcile)을 N회 확인하는 대체 증거 + 최소 기간(예: 4주) / (c) 별도 REVIEW 승인 — 기준 변경은 사용자 결정.
4. D 파라미터 고정 방식: WF 최신 윈도우 선택값 vs 다수결(`entry_n 30`, `trend_ema 100`, `exit_n 10`) vs 재선택 주기(예: 180일마다 train 540d 재선택 — WF와 동일 절차).
5. ₩100,000 입금 후 `private_smoke.py` 재실행(orders/test PASS 기록).
