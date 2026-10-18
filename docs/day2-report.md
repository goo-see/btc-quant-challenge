# DAY 2 보고 — 2026-09-24

## 1. Git checkpoint
- `e0ba31d feat: complete day1 quant trading foundation` · tag **`day1-baseline`** (79 files, data/logs/db/.env 미포함 확인)
- DAY 2 변경분(33개 파일)은 **미커밋** 상태 — 승인 후 `feat: day2 paper trading, risk, order lifecycle, 4-state ops` 로 커밋 예정

## 2. pandas pin
- `pandas>=2.2,<3` → **2.3.3** 설치(3.0.6에서 다운그레이드), `alembic>=1.13` 추가, `uv.lock` 갱신. 전체 테스트 재실행 통과.

## 3. Alembic migration
- `backend/alembic.ini` + `app/database/migrations/` (sync URL 변환, SQLite batch mode)
- `0001_baseline_day1_schema`: 15 테이블. **pre-alembic DB(`candles` 존재) 감지 시 DDL 생략 + 버전 stamp**
- 결과
  | 대상 | 결과 |
  |---|---|
  | `quant.db` 복사본 | upgrade head OK · candles **1,048,360 → 1,048,360** 보존 · `alembic check` 드리프트 없음 |
  | 실제 `data/quant.db` | version `0001` 적용, 데이터 보존 |
  | 빈 DB | 15 테이블 생성, `alembic check` OK |
  | 테스트 | `tests/integration/test_alembic_migration.py` 3건(fresh / legacy 채택 / 실 DB 복사본) |

## 4. Paper execution (실제 Upbit WS, 가상 ₩100,000)
| | RUN 1 (`--reset`, 180s) | RUN 2 (재시작, 90s) |
|---|---|---|
| 기동 | tail sync 1 req → 12일 1m 17,250봉 warm-up → 15m/1h 지표 ready | 동일 |
| 상태 전이 | MAINTENANCE(STARTUP) → 5s 후 **RUNNING**(복구 체크 6/6) | 동일, 이전 RUNNING 상태·원장 복원 |
| WS | 278 msg, 재접속 0, latency 186ms | 155 msg, 재접속 0, 62ms |
| 시그널 | 15m 봉 마감 3회 → 모두 HOLD(`NO_RECENT_GOLDEN_CROSS`) | — |
| 주문 | 0 (4.5분 내 진입 조건 미충족, 정상) | 0 |
| Reconcile | 5분 주기 3회 **OK** | OK |
| 종료 | 정상(exit 0), 상태 RUNNING 저장 | 정상 |

- 실거래 조건이 발생하지 않아 실주문 경로는 **E2E 테스트(합성 데이터 + 실제 파이프라인)** 로 검증: `test_full_paper_pipeline_trades_and_stays_consistent`(BUY/SELL 체결, 내부 장부 == 페이퍼 원장), `test_stop_loss_via_ticker_goes_through_risk_path`(ticker 스탑 → Risk 승인 → 체결 → 손실 트레이드 기록).

## 5. Risk Manager (`tests/unit/test_risk_manager.py` 9건)
- 규칙 순서: BOT_STATE → KILL_SWITCH → UNKNOWN_ORDER → INFLIGHT → DAILY_LOSS_LIMIT(+kill) → MAX_DRAWDOWN(+kill) → [SELL: 포지션 유무·최소금액] → [BUY: 연패·쿨다운·시간당 주문·포지션 상태·동시 포지션·사이징·최소잔고]
- 사이징: equity 30% & ₩30,000 상한 → ₩100k에서 **₩30,000**, ₩50k에서 ₩15,000. `test_order_size_never_increases_after_losses` 통과.
- SELL은 연패/쿨다운/시간당 한도에 막히지 않음(청산은 항상 가능). 단 MAX_DRAWDOWN/DAILY_LOSS는 SELL도 거부 후 HALTED(포지션은 유지, 수동 판단).

## 6. Order lifecycle (`tests/integration/test_order_manager.py` 10건)
CREATED → SUBMITTED(DB 저장 후 거래소 호출) → FILLED / REJECTED / CANCELLED(부분체결) / UNKNOWN → 해소. 이벤트 `ORDER_CREATED/SUBMITTED/FILLED/REJECTED/PARTIALLY_FILLED/UNKNOWN` 발행·DB 기록.

## 7. Duplicate / Idempotency
- 동일 `client_order_id` 재제출 → 거래소 호출 **1회**(`place_calls==1`), 프로세스 재시작 후에도 동일.
- in-flight 존재 시 신규 주문 `INFLIGHT_ORDER` 거부. UNKNOWN 존재 시 `UNKNOWN_ORDER_PENDING` 거부.
- 거래소에만 존재하는 identifier(크래시 후 재제출) → `DuplicateIdentifier` → 조회로 FILLED 확정.
- 네트워크 오류 **전송 후** → UNKNOWN → 조회 → FILLED / **전송 전** → NOT_FOUND → REJECTED(포지션 변화 없음) / 조회도 실패 → UNKNOWN 유지·차단 → 재시작 rehydrate → 복구.

