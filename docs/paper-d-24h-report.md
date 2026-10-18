# D PAPER 24H Operational Validation — 최종 보고 (2026-09-26 23:15 KST)

Strategy `trend_following_4h_v1` · epoch 2026-09-25 23:09:28 KST · hash `c0e59de4f7b70c0c`
**이 보고서는 분석만 수행했으며 코드/파라미터/epoch/승인 상태/주문을 변경하지 않았습니다.**

## 1. 시스템 상태 (23:15 KST 실측)
| 항목 | 값 | 판정 |
|---|---|---|
| com.quant.trader | running pid 64658, uptime 24.0h, launchd runs 9(누적; epoch 이후 재시작은 의도된 kill-test 1회) | PASS |
| com.quant.api | running pid 64582 | PASS |
| com.quant.watch | running pid 66729, runs 1, 종료 이력 없음 | PASS |
| Bot State / Mode / ExecutionMode | RUNNING (RECOVERY_CHECKS_PASSED) / PAPER / ENTRY_AND_EXIT | PASS |
| Strategy Version / Epoch runtime | trend_following_4h_v1 / **24.1h** (86,760 s RUNNING) | PASS |
| Infrastructure Epoch runtime | 33.2h / 48h (인프라 게이트는 별도, 진행 중) | PROGRESS |
| WebSocket | CONNECTED, 재접속 0, 메시지 139,966, data age 11s | PASS |
| REST / Database | OK / OK (API 오류 0, DB lock 0) | PASS |
| Reconciliation | 291/291 OK, 마지막 23:14 KST, Δ₩0/Δ0 BTC | PASS |
| Critical Errors | 0 | PASS |
| Static Public IP | 203.0.113.10, 관측 39건 동일, **31.3h** → IP_STABILITY_MINIMUM_PASS(ISP 고정 단정 아님) | PASS |
| Frozen Parameter Hash | epoch c0e59de4f7b70c0c == frozen | PASS |
| Power / caffeinate | AC 100%, `PreventUserIdleSystemSleep` assertion 4건(caffeinate), 경고 없음 | PASS |

## 2. 12H 체크포인트 이후 변화
| | 12H (10:59 KST) | 24H (23:15 KST) |
|---|---|---|
| Runtime | 11.8h | 24.1h |
| 평가 횟수 | 3 | 6 (+13:00, 17:00, 21:00 KST) |
| Natural BUY / SELL | 0 / 0 | 0 / 0 |
| Decision | HOLD INSIDE_CHANNEL | HOLD INSIDE_CHANNEL |
| Breakout threshold(30봉 고가) | 117,600,000 | 117,600,000 (불변) |
| 돌파까지 거리 | +3.47% | **+2.75%** (가격이 113.66M→114.45M 상승) |
| Trend(일봉 종가 vs EMA100) | 113,657,000 > 103,160,289 PASS | 동일(일봉 다음 갱신 09-27 09:00 KST) |
| Position / Stop | 없음 / — | 없음 / — |
| Reconcile | 144/144 | 291/291 |
| Static IP | 18.3h | 31.3h (16:15 KST 24h 도달) |
| Warning/Error | 0 | 0 |

## 3. Watcher 최종 결과 (`logs/trigger_watch.log`, `.state` = `ip24`, `rt24`)
| KST | 트리거 |
|---|---|
| 09-26 00:05:27 | watcher start (python, loopback-only, no LLM) pid=66729 |
| 09-26 16:15:13 | **5 STATIC IP 24h** — 203.0.113.10 (24.3h observed) IP_STABILITY_MINIMUM_PASS |
| 09-26 23:10:22 | **6 PAPER RUNTIME 24h** (trend_following_4h_v1) 24.0/24h, paper_approval_ready=true |
- 1 BUY / 2 SELL / 3 CRITICAL / 4 Reconciliation FAIL / WS·REST·DB 경고: **발생 없음**.
- 구조 확인: `scripts/trigger_watch.py` — import는 json/sqlite3/sys/time/urllib/datetime/pathlib만, 네트워크는 `http://127.0.0.1:8787/api/readiness` 단일 loopback, DB는 `mode=ro`. Claude/LLM 참조 0(문서 문자열의 "no LLM" 문구 외). launchd `KeepAlive`, runs=1(재시작 없음). Claude 토큰 소비 0.

