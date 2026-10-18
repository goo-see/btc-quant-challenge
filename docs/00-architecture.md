# ₩100K → ₩1M Bitcoin Quant Challenge — Architecture & Plan

작성일: 2026-09-24 · 상태: **승인됨 (2026-09-24) — 아래 §10 승인 시 수정사항 반영**

---

## 0. 요구사항 분석 — 핵심 판단

| 항목 | 판단 |
|---|---|
| 목표 성격 | ₩1M은 콘텐츠 목표. 최적화 대상은 Net Return / MDD / PF / Expectancy / 안정성 |
| 자본 제약 | ₩100,000, 최소 주문 ₩5,000, 수수료 0.05%/side(왕복 0.1%) → **거래 빈도가 곧 비용**. 15m 이상 타임프레임, 하루 0~3회 거래가 현실적 |
| 포지션 사이징 | 자본이 작아 "리스크 1%/거래" 같은 정밀 사이징이 불가능(₩1,000 리스크 → 손절폭 대비 포지션이 최소주문 미달). V1은 **equity의 고정 비율(기본 90%)로 단일 포지션, ATR 기반 손절** 방식. 이는 마틴게일/물타기와 무관하며 주문 금액은 손실 후 절대 증가하지 않음 |
| 주문 방식 | V1은 **시장가**(매수 `ord_type=price`, 매도 `ord_type=market`). BTC 호가 깊이 대비 ₩10만은 무시할 수준 → 슬리피지 극소, 미체결 상태 관리 단순. 지정가/best는 V2 |
| 권한 계층 | Strategy → Signal만 반환. Risk Manager가 최종 승인. Order Manager만 거래소 호출. Kill Switch는 Risk Manager 상위 |
| 모드 분리 | `TRADING_MODE` env + `config.yaml live.enabled` + `LIVE_CONFIRM` 문구, 3중 게이트. Paper/Live 게이트웨이는 **별도 클래스**이며 Paper 클래스는 주문 엔드포인트 코드 자체를 갖지 않음 |
| 런타임 | Python 3.14.2 설치됨(요구 3.12+ 충족). `uv`로 프로젝트/가상환경 관리(brew 설치 필요). node, tailscale 미설치 → Day 3 전 설치 |

---

## 1. 전체 Architecture

```
                         ┌──────────────────────────────────────────────────────┐
                         │                    MacBook M4 Pro                    │
                         │                                                      │
 Upbit WS ───────────►   │  MarketDataCollector ─► CandleEngine ─► IndicatorEngine│
 (ticker/trade/candle)   │        │ health            │ closed bar               │
                         │        ▼                   ▼                          │
                         │  HealthMonitor      StrategyEngine ──Signal──►        │
                         │        │                                RiskManager   │
                         │        │ kill                          APPROVED/REJECTED
                         │        ▼                                    │         │
                         │  KillSwitch ◄────────────────────────  OrderManager    │
                         │        │                                    │         │
                         │        │              PaperGateway ◄──┐     │         │
 Upbit REST ◄────────────┼────────┼──────────────LiveGateway  ◄──┴─(mode factory)│
 (orders/accounts)       │        │                    │ fills                   │
                         │        ▼                    ▼                          │
                         │   BotState ◄──────── PortfolioManager ──► Reconciler   │
                         │        │                    │                          │
                         │        └──────► EventBus ◄──┘                          │
                         │                    │                                   │
                         │        ┌───────────┴───────────┐                       │
                         │        ▼                       ▼                       │
                         │   Repository(SQLAlchemy)   FastAPI (REST + WS push)    │
                         │   SQLite WAL → (PostgreSQL)        │                   │
                         │                                    ▼                   │
                         │                          React/Vite Dashboard          │
                         └────────────────────────────────────┬───────────────────┘
                                                              │ Tailscale (100.x.x.x)
                                                        Phone / Notebook
```

프로세스 구성 (V1: **단일 Python 프로세스, asyncio 태스크로 분리**)

- `trader` 프로세스: Collector, Engine, Risk, Order, Portfolio, Heartbeat — launchd로 상시 실행
- `api` 프로세스: FastAPI + 정적 빌드된 Dashboard 서빙 — 같은 DB를 읽기 전용 + 제어 명령은 DB `commands` 테이블 통해 전달 (trader 죽어도 대시보드는 살아서 OFFLINE 표시)
- 두 프로세스 분리 이유: Dashboard 장애가 Trader에 영향 주지 않게, Trader 재시작이 Dashboard 세션을 끊지 않게

---

## 2. MVP (V1, 4일) vs 이후

### V1 MVP
- Upbit REST/WS 클라이언트 (인증, rate limiter, retry, 헤더 파싱)
- Historical 1m 캔들 수집 → 5m/15m/1h 리샘플
- Backtester (수수료·슬리피지·최소주문·틱·next-bar fill·Walk-Forward)
- Baseline 전략 1개 (EMA Trend + RSI Momentum + ATR Stop + Volume Filter)
- Paper Trading (실시간 가격, 가상 계좌)
- Risk Manager 8개 규칙 + Kill Switch 9개 트리거
- Order Manager (identifier 멱등성, 상태머신, UNKNOWN 복구)
- Portfolio Accounting + Reconciliation
- FastAPI + React Dashboard (섹션 A~K), Equity Curve, BTC 차트 마커
- Daily Report (LLM 없음, 순수 집계) + Content 데이터 API
- launchd 자동 실행, Tailscale 접속, README
- 테스트 스위트 (spec §21 전항목)

### V2 이후
- 지정가/best 주문, Private WS(myOrder/myAsset)로 체결 실시간 수신
- 다중 전략 앙상블, 파라미터 자동 Walk-Forward 재최적화 스케줄
- LLM 기반 Daily Report 요약 / 로그 요약
- SNS 자동 게시 연동
- PostgreSQL 이관, Prometheus/Grafana
- 텔레그램/푸시 알림
- Regime filter(변동성/추세 국면 분류)

---

## 3. Directory Structure (확정안)