## 8. Reconciliation
- 잔고(KRW ±100 / BTC ±0.00001) · 미체결 집합 · 포지션(거래소 BTC ⇔ 내부 포지션 ⇔ open trade row).
- 불일치 3종 모두 FAIL 감지 테스트. 실행 중 FAIL → **HALTED(RECONCILE_FAILED)**; RESUME 시 복구 체크에서 다시 FAIL → **HALTED(RECONCILE_FAILED_DURING_RECOVERY)**; 원인 해소 후 RESUME → RUNNING.
- 실 PAPER 9회 전부 OK.

## 9. MAINTENANCE / HALTED 상태 전이 (`test_trader_service.py`, `test_state_machine.py`, `test_health.py`)
| 시나리오 | 결과 |
|---|---|
| 데이터 stale + REST 5xx | → MAINTENANCE(EXCHANGE_OUTAGE), 지속 시 유지(HALT 아님), REST 복귀+fresh data → 6체크 PASS → **RUNNING 자동 복귀** |
| 거래소 체결 중 + 우리 피드만 stale | 2× 유예 내 RUNNING 유지 → 초과 시 **HALTED(UNEXPLAINED_STALE_LOCAL_FAULT)**, fresh data 와도 자동 재개 없음 |
| 프로브 판단 불가 | HALTED(UNEXPLAINED_STALE) |
| 공식 StatusSource 점검 공지 | EXCHANGE_OUTAGE 우선 |
| HALTED → RUNNING 직접 전이 | 금지(`InvalidTransition`), 자동 전이 금지(`ManualResumeRequired`) |
| PAUSE/RESUME/KILL | confirmed=false 명령 거부, RESUME은 MAINTENANCE 경유 후 체크 통과 시 RUNNING |

## 10. Process restart recovery
- 테스트: 포지션 보유 + MANUAL_KILL 상태에서 프로세스 종료 → 새 프로세스: **HALTED 유지**, 장부/스탑/원장/open trade id 동일, fresh data 와도 HALTED 유지, RESUME → 체크 PASS → RUNNING, reconcile OK.
- 실제: RUN 2가 RUN 1의 상태·원장을 이어받아 기동(§4).

## 11. 품질 게이트
- `ruff check app tests ../scripts` → **All checks passed**
- `mypy` (strict, **86 files**) → **Success**
- `pytest` → **141 passed** (12.4s; Day 1 89 → +52)

## 12. 발견된 문제
1. **[중요] 동기식 stdout 로깅이 이벤트 루프를 15분 정지** — 도구가 프로세스를 백그라운드로 전환하며 stdout 파이프가 막히자 `StreamHandler` 쓰기에서 전체 루프가 블로킹(타이머 미발화, WS PING 중단 → 서버 연결 종료, 이후 stale 판정으로 HALTED). `QueueHandler/QueueListener` 비차단 로깅으로 교체, httpx 요청 로그 억제. launchd 운영(파일 리다이렉트)에서도 디스크 지연 시 같은 위험이 있었음.
2. 첫 기동 시 포트폴리오 상태가 거래 발생 전까지 저장되지 않아 재시작 시 "복원" 로그가 없었음 → 기동 시·스냅샷마다 persist하도록 수정.
3. PAPER 원장과 내부 장부가 같은 체결 데이터를 공유하므로 reconcile OK는 **내부 일관성** 검증이지 거래소 진실 검증이 아님. 실제 의미는 Day 4 LIVE 게이트웨이(`GET /v1/accounts`)에서 발생.
4. `RiskManager`가 MAX_DRAWDOWN/DAILY_LOSS 시 SELL도 거부 → HALTED 상태에서 포지션이 남을 수 있음(설계상 자동 청산 안 함). 대시보드에 "HALTED with open position" 경고 필요(Day 3).
5. Alembic baseline은 `candles` 테이블 존재 여부로 legacy DB를 판단 — Day 1 스키마와 실제 DB가 동일하다는 가정. 이후 변경은 반드시 0002+ 마이그레이션으로.
6. 히스토리 수집 429(10%)는 여전히 존재(warm-up tail sync에서는 1 req로 무관).

## 13. DAY 3 전 결정 사항
1. **Dashboard/API 바인딩**: Tailscale 미설치. Day 3 전에 `brew install --cask tailscale` 후 Tailnet IP를 `API_HOST`로 고정할지, 초기엔 `127.0.0.1`로 개발 후 전환할지.
2. **Frontend 툴체인**: node 미설치 → `brew install node`(LTS) 승인.
3. **HALTED + 포지션 보유 시 UI 동작**: 경고만 표시(권장) vs 수동 "청산" 버튼 제공(위험 수동 버튼 금지 원칙과 충돌 — 확인 다이얼로그 2단계라면 허용?).
4. **Command 채널**: Dashboard → `commands` 테이블(현행) 유지 vs API에서 Trader 프로세스로 직접 IPC. 현행 유지 권장(프로세스 독립성).
5. **PAPER 장기 실행 시작 시점**: launchd 등록은 Day 4 계획이지만, LIVE 게이트 증거(`min_paper_hours 48`, `min_paper_trades 10`) 누적을 위해 Day 3부터 `caffeinate` + 수동 백그라운드로 PAPER를 계속 돌릴지.
6. **DAY 2 커밋** 승인 여부.