## 4. 24H Operational Gate 최종 판정
| 조건 | 근거 | 판정 |
|---|---|---|
| Strategy runtime ≥ 24h | 24.1h | PASS |
| Critical errors == 0 | epoch counters 0, CRITICAL 이벤트 0 | PASS |
| Reconciliation | 291/291 OK | PASS |
| WebSocket / REST / Database | CONNECTED / OK / OK | PASS / PASS / PASS |
| 1h ingest | 24 × 1h 바 저장(14:00Z~13:00Z), 마지막 1.3h 전 | PASS |
| 4h aggregation | 6바(12/16/20/00/04/08Z), 마지막 마감 2.3h 전(12:00Z) | PASS |
| 1d aggregation | 1바(09-25 UTC, 00:00Z 마감; 다음 마감 09-27 09:00 KST) | PASS |
| Indicator state | 4h ready, 1d ready | PASS |
| Frozen parameter hash | 일치 | PASS |
| Restart recovery | epoch 이후 kill -9 64562→64658, RUNNING 유지, reconcile OK | PASS |
| Replay E2E(최신 버전) | 28/28, strategy_version trend_following_4h_v1 | PASS |
| Trailing stop persistence | scenario 10 PASS | PASS |
| Static IP ≥ 24h 동일 | 31.3h, 39/39 동일 | PASS |
| Natural signals / trades | 0 / 0 | INFO |
**paper_approval_ready = true** (readiness 계산값; `paper_approved`는 여전히 false — 변경하지 않음)

## 5. 24시간 Decision 전체 (6회, 모두 HOLD)
| 평가 KST | 4H Close | Daily Close | EMA100 | Trend | Upper(30) | 돌파까지 | Breakout | Lower(20) | ATR14 | Pos | Init.Stop | Trail | Decision | Reason | Execution |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 09-26 01:00 | 113,630,000 | 115,122,000 | 102,948,234 | PASS | 117,600,000 | +3.49% | FAIL | 114,069,000 | 1,206,601 | — | — | — | HOLD | INSIDE_CHANNEL | 없음 |
| 05:00 | 113,539,000 | 115,122,000 | 102,948,234 | PASS | 117,600,000 | +3.58% | FAIL | 113,000,000 | 1,162,344 | — | — | — | HOLD | INSIDE_CHANNEL | 없음 |
| 09:00 | 113,657,000 | 113,657,000 | 103,160,289 | PASS | 117,600,000 | +3.47% | FAIL | 113,000,000 | 1,117,677 | — | — | — | HOLD | INSIDE_CHANNEL | 없음 |
| 13:00 | 113,929,000 | 113,657,000 | 103,160,289 | PASS | 117,600,000 | +3.22% | FAIL | 113,000,000 | 1,075,271 | — | — | — | HOLD | INSIDE_CHANNEL | 없음 |
| 17:00 | 114,404,000 | 113,657,000 | 103,160,289 | PASS | 117,600,000 | +2.79% | FAIL | 113,000,000 | 1,033,823 | — | — | — | HOLD | INSIDE_CHANNEL | 없음 |
| 21:00 | 114,454,000 | 113,657,000 | 103,160,289 | PASS | 117,600,000 | +2.75% | FAIL | 113,000,000 | 994,764 | — | — | — | HOLD | INSIDE_CHANNEL | 없음 |
설명: 여섯 번 모두 "장기 상승 추세(일봉 종가가 EMA100보다 약 10% 위)는 유지 중이지만, 4H 종가가 최근 30봉(5일) 고점 117,600,000원을 돌파하지 못해 진입하지 않음". 하루 동안 가격은 113.2M→114.7M로 완만히 올라 돌파선까지 거리가 3.5%→2.75%로 좁혀졐지만 임계값에는 미달. ATR14는 1.21M→0.99M로 축소(변동성 감소). 채널 하단(20봉 저가)은 113.0M(09-25 12~16Z 바 저가).

## 6. Natural BUY / SELL 상세
- **0건 / 0건.** 주문·체결·trade 레코드 없음. 자연 거래 부재는 게이트 FAIL 사유가 아님(INFO).

## 7. HOLD 판단 품질 관찰 (가설 생성용; 24h로 우열 판단 불가)
| 평가 KST | 기준 종가 | +4h | +8h | +12h | 12h 내 최고가(돌파선 대비) | 분류 |
|---|---|---|---|---|---|---|
| 01:00 | 113,630,000 | −0.08% | +0.02% | +0.26% | 114,023,000 (−3.04%) | A |
| 05:00 | 113,539,000 | +0.10% | +0.34% | +0.76% | 114,404,000 (−2.72%) | A |
| 09:00 | 113,657,000 | +0.24% | +0.66% | +0.70% | 114,717,000 (−2.45%) | A |
| 13:00 | 113,929,000 | +0.42% | +0.46% | INCOMPLETE | 114,717,000 (−2.45%) | A(부분) |
| 17:00 | 114,404,000 | +0.04% | INCOMPLETE | INCOMPLETE | 114,717,000 | INCOMPLETE |
| 21:00 | 114,454,000 | INCOMPLETE | — | — | 114,490,000(진행 중), 현재 114,079,000 | INCOMPLETE |
- A(횡보·소폭): 완료된 4건 모두 ±1% 이내 → 진입 회피가 비용 절감으로 작동. B(큰 상승 놓침): 없음. C(임계값 근접 실패): 없음(최대 접근 −2.45%). D(추세 필터 차단): 없음(전 구간 PASS).
- **Strategy v2 연구 가설**(변경 아님): (H1) 채널 상단이 5일 전 하락 직전 고점(117.6M)에 고정되어 저변동 횡보에서 임계값이 상대적으로 멀어짐 — 변동성(ATR) 정규화 돌파 폭 또는 더 짧은 entry_n을 **WF로** 비교할 가치. (H2) 24h ATR 축소(−18%)는 스퀴즈 후 확장 가능성의 관찰 지표일 뿐, 규칙에 반영하려면 별도 연구 필요.