```
Cryptocurrency/                      # repo root
├── backend/
│   ├── pyproject.toml               # uv, ruff, mypy, pytest 설정
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main_trader.py           # trader 엔트리 (asyncio)
│   │   ├── main_api.py              # FastAPI 엔트리
│   │   ├── config/
│   │   │   ├── settings.py          # pydantic-settings: .env + config.yaml 병합, TradingMode enum
│   │   │   └── live_guard.py        # LIVE 3중 게이트 검사
│   │   ├── core/
│   │   │   ├── types.py             # Signal, Side, OrderState, RiskDecision 등 공용 dataclass/enum
│   │   │   ├── events.py            # EventBus, EventType enum
│   │   │   ├── clock.py             # 실시간/백테스트 시계 추상화
│   │   │   └── logging.py           # structlog JSON, secret redaction
│   │   ├── exchange/
│   │   │   ├── base.py              # ExchangeGateway Protocol (paper/live 공통 인터페이스)
│   │   │   └── upbit/
│   │   │       ├── auth.py          # JWT/HS512, query_hash
│   │   │       ├── rate_limiter.py  # 그룹별 token bucket + Remaining-Req 파싱
│   │   │       ├── rest_client.py   # httpx async, retry, 에러 매핑
│   │   │       ├── ws_client.py     # websockets, backoff, PING, health counters
│   │   │       ├── models.py        # 응답 pydantic 모델
│   │   │       ├── tick_size.py     # KRW 호가단위 라운딩
│   │   │       ├── live_gateway.py  # 실제 주문 (LIVE에서만 import/생성)
│   │   │       └── errors.py
│   │   ├── market/
│   │   │   ├── collector.py         # WS 구독 → 내부 큐, health 상태
│   │   │   ├── candle_engine.py     # 1m 기준 캔들 완성 판단, 5m/15m/1h 집계
│   │   │   ├── indicators.py        # EMA/RSI/ATR/Volume SMA (numpy, 증분 계산)
│   │   │   └── historical.py        # REST 과거 캔들 수집/저장
│   │   ├── strategy/
│   │   │   ├── base.py              # BaseStrategy(on_candle, generate_signal)
│   │   │   ├── trend_momentum.py    # Baseline 전략
│   │   │   └── registry.py
│   │   ├── risk/
│   │   │   ├── risk_manager.py      # 규칙 평가 → APPROVED/REJECTED
│   │   │   ├── rules.py             # 개별 규칙 클래스
│   │   │   ├── position_sizer.py
│   │   │   └── kill_switch.py
│   │   ├── execution/
│   │   │   ├── order_manager.py     # 상태머신, 멱등성, UNKNOWN 복구
│   │   │   ├── paper_gateway.py     # 가상 체결 (슬리피지/수수료 모델)
│   │   │   └── fill_model.py        # 백테스트/페이퍼 공용 체결 시뮬레이션
│   │   ├── portfolio/
│   │   │   ├── portfolio_manager.py # 잔고/포지션/PnL/HWM/DD
│   │   │   ├── accounting.py        # 순수 계산 함수 (테스트 용이)
│   │   │   └── reconciler.py        # Upbit 잔고 vs 내부 비교
│   │   ├── backtest/
│   │   │   ├── engine.py            # 이벤트 드리븐 루프
│   │   │   ├── metrics.py           # 성과 지표
│   │   │   ├── walk_forward.py
│   │   │   └── report.py            # 결과 저장(JSON/CSV/equity curve)
│   │   ├── database/
│   │   │   ├── engine.py            # SQLAlchemy async engine, SQLite WAL pragma
│   │   │   ├── models.py            # ORM 테이블
│   │   │   ├── repository.py        # 저장/조회 추상화 (PostgreSQL 교체 지점)
│   │   │   └── migrations/          # alembic
│   │   ├── monitoring/
│   │   │   ├── heartbeat.py
│   │   │   ├── health.py            # 컴포넌트 상태 집계
│   │   │   ├── metrics.py           # 카운터/게이지 (in-memory + snapshot)
│   │   │   └── daily_report.py
│   │   ├── services/
│   │   │   ├── trader_service.py    # 전체 파이프라인 조립/실행
│   │   │   ├── content_service.py   # 콘텐츠용 DAY N 데이터
│   │   │   └── command_service.py   # pause/resume/kill 명령 처리
│   │   ├── api/
│   │   │   ├── router_status.py, router_portfolio.py, router_trades.py,
│   │   │   ├── router_chart.py, router_health.py, router_control.py,
│   │   │   ├── router_content.py, ws_push.py
│   │   │   └── deps.py
│   │   └── schemas/                 # API 응답 pydantic 스키마
│   └── tests/
│       ├── unit/  (strategy, risk, accounting, fees, drawdown, tick_size, auth, rate_limiter)
│       ├── integration/ (order_manager, paper_trading, kill_switch, reconciler, db_failure)
│       └── fixtures/ (mock_upbit_rest.py, mock_upbit_ws.py, sample_candles.parquet)
├── frontend/
│   ├── package.json, vite.config.ts, index.html
│   └── src/
│       ├── api/          # fetch 클라이언트, WS 구독
│       ├── pages/        # Dashboard.tsx
│       ├── components/   # Header, Portfolio, PnL, Risk, Stats, Position, Signals, Trades, Health, EventLog, Progress
│       ├── charts/       # EquityCurve.tsx, BtcChart.tsx (lightweight-charts)
│       └── theme/
├── scripts/
│   ├── fetch_history.py     # 과거 캔들 수집
│   ├── run_backtest.py
│   ├── run_walk_forward.py
│   ├── run_trader.sh / run_api.sh
│   ├── order_test.py        # /v1/orders/test 사전 검증
│   ├── backup_db.sh
│   └── launchd/  (com.quant.trader.plist, com.quant.api.plist, caffeinate 포함)
├── config/
│   ├── config.yaml          # 전략/리스크/시스템 파라미터 (secrets 없음)
│   └── config.backtest.yaml
├── data/     (gitignore) candles sqlite/parquet
├── logs/     (gitignore)
├── docs/
├── .env.example
├── .gitignore
└── README.md
```

---

## 4. Data Model

### 4.1 공용 타입 (in-memory)

```python
class TradingMode(StrEnum): BACKTEST, PAPER, LIVE
class BotStatus(StrEnum):  STARTING, ONLINE, PAUSED, HALTED, STOPPED
class Action(StrEnum):     BUY, SELL, HOLD
class OrderState(StrEnum): CREATED, SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, UNKNOWN

@dataclass(frozen=True)
class Candle: market, timeframe, open_time(UTC), open, high, low, close, volume, quote_volume, is_closed

@dataclass(frozen=True)
class Signal: action, strength(0~1), reason, timestamp, strategy, timeframe, indicators: dict, stop_price: Decimal|None

@dataclass(frozen=True)
class RiskDecision: approved: bool, reason: str|None, order_krw: Decimal|None, snapshot: dict

@dataclass
class OrderIntent: client_order_id(identifier), side, ord_type, krw_amount|volume, signal_id, mode
```

금액/수량은 전부 `Decimal`. float은 지표 계산(numpy)에만 사용.

### 4.2 DB 테이블 (SQLAlchemy, SQLite WAL → PostgreSQL 호환 타입만 사용)

| 테이블 | 주요 컬럼 | 용도 |
|---|---|---|
| `candles` | market, timeframe, open_time(UTC) **UNIQUE**, o,h,l,c, volume, quote_volume, source(rest/ws/resample) | 과거+실시간 캔들 |
| `signals` | id, ts, mode, run_id, strategy, timeframe, action, strength, reason, indicators(JSON), stop_price | 전략 출력 전수 기록 |
| `risk_decisions` | id, signal_id FK, ts, approved, reason, order_krw, snapshot(JSON: equity, dd, daily_pnl, consec_losses…) | Reject 포함 전수 기록 |
| `orders` | id, **client_order_id UNIQUE**(=Upbit identifier), exchange_uuid, mode, market, side, ord_type, req_price, req_volume, req_krw, state, executed_volume, avg_fill_price, paid_fee, signal_id, error_code, raw(JSON), created/submitted/updated_at | 주문 상태머신 |
| `fills` | id, order_id FK, exchange_trade_uuid UNIQUE, price, volume, funds, fee, ts | 체결 단위 |
| `trades` (round-trip) | id, mode, entry_order_id, exit_order_id, entry_time, entry_price, exit_time, exit_price, quantity, gross_pnl, fees, net_pnl, return_pct, holding_sec, strategy, entry_reason, exit_reason, status(OPEN/CLOSED) | 승/패, PF, Expectancy 계산 기준 |
| `portfolio_snapshots` | ts, mode, krw, btc_qty, btc_price, btc_value, equity, avg_entry, realized_pnl, unrealized_pnl, fees_total, hwm, drawdown | Equity Curve (1분 + 이벤트 시) |
| `daily_stats` | date(KST), mode, start_equity, end_equity, daily_pnl, trades, wins, losses, fees, best_trade, worst_trade, max_dd, errors, report(JSON) | Daily Report / 콘텐츠 |
| `events` | id, ts, level, type(EventType), message, payload(JSON) | Event Log |
| `reconciliations` | ts, internal_krw, exchange_krw, internal_btc, exchange_btc, krw_diff, btc_diff, status(OK/WARN/FAIL) | 잔고 대조 |
| `bot_state` | key PK, value(JSON), updated_at | status, mode, halted_reason, run_id, started_at, paused_by, hwm |
| `commands` | id, ts, type(PAUSE/RESUME/KILL), requested_by, confirmed, processed_at, result | Dashboard → Trader 제어 |
| `backtest_runs` | id, ts, strategy, params(JSON), tf, start, end, metrics(JSON), equity_curve_path, is_walk_forward | 백테스트 이력 |
| `health_snapshots` | ts, ws_connected, reconnect_count, msg_count, last_data_ts, rest_latency_ms, rest_errors, db_ok, heartbeat_ts, uptime_sec | System Health 이력 |

