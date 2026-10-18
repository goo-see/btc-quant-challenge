# DAY 1 보고 — 2026-09-24

## 1. 생성/수정 파일

```
.gitignore  .env.example  README.md
config/config.yaml
docs/00-architecture.md (§10 승인 반영 추가)  docs/01-upbit-api-policy.md  docs/day1-report.md
backend/.python-version (3.12)  backend/pyproject.toml  backend/uv.lock
backend/app/
  core/        types.py events.py clock.py logging.py
  config/      settings.py live_guard.py
  exchange/upbit/ auth.py tick_size.py rate_limiter.py errors.py models.py rest_client.py ws_client.py
  market/      indicators.py candle_engine.py historical.py
  strategy/    base.py trend_momentum.py registry.py
  risk/        position_sizer.py
  execution/   fill_model.py
  portfolio/   accounting.py
  backtest/    engine.py metrics.py report.py walk_forward.py
  database/    models.py engine.py repository.py
backend/tests/
  conftest.py
  unit/        test_auth test_tick_size test_rate_limiter test_indicators test_candle_engine test_accounting
               test_fill_model test_position_sizer test_strategy test_backtest_engine test_settings_and_guard test_logging_redaction
  integration/ test_historical_sync (respx mock) test_ws_reconnect (fake conn) test_database (WAL / 2-engine / lock retry)
scripts/  _bootstrap.py fetch_history.py run_backtest.py run_walk_forward.py rest_smoke.py ws_smoke.py
data/quant.db (291MB, gitignore)  data/backtests/*.csv|json  logs/fetch_history.log
```

## 2. Architecture 변경 사항 (설계 대비)

| 변경 | 이유 |
|---|---|
| `CandleAggregator`가 1m 뿐 아니라 임의 base TF(예: 15m→1h)를 받도록 일반화 | Walk-Forward 54조합 스윕을 15m 리샘플 데이터로 빠르게 돌리고, 최종 검증만 1m으로 실행. 버킷 의미는 동일(테스트 `test_resample_15m_to_1h_matches_direct`) |
| 백테스트 인트라바 스탑을 **base bar 단위**로 체크, 시그널 체결은 **다음 base bar 시가** | 룩어헤드 차단 + Stop 우선 정책을 1m 해상도로 적용 |
| Alembic 마이그레이션은 Day 2로 이월, Day 1은 `metadata.create_all` | 스키마가 아직 변동 중 |
| `risk.by_mode` 구조 도입 (LIVE/PAPER/BACKTEST 별 `max_position_pct`, `max_order_krw`, `max_concurrent_positions`) | 승인 조건 3 |
| `live_guard.py`에 정적 4게이트 + 증거 게이트(`LiveEvidence`) 구조 | 승인 조건 B |
| Rate limiter: 헤더 그룹 alias(`candles`→`candle`), candle 4 req/s, burst 3 | 실측 429 (아래) |
| 수수료 반올림 `ROUND_HALF_UP` | 보수적(수수료를 더 크게) |

## 3. 실제 Upbit 연결 결과

**REST (public)** — `scripts/rest_smoke.py`
- `GET /v1/ticker` KRW-BTC = ₩115,313,000, latency EMA 56ms
- `GET /v1/candles/minutes/1?count=3` 정상, 응답 헤더 `Remaining-Req: group=candles; min=600; sec=9`
  → **문서의 그룹명 `candle`과 다르게 `candles`로 옴** (클라이언트에 alias 처리)
- 인증 엔드포인트는 `.env`에 키가 없어 스킵(Day 1 범위 밖, 정상)

**WebSocket (public)** — `scripts/ws_smoke.py --seconds 60`
- `wss://api.upbit.com/websocket/v1` 연결 OK, ticker 84 / trade 132 / candle.1m 19 = 235 msg
- 재접속 0, 에러 0, latency EMA ≈ 206ms(서버 timestamp 대비), `PING`→`{"status":"UP"}` 확인
- `candle.1m` 필드(`candle_date_time_utc, opening_price, ...`) 문서와 일치

## 4. Historical 데이터 수집 결과

- 범위: **2024-09-24 04:09 → 2026-09-24 04:08 UTC**, 1m 캔들 **1,048,360개**, DB 291MB
- 요청 5,242회, **429 = 523회(10%)** → 1초 대기 후 재시도로 전부 복구, 최종 에러 0
- 검증: duplicates **0**, out_of_order **0**, OHLC 위반 **0**, coverage **99.73%** (missing 2,840분)
- 결측의 대부분은 UTC 17:00(KST 02:00) 시작 4~6시간 구간 → Upbit 정기 서버점검 시간대. 나머지 1분 결측은 무체결 분(공식 문서상 정상)
- Resume 검증: 통합 테스트 `test_sync_paginates_and_is_resumable`에서 (재실행 → 요청 ≤1, head 확장 → 부족분만 요청) 확인

## 5. Backtest 결과 (config 기본 파라미터, 1m base, LIVE 동일 사이징 30%/₩30,000)

| 구간 | Net Return | Buy&Hold | Trades | Win% | PF | Expectancy | MDD | Fees |
|---|---|---|---|---|---|---|---|---|
| 2024-09-24 → 2026-09-24 | **-21.06%** | +36.64% | 376 | 20.5% | 0.40 | -₩56 | 22.19% | ₩10,033 |
| 2026-01-01 → 2026-09-24 | -7.86% | -9.91% | 129 | 18.6% | 0.40 | -₩61 | 8.41% | ₩3,698 |