## 8. 이상 징후 전체 점검
| 항목 | 결과 |
|---|---|
| Candle gap (1m 1,445봉, 14:10Z→14:14Z) | NONE |
| Aggregation timestamp | NONE (4h 12/16/20/00/04/08Z, 1d 00Z, 평가 마감+61s) |
| Duplicate signal / Duplicate order attempt | NONE / NONE |
| Stale market data | NONE (age ≤ 11s, stale 이벤트 0) |
| Position / Accounting / Stop state mismatch | NONE (무포지션, reconcile Δ0, stop null 일치) |
| Parameter hash mismatch | NONE |
| Unexpected process restart | NONE (BOT_STARTED 2건 = 23:09 승격 기동 + 23:14 의도된 kill-test) |
| launchd restart storm / Watcher restart | NONE / NONE |
| API error / latency spike | NONE (REST는 PAPER에서 미사용) |
| SQLite lock / DB error / Warm-up error | NONE (`db_locked_retry` 0, error·critical 로그 0) |
| Decision snapshot missing | NONE (6/6 평가 저장, 마지막 21:01 KST) |
| `reports/2026-01-05.md` | 09-25 23:25 KST 리플레이 실행이 합성 날짜 리포트를 공용 `reports/`에 남긴 것. 임시 DB를 썼으므로 운영 DB·daily_stats·검증 결과에 **영향 없음**. 수정(리플레이의 reports_dir를 임시 경로로) 은 승인 후 |

## 9. Research vs 24H PAPER 결과 구분
| 평가 방식 | Return | MDD | PF | Trades | 비고 |
|---|---|---|---|---|---|
| Research WF OOS (15 windows, 윈도우별 ₩100k) | +200.35% | **6.72%** | 3.45 | 119 | 수익 윈도우 14/15 — Research 승인 근거 |
| Frozen 9Y full-period (복리) | +129.5% | **11.3%** | 3.75 | 116 | ⚠ Small LIVE 전 Risk Review **Warning** |
| Recent 2Y (1m base) | +9.4% | **4.1%** | 2.34 | 20 | |
| **24H PAPER (이번)** | 0.00% (거래 0) | 0.00% | — | 0 | 수익성 증거가 아니라 **실행 파이프라인 검증** |

## 10. Upbit 계좌 (조회만)
KRW **₩10,039.50** · locked 0 · BTC 0 · 미체결 0 · fee 0.05%/0.05% · min ₩5,000 · max ₩1,000,000,000 · market active → **CHALLENGE_CAPITAL_NOT_FUNDED** 유지(PAPER 결과와 무관).

## 11. LIVE Readiness (그룹)
Research **PASS** · Infrastructure **PROGRESS**(runtime 33.2/48h; 그 외 PASS) · Private API **FAIL**(capital) · Strategy PAPER **READY_FOR_APPROVAL**(`paper_approval_ready=true`, `paper_approved=NO`) · Static IP **PASS** · Challenge Capital **FAIL** · Paper Approved **NO** · Live Approved **NO** · **LIVE BLOCKED** (STRATEGY_NOT_APPROVED)