모든 시각은 **UTC 저장**, KST 변환은 API/UI 계층에서만. 일자 경계(Daily Loss, Daily Report)는 **KST 00:00** 기준(config로 변경 가능).

### 4.3 config.yaml 스켈레톤

```yaml
system:
  market: KRW-BTC
  timezone_report: Asia/Seoul
  heartbeat_sec: 5
  snapshot_interval_sec: 60
  reconcile_interval_sec: 300
strategy:
  name: trend_momentum
  timeframe: 15m
  ema_fast: 12
  ema_slow: 48
  trend_ema: 200          # 1h 상위 추세 필터
  rsi_period: 14
  rsi_buy_min: 50
  rsi_buy_max: 70
  atr_period: 14
  atr_stop_mult: 2.0
  atr_trail_mult: 3.0
  volume_sma_period: 20
  volume_min_ratio: 1.0
risk:
  max_position_pct: 0.90
  max_order_krw: 100000
  min_order_krw: 5000        # 기동 시 orders/chance로 검증
  daily_loss_limit_pct: 0.03
  max_drawdown_limit_pct: 0.15
  max_consecutive_losses: 4
  cooldown_after_loss_min: 60
  minimum_krw_balance: 1000
  max_orders_per_hour: 4
  fee_rate: 0.0005           # 기동 시 orders/chance로 덮어씀
  slippage_bps: 3
kill_switch:
  api_error_consecutive: 5
  market_data_stale_sec: 120
  heartbeat_stale_sec: 30
  reconcile_krw_tolerance: 100
  reconcile_btc_tolerance: 0.00001
live:
  enabled: false             # LIVE 게이트 2/3
  initial_capital_krw: 100000
```

`.env`: `TRADING_MODE`, `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`, `LIVE_CONFIRM`(LIVE 시 `I_UNDERSTAND_THIS_USES_REAL_KRW`), `DATABASE_URL`, `API_HOST=100.x.x.x`(Tailscale IP 바인딩), `API_PORT`.

---

## 5. 데이터 흐름

### 5.1 Trading Flow (PAPER / LIVE 공통)

```
WS candle.1m(REALTIME) ─► CandleEngine
   ├─ 1m bar 완성 판단: 다음 open_time의 첫 메시지 수신 or 시계 기준 +2s grace
   ├─ 5m/15m/1h 집계 (1m closed bars 기반, 결측 분은 gap 기록)
   └─ closed bar 발생 ─► IndicatorEngine.update(tf) ─► StrategyEngine.on_candle(tf)
                                                          │
                     Signal(BUY|SELL|HOLD, strength, reason, indicators, stop)
                                                          │ (DB: signals)
                                                          ▼
                                                  RiskManager.evaluate()
        ┌─ KillSwitch.active? ── REJECTED(KILL_SWITCH)
        ├─ BotStatus PAUSED?  ── REJECTED(PAUSED)
        ├─ daily_pnl ≤ -limit ── REJECTED(DAILY_LOSS_LIMIT) + KillSwitch.trigger
        ├─ drawdown ≥ limit  ── REJECTED(MAX_DRAWDOWN) + KillSwitch.trigger
        ├─ consecutive_losses ≥ n ── REJECTED(MAX_CONSECUTIVE_LOSSES)
        ├─ cooldown 잔여     ── REJECTED(COOLDOWN)
        ├─ orders_last_hour ≥ n ── REJECTED(RATE_ORDERS_PER_HOUR)
        ├─ BUY & 보유중 / SELL & 미보유 ── REJECTED(POSITION_STATE)
        ├─ PositionSizer → order_krw; < min_order ── REJECTED(MIN_ORDER)
        └─ krw_after ≥ minimum_krw_balance 확인
                                                          │ (DB: risk_decisions)
                                                  APPROVED(order_krw)
                                                          ▼
                                                  OrderManager.submit(intent)
        ├─ client_order_id = f"{run_id}-{signal_id}" (UNIQUE 제약 → 중복 차단)
        ├─ 동일 signal_id 미완료 주문 존재? ── skip(DUPLICATE)
        ├─ state CREATED ─► gateway.place() ─► SUBMITTED
        ├─ 응답 OK ─► uuid 저장 ─► poll GET /v1/order (0.5s→1s→2s, 최대 30s)
        │     done: FILLED / executed>0 & cancel: PARTIALLY_FILLED→CANCELLED / wait: OPEN
        ├─ 타임아웃/네트워크 오류 ─► UNKNOWN ─► GET /v1/order?identifier= 로 복구
        │     못 찾으면 REJECTED(NOT_FOUND), 찾으면 상태 반영. UNKNOWN 잔존 시 신규 주문 금지
        └─ fills 저장 ─► PortfolioManager.apply_fill()
                                                          ▼
                                                  PortfolioManager
        ├─ krw/btc/avg_entry 갱신, realized_pnl (FIFO, 단일 포지션이므로 단순)
        ├─ trades round-trip open/close, consecutive_losses 갱신
        ├─ equity/hwm/drawdown 재계산 ─► portfolio_snapshots
        └─ EventBus.publish(ORDER_FILLED …)
                                                          ▼
                         ticker 매 수신 ─► unrealized_pnl, stop 체크(ATR stop 도달 시 SELL Signal을 전략 우회 없이 StrategyEngine.on_tick 경로로 생성 → 동일 Risk 경로 통과)
```

- 백테스트는 같은 파이프라인에 `BacktestClock` + `BacktestGateway(fill_model)`를 꽂아 실행 → **전략/리스크 코드가 백테스트와 실거래에서 동일**하게 동작(look-ahead 방지: signal은 bar close, fill은 다음 bar open ± slippage).

### 5.2 Risk / Kill Switch Flow