- 평균 손실 ₩118 = 포지션 ₩30,000의 0.4% → 15m ATR×2 손절은 왕복 수수료 0.1%와 노이즈에 비해 너무 좁음. 최대 25연패.
- 실행 시간: 2년 1m(105만 봉) 12.7초.

## 6. Walk-Forward 결과 (train 180d / test 60d, 60d 롤링, 9 윈도우, 15m base, 54 조합)

| Train | Test end | 선택 파라미터 (fast/slow/stop/trail) | Train Ret% | Test Ret% | Test MDD% | Test Trades |
|---|---|---|---|---|---|---|
| 24-09→25-03 | 25-05-22 | 20/48/2.0/5.0 | +0.09 | -1.31 | 2.01 | 32 |
| 24-11→25-05 | 25-07-21 | 12/26/2.0/5.0 | -2.35 | -0.35 | 1.92 | 36 |
| 25-01→25-07 | 25-09-19 | 12/48/2.0/5.0 | -4.77 | -0.84 | 1.18 | 31 |
| 25-03→25-09 | 25-11-18 | 20/26/3.0/5.0 | -3.23 | **+0.58** | 1.29 | 15 |
| 25-05→25-11 | 26-01-17 | 20/26/4.0/5.0 | -1.83 | -2.10 | 2.60 | 28 |
| 25-07→26-01 | 26-03-18 | 20/96/2.0/5.0 | -0.66 | -0.93 | 1.54 | 11 |
| 25-09→26-03 | 26-05-17 | 20/48/4.0/5.0 | -1.03 | -1.10 | 2.13 | 29 |
| 25-11→26-05 | 26-07-16 | 8/96/2.0/5.0 | -2.41 | -0.81 | 1.41 | 8 |
| 26-01→26-07 | 26-09-14 | 20/48/4.0/5.0 | -1.25 | -2.32 | 2.32 | 17 |

- **Stitched OOS return -8.84%, OOS MDD 9.49%, 수익 윈도우 1/9**
- 선택 파라미터가 매 윈도우마다 바뀜(안정성 없음). 54조합 중 **train에서조차 양의 수익을 낸 조합이 거의 없음** → 과최적화가 아니라 **전략 자체에 엣지가 없음**.
- 결론: `trend_momentum` 베이스라인은 현 형태로는 PAPER 투입 부적합. 파이프라인(수집→리샘플→지표→전략→체결→지표계산)은 검증 완료.

## 7. 품질 게이트

- `uv run ruff check app tests ../scripts` → **All checks passed**
- `uv run mypy` (strict, 63 files) → **Success: no issues found**
- `uv run pytest -q` → **89 passed** (9초, 네트워크 없음)

## 8. 발견된 문제

1. **전략 무엣지**: §5–6. 15m 골든크로스+RSI 필터는 2년 KRW-BTC에서 손실. 승률 20%, 손익비 1.6은 구조적으로 안 맞음.
2. **Upbit candle Rate Limit**: 순차 6 req/s에서 10% 429. 공식 10 req/s와 불일치(고정 윈도우 카운팅 추정). 4 req/s로 하향.
3. **Remaining-Req 그룹명 불일치**(`candles`): 문서와 다름. alias 처리했으나 다른 그룹도 다를 수 있음 → 인증 엔드포인트 첫 호출 시 헤더 로깅 예정.
4. Upbit 정기점검(KST 02:00~) 구간에 캔들 없음 → 라이브에서 `market_data_stale_sec=120` Kill Switch가 점검마다 발동할 수 있음. 점검 시간대 예외 처리 또는 "거래소 점검" 상태 구분 필요.
5. `pandas 3.0.6`이 설치됨(2.x 가정으로 작성). 테스트는 통과했지만 Copy-on-Write 기본값 변화 등 API 차이 유의.
6. Walk-forward 스크립트를 백그라운드로 전환하자 wall time이 12분(CPU 75초)으로 늘어남 — 도구 환경 문제로 보이며 코드 이슈 아님. 포그라운드 3윈도우 실행은 21초.

## 9. DAY 2 진행 전 결정 필요 사항

1. **전략 방향** (가장 중요). 선택지:
   - (a) 베이스라인 규칙 유지, 파라미터만 확장 탐색 — WF 결과상 비추천
   - (b) 진입 조건 교체: 예) 1h/4h 추세 + 15m **눌림목(pullback) 재진입**, 또는 **Donchian/변동성 돌파 + ATR 3~4× 스탑**, 보유 시간 연장(거래 빈도↓, 수수료 비중↓)
   - (c) Day 2는 Paper/Risk/Order 인프라에 집중하고 전략 연구는 병행 트랙으로 분리 (권장: 인프라는 전략 독립적)
2. **Kill Switch 점검 시간대 처리**: 점검 창(KST 02:00–06:00) 동안 stale 판정 유예 vs 그대로 HALT 후 수동 Resume.
3. **Alembic 도입 시점**: Day 2에 도입(권장) vs Day 4.
4. **PAPER 사이징**: 현재 LIVE와 동일(30%/₩30,000)으로 두어 PAPER 결과가 LIVE 예측치가 되게 함. 유지 여부.
5. Git 커밋: 아직 커밋하지 않았음(지시 없음). Day 1 상태로 첫 커밋할지.