## 12. Small LIVE(₩6,000) 전 남은 blocker — 실제 순서
1. 사용자 **`paper_approved=true`** 명시 승인(config `strategy_status`) — 게이트 PASS 상태.
2. **Challenge capital**: ₩100,000 입금 후 `private_smoke.py` → `challenge_capital PASS`(정책상 Small LIVE도 원금 확인 후).
3. **Infrastructure runtime ≥ 48h**(현재 33.2h → 09-27 14:00 KST 무렵) — 인프라 게이트는 24h 전략 게이트와 별도.
4. **Small LIVE 상한 배선**: 현재 `live_gate.small_live_max_order_krw: 6000`은 정의만 있고 사이징은 `risk.by_mode.LIVE.max_order_krw=30,000`을 사용 → Small LIVE 단계용으로 LIVE max_order_krw를 6,000으로 두거나 플래그 배선 필요(코드/설정 변경 → 사전 보고).
5. **Slippage 측정 배선**(§13) + `/api/trades` 노출 — 코드 변경, 사전 보고.
6. **Risk Review**: Frozen 9Y MDD 11.3% warning 확인, ExecutionMode(EXIT_ONLY/FROZEN) 정책 재확인, HALTED+포지션 UI 확인.
7. LIVE 기동 절차: `TRADING_MODE=LIVE`(launchd plist 환경변수 변경 필요 — 현재 PAPER 하드코딩) + `LIVE_CONFIRM` + `live_gate.enabled=true` + `live_approved=true` → 기동 시 정적 게이트·readiness·preflight(accounts/chance/orders-test/capital) 재실행 → 첫 LIVE BUY FILLED 시 challenge start_date 기록. PAPER 트레이더와 동시 실행 불가(단일 트레이더).

## 13. `app/monitoring/slippage.py` 검토 (미배포)
- 기능 확인: VWAP 체결가, 불리 방향 양수 slippage_bps(BUY: fill>signal, SELL: fill<signal), 초기 스탑 거리 2종. 테스트 2건 통과. 배선 가치: **있음** — Small LIVE의 핵심 측정(신호 종가 vs 실체결)이며 PAPER에서도 같은 필드를 기록해 PAPER↔LIVE 비교 기준선이 됨.
- 제안(수정은 승인 후): (a) `signal_time`, `order_sent_at`, `first_fill_at`과 **주문 전송 시점 ticker(reference_price_at_send)** 를 추가해 "신호→전송 지연"과 "거래소 슬리피지"를 분리; (b) 저장 위치 `orders.raw["execution_metrics"]` + `/api/trades`에 노출 + Trading 탭 거래 표에 slippage 열; (c) SELL 시 `initial_stop` 대신 `exit_trigger_price`(스탑/채널/추세 트리거 값)를 기록해 청산 슬리피지도 측정; (d) 수수료는 별도(`paid_fee`)이므로 VWAP는 fee 제외 유지.

## 14. Git / 미커밋
`9245f43 feat: strategy D paper promotion…` 이후 미커밋 8개:
| 파일 | 성격 |
|---|---|
| `scripts/trigger_watch.py`, `scripts/launchd/com.quant.watch.plist`, `scripts/launchd_install.sh`(M) | 순수 로컬 watcher + launchd 등록(운영 중, 트레이더 무관) |
| `scripts/trigger_watch.sh` | 구 zsh watcher — TCC 문제로 폐기, **삭제 대상** |
| `backend/app/monitoring/slippage.py`, `backend/tests/unit/test_slippage.py` | Small LIVE 측정 준비 모듈(미배선) |
| `docs/00-architecture.md`(M), `docs/strategy-D-rules.md`(M) | 평가방식별 MDD 분리 기록, 동결 메모 |
- 커밋 후보(승인 후): `chore: local trigger watcher via launchd, slippage measurement module (unwired), validation docs` — 24H 검증과 무관한 변경 없음(전략/정책/파라미터 미포함). 이 보고서 `docs/paper-d-24h-report.md`도 포함.

## 15. 최종 판정
```
D PAPER 24H FINAL RESULT

Strategy:               trend_following_4h_v1
Runtime:                24.1 / 24h
Operational Validation: PASS
paper_approval_ready:   true
Natural BUY:            0
Natural SELL:           0
Current Decision:       HOLD
Current Reason:         INSIDE_CHANNEL — 4H close 114,454,000 < 30-bar high 117,600,000 (+2.75% 필요); trend PASS
Critical Errors:        0
Reconciliation:         PASS (291/291)
Static IP:              PASS  31.3h / 24h (203.0.113.10, 39/39 동일)
Unexpected Restarts:    0
Data / State Integrity: PASS
Research Status:        APPROVED
Strategy PAPER:         READY_FOR_APPROVAL
Challenge Capital:      NOT_FUNDED (₩10,039)
LIVE:                   BLOCKED
Recommendation:         A. paper_approved 승인 후 Small LIVE 준비 가능
```
근거: 24h 동안 실시간 ingest·4h/1d 집계·지표·의사결정(6회)·리스크 파이프라인·reconcile(291회)·재시작 복구·해시 고정이 오류 0으로 동작했고, 검증 대상인 "실행 파이프라인의 정확성"은 리플레이 28/28과 실시간 24h로 충족됨. 자연 거래 0건은 시장이 5일 고점을 돌파하지 않은 결과이며 게이트 조건이 아님. 단 Small LIVE 착수 전 §12의 blocker(원금 입금, 인프라 48h, Small LIVE 상한 배선, slippage 배선, Risk Review)는 별도 승인이 필요하다.