```
트리거 소스                        → KillSwitch.trigger(reason)
 RiskManager: DAILY_LOSS, MAX_DD
 OrderManager: API 연속 오류 n회, DUPLICATE_ORDER 감지, UNKNOWN 복구 실패
 HealthMonitor: market_data_stale > 120s, heartbeat 누락, DB write 실패
 Reconciler: 잔고 불일치 > tolerance, 예상치 못한 포지션(BTC 있는데 내부 포지션 없음)
                                     │
                    ┌────────────────┴────────────────┐
                    │ bot_state.status = HALTED        │
                    │ bot_state.halted_reason, ts       │
                    │ events: KILL_SWITCH (level=CRIT)  │
                    │ 진행중 OPEN 주문 → 취소 시도(LIVE)  │
                    │ 신규 주문 차단 (RiskManager 최우선) │
                    │ 포지션은 유지(자동 청산 안 함, 사용자 판단) │
                    └────────────────┬────────────────┘
                                     ▼
              Dashboard: 상단 붉은 배너 + 사유. RESUME은 Dashboard 확인 다이얼로그 → commands 테이블 → Trader가 처리
              자동 재개 없음. 재개 시 reconcile 강제 실행 후 통과해야 ONLINE.
```

### 5.3 Monitoring Flow

```
Heartbeat(5s) ─► bot_state.heartbeat_ts, health_snapshots(60s)
Collector     ─► ws_connected, reconnect_count, msg_count, last_data_ts, latency(ws timestamp vs 수신)
RestClient    ─► latency EMA, error count, Remaining-Req 잔량
Repository    ─► db_ok, db_errors
Portfolio     ─► equity, dd, daily_pnl, fees  (metrics gauge)
       │
       ▼
FastAPI /api/health, /api/status ─► Dashboard 5s polling + /ws/live push
 Dashboard: heartbeat_ts > 30s → OFFLINE 표시
 모든 events(level ≥ WARN) → Alert 영역
Daily 00:00 KST ─► daily_stats 집계 + reports/YYYY-MM-DD.md + /api/content/day/{n}
```

---

## 6. Baseline 전략 (trend_momentum) 규칙 초안

- 상위 추세 필터(1h): close > EMA200 일 때만 매수 허용
- 진입(15m): EMA12 > EMA48 상향 교차 후 N봉 이내 && RSI14 ∈ [50, 70] && volume ≥ SMA20×ratio
- 손절: entry − ATR14×2.0 (틱 체크)
- 트레일링: 최고가 − ATR14×3.0
- 청산: EMA12 < EMA48 하향 교차 또는 손절/트레일 도달
- 모든 수치는 config, Walk-Forward(예: 6개월 train / 2개월 test rolling)로 선정. 전체 구간 최적값 채택 금지.
- 기대치: 거래 빈도 낮음(주 1~3회). 수수료 0.1% 왕복이므로 평균 수익 거래가 0.5% 이상이어야 의미 있음 — 백테스트에서 이 기준 미달이면 전략 교체.

---

## 7. LIVE 진입 절차 (Safety Gate)

1. `TRADING_MODE=LIVE` (env)
2. `config.yaml live.enabled: true`
3. `LIVE_CONFIRM=I_UNDERSTAND_THIS_USES_REAL_KRW` (env)
4. 기동 시 자동 검사: API 키 존재/권한(자산조회+주문, **출금 없음**), `GET /v1/accounts` KRW ≥ initial_capital의 95%, `orders/chance`로 수수료·최소금액 로드, `POST /v1/orders/test` 매수/매도 각 1회 성공, Paper 모드 누적 실행 시간 ≥ config 값(기본 48h) 기록 확인, 최근 백테스트 run 존재
5. 하나라도 실패 → 프로세스 종료 + 사유 로그. 코드 수정 없이는 우회 불가하게 `live_guard.py` 단일 지점에 집중.
6. 첫 LIVE 주문은 `max_order_krw`를 ₩6,000으로 낮춘 "Small Live Order" 단계 → 체결/정산/대조 확인 후 정상 한도 적용.

---

## 8. DAY 1 구현 계획 (파일 단위)

목표: **"1년치 KRW-BTC 1m 데이터를 받아 baseline 전략 백테스트 리포트가 출력된다"** + Upbit 클라이언트/WS가 실데이터로 검증된다.

| # | 파일 | 내용 | 검증 |
|---|---|---|---|
| 1 | `.gitignore`, `.env.example`, `backend/pyproject.toml`, `config/config.yaml` | uv 프로젝트, ruff/mypy(strict)/pytest 설정, 의존성(httpx, websockets, pydantic-settings, sqlalchemy[asyncio], aiosqlite, alembic, numpy, pandas, pyarrow, structlog, PyJWT, pyyaml) | `uv sync`, `ruff`, `mypy` 통과 |
| 2 | `app/core/types.py`, `app/core/events.py`, `app/core/clock.py`, `app/core/logging.py` | 공용 enum/dataclass, EventBus, Clock 추상화, JSON 로깅 + secret redaction | unit test: redaction |
| 3 | `app/config/settings.py` | .env + yaml 병합, TradingMode 파싱, LIVE 값 기본 차단 | test: 모드 파싱, 잘못된 모드 거부 |
| 4 | `app/exchange/upbit/auth.py` | JWT HS512, query_hash(GET/POST, 배열) | test: 공식 문서 예시 해시 재현 |
| 5 | `app/exchange/upbit/tick_size.py` | 호가단위 테이블, 라운딩 | test: 경계값 |
| 6 | `app/exchange/upbit/rate_limiter.py` | 그룹별 token bucket, Remaining-Req 파싱 | test: 한도 초과 대기, 헤더 파싱 |
| 7 | `app/exchange/upbit/models.py`, `errors.py`, `rest_client.py` | 공개: candles/ticker/orderbook; 인증: accounts, orders/chance, order 조회 (주문 생성은 Day 2·4) — retry(5xx/네트워크), 429/418 처리 | mock 서버 test + 실제 public API 호출 1회 |
| 8 | `app/exchange/upbit/ws_client.py` | 구독, backoff 재접속, PING, health counters | mock WS test(끊김→재접속), 실 연결 60초 smoke |
| 9 | `app/database/engine.py`, `models.py`, `repository.py`, `migrations/` | WAL pragma, 테이블 전체 정의, candles/backtest_runs repository 우선 구현 | test: 임시 sqlite CRUD |
| 10 | `app/market/historical.py`, `scripts/fetch_history.py` | 역방향 페이지네이션 수집, 중복 제거, parquet + DB 저장, gap 리포트 | 1년치 1m 수집 실행, 건수/gap 출력 |
| 11 | `app/market/candle_engine.py`, `app/market/indicators.py` | 1m→5m/15m/1h 리샘플, EMA/RSI/ATR/VolSMA 증분 계산 | test: pandas 기준값 대비 오차 |
| 12 | `app/strategy/base.py`, `trend_momentum.py`, `registry.py` | 인터페이스 + baseline | test: 합성 데이터에서 교차 시 BUY/SELL |
| 13 | `app/execution/fill_model.py` | 수수료·슬리피지·최소주문·틱 반영 체결 시뮬 (백테스트/페이퍼 공용) | test: 수수료/슬리피지 계산 |
| 14 | `app/portfolio/accounting.py` | 순수 함수: equity, avg entry, realized/unrealized, HWM, DD | test: 시나리오 기반 |
| 15 | `app/backtest/engine.py`, `metrics.py`, `report.py`, `scripts/run_backtest.py` | next-bar fill, 전 지표, equity curve 저장, backtest_runs 기록 | 실행 → 리포트 출력 |
| 16 | `app/backtest/walk_forward.py`, `scripts/run_walk_forward.py` | rolling train/test, 파라미터 grid(작게), overfit 지표(train vs test 성과 비율) | 실행 결과 표 |
| 17 | `docs/day1-report.md` | 실행 결과, 테스트 결과, 남은 문제, 다음 단계 | — |

Day 1 완료 기준: `ruff check`, `mypy --strict`, `pytest` 전부 PASS + 백테스트/WF 리포트 파일 존재 + WS 실연결 smoke 로그.

---

## 9. 승인 요청 사항 (결정 필요)

1. 패키지 관리: **uv** 도입(brew 설치) — 승인?  (대안: 기존 poetry)
2. 기본 타임프레임 **15m** + 1h 추세 필터, 시장가 주문 — 승인?
3. 포지션 사이징: equity 90% 단일 포지션(자본 제약상) — 승인?  (대안: 50%로 낮춰 2단 분할, 단 최소주문 여유 감소)
4. Daily 경계 **KST 00:00** — 승인?
5. Trader/API 2프로세스 분리 — 승인?
6. Historical 수집 범위: 최근 **2년** 1m 캔들(≈100만 봉, 요청 ~5,300회, 약 12분) — 승인?

---

## 10. 승인 시 수정/추가 사항 (2026-09-24 반영)

| 항목 | 결정 |
|---|---|
| 패키지 관리 | **uv** + `backend/.python-version`(3.12) + `pyproject.toml`(`>=3.12,<3.13`) + `uv.lock`. 시스템 Python 3.14는 사용하지 않음 |
| Position Size | LIVE V1: `max_position_pct 0.30`, `max_order_krw 30,000`, `max_concurrent_positions 1`. `risk.by_mode`로 BACKTEST/PAPER/LIVE 별도 설정. 손실 후 주문 증액 로직 없음(`position_sizer.py`는 equity 비율·상한만 사용, 테스트 `test_size_never_grows_after_loss`) |
| Daily 경계 | KST 00:00 (`system.report_timezone`), DB는 UTC |
| 프로세스 | trader / api 2프로세스. SQLite WAL + `busy_timeout 5s` + `with_lock_retry`(지수 백오프). 2엔진 동시 쓰기 테스트 `test_two_engines_concurrent_writes_succeed` |
| Historical | Raw 1m = source of truth(`candles` 테이블, UNIQUE(market,tf,open_time), insert-or-ignore). 15m/1h는 `CandleAggregator`로 리샘플(live/backtest 동일 코드). DB 범위 기반 resume(head/tail 자동 보충). `validate_1m`: duplicates / out_of_order / gaps / OHLC 위반 |
| A. Fill Ambiguity | `FillModel.resolve_intrabar_exit`: 같은 바에서 Stop과 Target 동시 도달 → **STOP 우선**. 갭으로 Stop을 뚫고 시작하면 Stop가 아닌 **시가**로 체결(더 불리). Target은 갭 상승 시에도 Target 가격으로만 체결. 테스트 `tests/unit/test_fill_model.py` 4건 |
| B. LIVE Gate | `app/config/live_guard.py`: 정적 게이트 4개(TRADING_MODE, `live_gate.enabled`, `LIVE_CONFIRM` 문구, 키 존재) + 증거 게이트(paper_hours ≥48, paper_trades ≥10, critical_errors =0, reconcile OK, orders/test OK, 최근 14일 내 백테스트). `LiveEvidence`는 Day 4에 DB에서 채움. 순서 BACKTEST → PAPER → /orders/test → Small LIVE(`small_live_max_order_krw 6000`) → LIVE |

### Day 1 구현 중 확인된 Upbit 실제 동작
- REST `Remaining-Req` 헤더의 그룹명은 문서와 달리 **`candles`**(복수)로 옴: `group=candles; min=600; sec=9`. 클라이언트에서 alias 매핑.
- 순차 요청 6 req/s에서도 캔들 API가 **약 10% 429** 반환 → 기본 candle 그룹 속도를 4 req/s, burst 3으로 하향. 429는 1초 대기 후 재시도로 자동 복구.
- WebSocket 60초 스모크: ticker 84 / trade 132 / candle.1m 19 메시지, 재접속 0, latency EMA ≈ 200ms.

---

## 11. DAY 2 반영 사항 (2026-09-24)

### Bot State (4상태) — `app/core/state.py`
```
MAINTENANCE ──(6개 복구 체크 전부 PASS)──► RUNNING ◄──► PAUSED
     ▲  ▲                                     │  │
     │  └──── EXCHANGE_OUTAGE (REST 5xx/불가, 또는 거래소 자체 체결 정지) ◄─┘  │
     │                                                                        ▼
     └──── 수동 RESUME(manual=True) ◄──────────────────────────────────── HALTED
```
- `HALTED`에서 나가는 유일한 경로는 **명시적 RESUME → MAINTENANCE → 복구 체크 → RUNNING**. 자동 재개 없음(프로세스 재시작 후에도 HALTED 유지).
- 복구 체크 6개(`RecoveryChecker`): websocket_connected · market_data_fresh · rest_ok · reconcile_balance · reconcile_open_orders · reconcile_position. REST 불가 시 reconcile 3개는 `skipped`(실패로 간주하지 않음).
- Stale 분류(`OutageClassifier`, 점검 시간 하드코딩 없음): 데이터 stale → REST 프로브
  - REST 불가/5xx 또는 거래소 최근 체결 자체가 stale → `EXCHANGE_OUTAGE` → MAINTENANCE(신규 주문 금지, 자동 복귀 가능)
  - 거래소는 체결 중인데 우리 피드만 죽음 → `LOCAL_FAULT` → stale×2 유예 후 HALTED(`UNEXPLAINED_STALE_LOCAL_FAULT`)
  - 판단 불가 → HALTED(`UNEXPLAINED_STALE`)
  - `StatusSource` 프로토콜로 공식 공지(Upbit announcement WS) 소스를 나중에 끼울 수 있음(우선 적용).
- HALTED 트리거 목록: DAILY_LOSS_LIMIT, MAX_DRAWDOWN, RECONCILE_FAILED(_DURING_RECOVERY), API_ERRORS, DATABASE_FAILURE, ORDER_UNKNOWN_UNRESOLVED, UNEXPLAINED_STALE(_LOCAL_FAULT), MANUAL_KILL.

### Order Manager — `app/execution/order_manager.py`
- 멱등성: `client_order_id`(=Upbit `identifier`, `{run_id}-{seq:06d}-{side}`) DB UNIQUE. 같은 id 재제출은 거래소 호출 없이 기존 레코드 반환.
- 단일 in-flight: SUBMITTED/OPEN/PARTIALLY_FILLED/UNKNOWN 존재 시 신규 주문 거부.
- UNKNOWN: 전송 후 네트워크 오류 → UNKNOWN 저장 → `identifier` 조회로 해소(FILLED/…/NOT_FOUND→REJECTED). 조회도 실패하면 UNKNOWN 유지 + 신규 주문 차단, 재시작 시 rehydrate 후 재시도. 미해소 시 KillSwitch.
- 거래소 측 `DuplicateIdentifier`(크래시 후 재제출)는 조회로 해소.
- 부분체결 후 취소(Upbit 시장가 매도 잔량 `cancel`) → `CANCELLED` + `executed_volume` 반영 + `ORDER_PARTIALLY_FILLED` 이벤트.
- 상태 변경마다 DB 저장(거래소 호출 **전**에 SUBMITTED 저장 → 크래시 흔적 보장).

### PAPER — `app/execution/paper_gateway.py`
- 실시간 ticker 가격 + `FillModel`(수수료 0.05%, 슬리피지 3bps)로 체결. Upbit식 거부(`under_min_total_bid`, `insufficient_funds_bid/ask`, 중복 identifier).
- 원장(ledger)은 `bot_state.gateway`에 JSON 저장 → 재시작 시 가상 계좌 연속성.
- 장애 주입(테스트): network_before/after_accept, reject, partial, get_order 실패, ping 실패.
- 주문 엔드포인트 코드 경로 없음. `TraderService`는 LIVE 모드에 PaperGateway가 주입되면 기동 거부.

### Portfolio / Reconciliation
- `PortfolioManager`: `PortfolioState` + 스탑(ratchet-up only) + 일별 트래커(KST) + 연패/마지막 손실 시각. 전체 상태를 `bot_state.portfolio`에 JSON 저장, trades/fills/snapshots는 이력 테이블.
- `Reconciler`: 잔고(KRW ±100, BTC ±0.00001) · 미체결 주문 집합 · 포지션(거래소 BTC>dust ⇔ 내부 포지션 ⇔ open trade row) 3종. FAIL → HALTED.

### LIVE Gate 분리
- 정적 게이트 0번에 **`strategy_approved`**(`live_gate.approved_strategies`) 추가. 인프라가 모두 정상이어도 전략 미승인 시 `LIVE_BLOCKED reason=STRATEGY_NOT_APPROVED`. 현재 목록 비어 있음(baseline 미승인).

### Alembic
- `backend/alembic.ini`, `app/database/migrations/`. 0001 baseline = Day 1 스키마. 기존 `create_all` DB(`candles` 존재)에는 DDL을 건너뛰고 버전만 stamp → 105만 캔들 보존 확인. 이후 스키마 변경은 0002+ 마이그레이션으로.

### Reliability 교훈
- 동기식 stdout 로깅이 파이프 정체 시 asyncio 루프를 15분간 정지시킴(WS PING 중단 → 서버 연결 종료, 타이머 미발화) → `QueueHandler/QueueListener` 비차단 로깅으로 교체, httpx 요청 로그 억제.

---

## 12. DAY 3 반영 사항 (2026-09-25)

### 접속 구조
```
Phone/Notebook ──(WireGuard, tailnet only)──► Tailscale Serve (HTTPS, *.ts.net, 443)
                                                  │  proxies to 127.0.0.1:8787, injects Tailscale-User-Login
                                                  ▼
                                      FastAPI (app.main_api) — loopback bind 강제
                                        ├── GET /api/*        read-only (DB만 읽음)
                                        ├── POST /api/control/{PAUSE|RESUME|KILL}  token + CSRF header + confirm + audit
                                        └── /  React dist (정적)
                                                  │  SQLite WAL (읽기) / commands (쓰기)
                                                  ▼
                                      Trader (app.main_trader, PAPER 상시) — commands 폴링 2s, TTL 검사
```
- Trader/API는 완전히 독립된 프로세스(각각 `scripts/*_daemon.sh`). API 장애는 트레이더에 영향 없음, 트레이더 OFFLINE은 대시보드가 heartbeat 30s 초과로 표시.
- Trader → API 데이터 전달은 오직 DB(`bot_state.{bot,market,portfolio,runtime_PAPER,kill_switch}`, 이력 테이블)를 통해서만.

### Commands lifecycle (migration 0002)
`command_id`(UNIQUE, 클라이언트 생성 → 멱등) · `status` PENDING/ACKNOWLEDGED/EXECUTED/FAILED/EXPIRED/REJECTED · `ts(requested_at)/expires_at/acknowledged_at/executed_at/failure_reason/audit(JSON)`. TTL은 API가 설정(config `command_ttl_sec`, `kill_command_ttl_sec`). 트레이더는 PENDING을 읽을 때 `now > expires_at`이면 EXPIRED 처리 후 실행하지 않음. 동일 상태 명령은 FAILED(`already PAUSED`).

### LIVE readiness
`app/monitoring/readiness.py` — Infrastructure(키) · Paper Runtime(RUNNING 누적 초, `runtime_PAPER`) · Paper Trades(closed) · Critical Errors(현재 paper epoch 이후 CRITICAL 이벤트) · Reconciliation(마지막 결과) · Order Test(`bot_state.order_test`, Day 4) · Backtest(14일 내 + PF ≥ 1.0 & 수익 ≥ 0) · Strategy Approved. 하나라도 실패 시 `LIVE BLOCKED — <reason>`; strategy 미승인이 최우선 사유.

### Frontend
React 19 + Vite 8 + TypeScript, `lightweight-charts@4`. 탭 2개(Trading / System) + 상단 Challenge 헤더 + HALTED 경고 배너 + LIVE readiness 패널. 5~30s 폴링. Node 24 LTS 고정(`.nvmrc`, `engines`, `package-lock.json` 커밋).

---

## 13. DAY 4 반영 사항 (2026-09-25)

### 운영 (launchd)
- `~/Library/LaunchAgents/com.quant.trader.plist` (`/usr/bin/caffeinate -i .venv/bin/python -m app.main_trader`, `TRADING_MODE=PAPER`), `com.quant.api.plist` — 각각 `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval` 10s/5s, stdout/err → `logs/launchd_*.log`. gui 도메인(로그인 후 시작). 서로 독립: 한쪽 kill -9 → 해당 에이전트만 재기동, 상태는 DB에서 복원.
- 기존 `scripts/*_daemon.sh`(nohup)는 개발용으로 유지.
- 재부팅 대응: 로그인 시 LaunchAgents 자동 기동. FileVault 자동 로그인이 꺼져 있으면 로그인까지는 정지(문서화).

### Sleep policy
- `app/monitoring/power.py`: `pmset -g batt / -g / -g assertions` 파싱 → AC 여부·배터리·`PreventUserIdleSystemSleep` assertion(caffeinate)·sleep/displaysleep 값·경고 목록. `/api/health.power`, 대시보드 상단 경고 + System 카드.

### Upbit private gateway
- `UpbitRestClient`: `test_order`(POST /v1/orders/test, group order-test), `create_order`(POST /v1/orders, 재시도 없음), `cancel_order`(DELETE /v1/order). 바디는 `market/side/ord_type/price|volume/identifier`.
- `app/exchange/upbit/live_gateway.py`: `ExchangeGateway` 구현(시장가 매수 `price`, 매도 `market`, identifier=client_order_id, 상태 매핑 wait/watch→OPEN, done→FILLED, cancel→CANCELLED, 주문 수수료를 체결별 funds 비례 배분). 오류 매핑: 네트워크/429→`GatewayNetworkError`(UNKNOWN 경로), 4xx→`GatewayRejectedError`, identifier 중복→`DuplicateIdentifierError`, 404→`OrderNotFoundError`. 잔고/미체결/reference price/ping 제공 → 기존 OrderManager/Reconciler/RecoveryChecker 그대로 재사용.
- `app.main_trader.build_live_service`: `assert_mode_is_safe`(정적 게이트) → `evaluate_readiness`(증거 게이트) → `run_preflight`(accounts/chance/orders-test) 모두 통과해야 LiveGateway 생성. 하나라도 실패 시 `SystemExit("LIVE_BLOCKED reason=…")`.

### Static Public IP 게이트
- `app/monitoring/network.py`: 3개 소스(ipify/amazonaws/ifconfig.me) IPv4 일치 시에만 관측 채택, CGNAT(100.64/10)·사설·루프백·IPv6 거부. 트레이더가 `live_gate.public_ip_check_interval_sec`(3600)마다 관측해 `bot_state.public_ip.history`(최대 500)에 저장.
- 판정: 설정된 `static_public_ip`가 공인 IPv4이고, 모든 관측이 그 값과 같고, 관측 기간 ≥ `static_ip_min_observation_hours`(24). 아니면 `STATIC_PUBLIC_IP_NOT_VERIFIED` — live_guard 증거 게이트·readiness 항목·preflight 2단계에서 모두 차단.

### Preflight (`app/services/live_preflight.py`)
credentials → static IP → `GET /v1/accounts` → `GET /v1/orders/chance`(수수료·최소금액을 `bot_state.exchange_terms`에 저장) → `POST /v1/orders/test`(₩6,000 dry-run). 결과/차단 사유를 `bot_state.order_test`에 기록(시크릿 미포함). 실주문 엔드포인트는 호출하지 않음(테스트로 보장).

### Daily report (`app/monitoring/daily_report.py`)
KST 일자 경계. 전일 마지막 스냅샷 → 당일 마지막 스냅샷으로 시작/종료 equity, 당일 청산 트레이드 집계, HWM/MDD, 포지션, 봇 상태, reconcile, CRITICAL 수. `daily_stats` upsert + `reports/YYYY-MM-DD.md`. 트레이더가 KST 자정 첫 tick에 전날분 생성, `scripts/daily_report.py`로 수동 생성, `/api/reports`, `/api/reports/{date}`.

---

## 14. DAY 5 반영 사항 (2026-09-25)
- 전략 A `trend_pullback` / B `donchian_breakout` / C `volatility_breakout` 추가(`app/strategy/`), `Timeframe.H4` 추가. 레지스트리 등록만으로 config 교체 가능.
- 연구 하네스 `app/backtest/research.py`: 동일 조건 WF(coarse grid → train 선택 → OOS), 이웃 파라미터 안정성(1-step neighbours 양의 기대값 비율, cliff), 비용 스트레스(1.0/1.5/2.0x), 승인 매트릭스. `scripts/research_day5.py` → `docs/research/`.
- Epoch 분리: `runtime_{MODE}`(infra: runtime/critical/reconcile) vs `strategy_epoch_{MODE}`(strategy id/version/params; paper trades·signals 기준). 전략/버전 변경 시 strategy epoch만 리셋.
- `bot_state.challenge`: 첫 LIVE FILLED 매수의 KST 날짜(`start_date`) + `live_activated_at` 자동 기록. Day N·리포트·API가 이를 우선 사용.
- Static IP: 24h 동일 = `IP_STABILITY_MINIMUM_PASS`(ISP 고정 IP 단정 아님), 동일 IP·24h 미달은 `pending`(읽기 전용 smoke 허용, LIVE 차단), 변경 시 즉시 FAIL.
- `ZeroVolumeError`: 1e-8 BTC 미만 수량 매수는 체결 불가로 거부(엔진/PAPER).
- 결과: **NO_STRATEGY_APPROVED** (`docs/day5-report.md`).

---

## 15. Strategy Research Round 2 (2026-09-25)

### 데이터셋 확장
- `scripts/fetch_history.py --tf 1h --start 2017-09-25`: Upbit `GET /v1/candles/minutes/60`(UTC 시간 버킷)로 **2017-09-25 → 현재** 1h 캔들 78,763봉, coverage 99.83%(결측 133봉, 대부분 2017년 초기·점검 시간대), 중복/역순/OHLC 위반 0. `candles` 테이블에 `timeframe='1h'`로 저장(기존 1m 2y 보존).
- 교차검증: 최근 2y 구간에서 REST 1h vs 1m 리샘플 1h 18,170봉 비교 — OHLC 불일치 2건(양끝 부분 바), 거래량 0.1% 초과 불일치 3건 → 동일 데이터로 판단.
- `UpbitCandle.timestamp`는 2019-03 이전 캔들에서 null → Optional 처리.
- `HistoricalSync.sync(tf=)`로 임의 분봉 단위 수집·resume·검증 일반화(`validate_series`).

### 하네스
- `Timeframe.D1` 추가. 엔진 `run(start_at=)`: warm-up 구간(지표·채널 상태만, 주문·equity 없음) → WF 각 윈도우에 240d warm-up prefix를 붙여 1d EMA100/200이 창 시작 시점에 준비됨(look-ahead 없음: prefix는 과거 데이터).
- `walk_forward`가 윈도우별 OOS 트레이드 목록과 B&H 수익률을 보존 → `research2.py`의 trade concentration(최대 승리, top1/3/5 기여, 중앙값, ex-top1/3/5 수익), regime(B&H 부호별 승률), 3단 판정(APPROVED ≥50 trades·전항목 / REVIEW 30~49 또는 집중 위험 / NOT_APPROVED).
- 전략 D `trend_following_4h`(4h, 1d EMA 추세) / E `trend_following_1d`(1d) — `app/strategy/trend_following.py`, 24조합 coarse grid, WF train 540d / test 180d / roll 180d.

---

## 16. Pre-LIVE Validation 구조 (2026-09-25)
- 승인 상태 모델: `live_gate.strategy_status.<strategy_version> = {research_approved, paper_approved, live_approved}`. `approved_strategies` 제거. `live_guard`/readiness는 `live_approved`만 LIVE 허용 근거로 사용.
- 파라미터 동결: `config/strategies/trend_following_4h_v1.yaml` — WF 15윈도우 선택값의 차원별 mode/median(entry_n 30, trend_ema 100, atr_stop 3.0, exit_n 20) + 고정값(ATR14, trail 4.0, trend-loss exit). `strategy.version_file`로 로드되면 name/timeframe/params를 덮어씀. 고정 파라미터 재평가(선택 없음): 동일 15 OOS 윈도우 stitched +226%, MDD 7.2%, 105 trades, PF 4.06, 13/15 수익.
- Strategy epoch: `strategy_epoch_{MODE}` = strategy_id/version/started_at/parameter_hash(sha256[:16])/snapshot/timeframes/counters; 버전·해시 변경 시 이전 epoch를 `strategy_epoch_history`에 보관 후 새 epoch. Infrastructure epoch(`runtime_{MODE}`)는 별도.
- Warm-up(4h/1d 전략): 1단계 1h 히스토리(`warmup_days_1h`=400d)를 1h-base aggregator로 재생 → UTC-day 경계에서 2단계 1m 재생(`warmup_days`=12d) → 라이브. 앵커는 저장 데이터 최대시각(리플레이 결정성), 라이브는 REST tail 보충.
- Private preflight 6단계: credentials → static IP(pending 허용) → accounts(`exchange_balance`) → orders/chance(`exchange_terms`) → orders/test(dry-run) → **challenge_capital**(거래소 KRW = ₩100,000 ±500, BTC 0). 실주문 없음.
- Challenge 상태(`bot_state.challenge`): initial_capital, funded, status NOT_STARTED/STARTED, start_date/first_fill_at/live_activated_at, external_deposit/withdrawal_after_start(+flows). LIVE Reconciler가 설명되지 않는 잔고 차이를 외부 입출금으로 기록(HALT는 기존대로).
- Replay/synthetic E2E(`app/validation/replay_scenarios.py`, 25 시나리오): 실제 PAPER 파이프라인(TraderService+PaperGateway+임시 DB)에 합성 1m 경로 재생. `scripts/replay_e2e.py` → `bot_state.replay_e2e`. pytest에도 포함.
- Restart recovery 증거: `scripts/restart_recovery_check.sh`(SIGKILL → launchd 재기동 → 상태/reconcile 확인) → `bot_state.restart_recovery`.
- Readiness 그룹: Research / Infrastructure / Private API / Strategy PAPER / LIVE.
- 발견·수정 결함: 트레일링 스탑 상향(`update_stop`)이 메모리에서만 갱신되어 재시작 시 오래된 스탑이 복원됨 → 변경 즉시 persist.

---

## 17. D PAPER 승격 · 실행 정책 · Decision snapshot (2026-09-25)
- **승격**: `strategy.version_file: strategies/trend_following_4h_v1.yaml` → 트레이더 재시작 → 새 Strategy Epoch(`trend_following_4h_v1`, hash `c0e59de4f7b70c0c`, 14:09:28Z), 이전 epoch(`trend_momentum_v1`)는 `strategy_epoch_history`에 보관, PAPER 장부·원장 ₩100,000으로 리셋(`paper_books_reset`), Infrastructure epoch(05:09Z) 유지. Warm-up: 1h 9,585봉 + 1m 18,099봉 → 4h/1d 지표 ready.
- **Initial stop 기준(전 모드 동일)**: `signal 4h close − 3×ATR14(4h)`. 백테스트 엔진(`pending.stop_price`), PAPER/LIVE(`apply_order(stop=sig.stop_price)`) 모두 신호 바 종가 기준. fill price 기준이 아님 — 9년 연구와 동일하므로 변경하지 않음(변경 시 재검증 필요).
- **ExecutionMode**(`app/core/state.py`): 4-state machine 위의 파생 정책. `RiskManager`는 FROZEN이면 전부 거부(`FROZEN_NO_AUTO_EXECUTION`/`BOT_STATE_*`), EXIT_ONLY면 BUY만 거부(`EXIT_ONLY_NO_NEW_ENTRY`); 손실한도는 kill 후에도 SELL 경로를 계속 통과시킴. 틱 스탑은 `sm.exits_allowed`(≠FROZEN)에서 실행. SELL 수량은 항상 보유량(`order_volume=ctx.btc`), 증가 불가.
- **Signal/Execution 분리**: 포지션이 없는 SELL 신호는 Risk를 거치지 않고 `risk_decisions`에 `SUPPRESSED_ALREADY_FLAT`로 기록(이벤트 없음). RISK_REJECTED 이벤트 payload에 `signal/execution/execution_mode`. `/api/signals`의 `risk.execution ∈ {EXECUTED, BLOCKED, SUPPRESSED}`.
- **Decision snapshot**: 전략 TF 평가·틱 스탑마다 `bot_state.decision_{MODE}`에 저장(strategy_version, evaluated_at, market_bar_time, decision/reason, daily_close/ema/trend_ok, close_4h/donchian_upper/breakout_ok/donchian_lower/atr, position/entry/initial_stop/current_stop/trailing/channel_exit/trend_loss, risk_status). `GET /api/decision`(+next_evaluation) → Trading 탭 상단 카드.
- 리플레이 시나리오 26/27/28 추가(손실한도 HALT에서 보호성 스탑 실행 · PAUSED에서 스탑 실행·수량≤보유 · 청산 후 채널 SELL 억제). 총 28.

---

## 18. PAPER 검증 중 동결 (2026-09-25 23:09 KST ~ 09-26 23:09 KST)
- `trend_following_4h_v1` 코드·frozen 파라미터·ExecutionMode·RiskManager 규칙 변경 금지. 불가피 시 사유 보고 → strategy epoch / 24h 리셋 필요성 판단 후 적용.
- 평가 지표는 방식별 분리 유지(`docs/strategy-D-rules.md` 부록): WF OOS MDD 6.72% / Frozen 9Y 11.3%(Risk Review warning) / Recent 2Y 4.1%.
- Small LIVE 측정 준비 모듈 `app/monitoring/slippage.py`(미배선).
- 보고 트리거: 첫 자연 BUY · 첫 자연 SELL · CRITICAL · Reconciliation FAIL · Static IP 24h · PAPER runtime 24h — `scripts/trigger_watch.sh`가 감시.

## 19. Research Round 3 — isolated shadow study (2026-09-27)

`backend/app/research/round3/` runs D1–D6 (1h/2h breakout-horizon variants of D) as **shadow-only**
virtual portfolios next to a D0 replica, in a separate launchd agent `com.quant.shadow-round3`:

```
data/quant.db (sqlite ?mode=ro) ──1h history (400d) ──► warm-up (base 1h, flushed at UTC midnight)
                               └─1m stream ─────────► 7 × ShadowEngine (D0 replica, D1..D6)
                                                        │  same FillModel / sizing / accounting /
                                                        │  STOP-first / next-bar fill as BacktestEngine
                                                        ▼
                                     data/research_round3.db (decisions, fills, trades, portfolio,
                                     forward_tracking, d0_consistency, divergences, heartbeat)
```

Guarantees: no import path to `app.exchange`, `OrderManager`, `PaperGateway`, `RiskManager`,
`app.services`, `app.config.settings` (.env) or network libraries (AST test, transitive); market DB
handle is read-only (tested); deterministic replay on restart with INSERT OR IGNORE keyed by
`(strategy_id, market_bar_time)`; `Timeframe.H2` exists only for this study. The trader, API and
watcher agents are not touched (`launchd_install.sh install com.quant.shadow-round3`).
Historical evaluation: `scripts/research_round3.py` (fixed-parameter WF, parallel, deterministic).

## 20. LIVE transition design (D0, 2026-09-29) — prepared, NOT activated

- **Single trader process**: `com.quant.trader` is the only trader label; its plist is rendered from
  `$TRADING_MODE` (default PAPER). `scripts/live_transition.sh` (run only after the user types
  `LIVE_APPROVE_D0`) checks the 4 static gates, verifies the PAPER book is flat, runs the private
  preflight, boots the PAPER trader out, verifies no `app.main_trader` process remains, then
  installs the plist with `TRADING_MODE=LIVE`. PAPER and LIVE never run concurrently.
- **LIVE books start from exchange truth** (`TraderService._init_live_books`): on the first LIVE
  start of a strategy epoch the exchange must show BTC = 0, no open orders and KRW within the
  challenge tolerance; the PAPER book under `bot_state.portfolio` is archived
  (`portfolio_archive_<ts>`), the LIVE book starts with `krw = exchange KRW` and
  `challenge.live_activated_at` is recorded. `challenge_started_at` (= first LIVE BUY fill) is a
  separate field written by `_record_challenge_start`. Later restarts restore the LIVE book.
- **Execution metrics** (`execution_metrics` table, migration 0003): every filled order records
  signal/send/fill prices, slippage vs signal and vs the reference price at send, fee, quantity,
  ATR + initial stop + stop distances (BUY), exit reason/trigger/realized PnL/return/holding (SELL).
  `GET /api/executions`; dashboard "Executions (slippage)" section.
- **Exchange terms**: `build_live_service` applies `orders/chance` (min_total, fees) to the
  RiskManager when stricter than config. LIVE sizing stays 30% / cap ₩30,000 / 1 position.
- **Automation after approval**: BUY/SELL/stop/trailing/channel/trend exits, restart recovery and
  reconciliation run without per-order approval; PAUSE (EXIT_ONLY) and KILL (FROZEN) remain
  available via the control API. Approval is needed again only for policy changes.
