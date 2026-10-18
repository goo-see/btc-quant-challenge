> **Public copy / 공개용 사본.** 실제 운용 레포에서 비밀정보(API 키·IP·호스트명·개인 식별자)를 제거하고 LIVE 게이트를 **비활성(기본값 PAPER/BACKTEST)** 으로 둔 템플릿입니다. `.env`는 포함되지 않으며(`.env.example` 참고) `data/`·`logs/`·`reports/`(런타임 DB·로그·스냅샷)도 제외했습니다. 이 코드로 실제 자금을 운용하는 것은 전적으로 사용자 책임이며 수익을 보장하지 않습니다. 문서의 날짜·금액·성과는 원 프로젝트의 기록입니다.

# ₩100K → ₩1M Bitcoin Quant Challenge

Upbit KRW-BTC 단일 종목 자동매매 시스템. 초기 실계좌 ₩100,000. **₩1,000,000은 콘텍츠 목표이며 보장 수익이 아닙니다.**
최적화 기준은 Net Return · Max Drawdown · Profit Factor · Expectancy · 비용 · 안정성입니다.

> 상태: **Strategy D `trend_following_4h_v1` PAPER 운영 중**(2026-09-25 23:09 KST 승격, 24h operational gate 진행). research_approved=true / paper_approved=false / live_approved=false. **LIVE는 BLOCKED**(`live_gate.enabled=false`). Challenge 원금 ₩100,000 입금 전(계좌 ₩10,039).

## 문서
- `docs/00-architecture.md` — 아키텍처, 데이터 모델, 데이터 흐름, 개발 계획
- `docs/01-upbit-api-policy.md` — Upbit 공식 API 정책 정리(2026-09-24 조회)
- `docs/day1-report.md` — DAY 1 결과 보고

## Setup (macOS)
```bash
brew install uv
cd backend
uv python install 3.12      # backend/.python-version 에 고정
uv sync                     # uv.lock 기준 재현 설치
cp ../.env.example ../.env  # TRADING_MODE=BACKTEST 로 시작. API 키는 여기에만.
```

## DAY 1 명령
```bash
cd backend
uv run ruff check app tests ../scripts && uv run mypy && uv run pytest -q     # 품질 게이트
uv run python ../scripts/rest_smoke.py                  # Upbit REST 연결 확인(공개 API)
uv run python ../scripts/ws_smoke.py --seconds 60       # Upbit WebSocket 60초 확인
uv run python ../scripts/fetch_history.py --years 2     # 1m 캔들 수집(중단 후 재실행 시 이어받기)
uv run python ../scripts/fetch_history.py --validate-only
uv run python ../scripts/run_backtest.py --base 1m      # 베이스라인 백테스트 (config/config.yaml 파라미터)
uv run python ../scripts/run_walk_forward.py --train-days 180 --test-days 60 --base 15m
```

## DAY 2 명령
```bash
cd backend
uv run alembic upgrade head                              # 스키마 마이그레이션 (기존 DB 데이터 보존)
uv run alembic check                                     # 모델 ↔ DB 드리프트 확인
TRADING_MODE=PAPER uv run python ../scripts/run_paper.py --seconds 300 --reset   # 가상 ₩100,000 PAPER 실행
TRADING_MODE=PAPER uv run python ../scripts/run_paper.py                         # 상태 이어받아 계속 (Ctrl-C 종료)
```
운영 상태: `RUNNING / PAUSED / MAINTENANCE / HALTED`. HALTED는 `commands` 테이블에 `RESUME`(confirmed) 명령이 들어와야 해제되며, 복구 체크 6종을 통과해야 RUNNING으로 돌아갑니다.

## DAY 3 — 상시 운영 · 대시보드 · Tailscale
```bash
# 1) 상시 PAPER 트레이더 (caffeinate로 잠자기 방지, nohup으로 셸과 분리)
scripts/paper_daemon.sh start|stop|status|log
# 2) 대시보드 API (127.0.0.1:8787 전용, 빌드된 React 정적 파일 서빙)
scripts/api_daemon.sh start|stop|status|log
# 3) 프론트엔드 빌드 (Node 24 LTS — .nvmrc / package.json engines 로 고정)
export PATH="/opt/homebrew/opt/node@24/bin:$PATH"; cd frontend && npm ci && npm run build
# 4) Tailscale Serve: tailnet 안에서만 https://<machine>.<tailnet>.ts.net 으로 접속 (public 노출 없음)
brew install --cask tailscale        # 설치 후 앱에서 로그인
alias tailscale=/Applications/Tailscale.app/Contents/MacOS/Tailscale
tailscale serve --bg 8787            # 재부팅 후에도 유지. 끄기: tailscale serve --bg 8787 off
tailscale serve status
```
- API는 loopback 외 바인딩을 **거부**합니다(`main_api.assert_loopback`). Tailnet IP를 코드/설정에 적지 않습니다. Funnel(공개)은 사용하지 않습니다.
- 제어(Pause/Resume/Kill)는 `.env`의 `DASHBOARD_CONTROL_TOKEN`(≥16자) + `X-Requested-With: quant-dashboard` 헤더 + 확인 문구가 모두 있어야 하며, Tailscale Serve가 주입하는 `Tailscale-User-Login`을 감사 로그에 남깁니다. 쿠키를 쓰지 않으므로 CSRF 표면이 없습니다.
- 명령은 `commands` 테이블에 `PENDING → ACKNOWLEDGED → EXECUTED|FAILED`, TTL 만료 시 `EXPIRED`(PAUSE/RESUME 120s, KILL 600s). 트레이더 재시작 후 오래된 명령은 실행되지 않습니다.
- HALTED 상태에서 포지션이 남아 있으면 대시보드 최상단에 붉은 경고(사유·수량·평가금액·진입가·현재가·미실현손익·스탑·거래중지)를 표시합니다. 자동/수동 청산 버튼은 없습니다.

## DAY 4 — 운영(launchd) · Upbit private API 준비 · Daily Report
```bash
# launchd (운영 기준). Trader/API 별도 user agent, KeepAlive+RunAtLoad → 프로세스 종료/로그인 후 자동 복구
scripts/launchd_install.sh install|uninstall|status|restart <trader|api>
tail -f logs/trader.jsonl logs/launchd_api.out.log

# Upbit API 키 준비 절차 (LIVE용, 실거래 시작 아님)
uv run python ../scripts/check_public_ip.py     # 외부 통신 공인 IPv4 관측(3개 소스 일치 확인) + 이력 저장 + 판정
#   1) 출력된 공인 IPv4(예: 210.x.x.x — Tailscale 100.x / LAN 172.x 아님)를 config/config.yaml live_gate.static_public_ip 에 기입
#   2) 트레이더가 1시간마다 자동 관측 → 24h 이상 동일해야 STATIC_PUBLIC_IP 게이트 PASS (아니면 LIVE_BLOCKED reason=STATIC_PUBLIC_IP_NOT_VERIFIED)
#   3) Upbit Open API 키 발급: 권한 [자산조회, 주문하기]만, 출금 권한 금지, 허용 IP = 위 공인 IPv4
#   4) 사용자가 직접 .env 에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 입력 (코드/로그/문서에 절대 출력하지 않음)
uv run python ../scripts/private_smoke.py       # credentials → IP 게이트 → accounts → orders/chance → orders/test(dry-run). 실주문 없음
uv run python ../scripts/daily_report.py [--date YYYY-MM-DD]   # reports/YYYY-MM-DD.md + daily_stats (트레이더는 KST 00:00에 전날분 자동 생성)
```
- **Sleep policy(MVP)**: 외부 전원 + 뚜껑 열림 + 디스플레이 절전 허용 + 시스템 절전 방지(`caffeinate -i`가 launchd 트레이더의 부모 프로세스). `pmset disablesleep` 같은 영구 변경 없음. 뚜껑 닫힘 24/7은 미지원. 배터리 구동/절전 가능 상태는 대시보드 System 탭과 상단 경고에 표시.
- **LIVE 활성 조건**(둘 다 필요): Infrastructure Ready(키·고정IP·accounts·chance·orders/test·reconcile·paper 48h/10건·critical 0·최근 백테스트 품질) **AND** Strategy Ready(`live_gate.approved_strategies`). 정적 게이트 3개(`TRADING_MODE=LIVE`, `live_gate.enabled`, `LIVE_CONFIRM`)와 기동 시 preflight 재실행까지 통과해야 `LiveGateway`가 생성됨.

## Pre-LIVE Validation (Strategy D)
```bash
cd backend
uv run python ../scripts/private_smoke.py          # accounts → orders/chance → orders/test(dry-run) → challenge_capital(=₩100,000 ±500)
uv run python ../scripts/replay_e2e.py             # 25 deterministic replay scenarios on the real PAPER pipeline → bot_state.replay_e2e
../scripts/restart_recovery_check.sh               # kill -9 the launchd trader → auto-restart → state/reconcile check → bot_state.restart_recovery
# PAPER promotion of D (ONLY after challenge_capital PASS and explicit approval):
#   config/config.yaml  strategy.version_file: strategies/trend_following_4h_v1.yaml
#   scripts/launchd_install.sh restart trader     # new strategy epoch (trend_following_4h_v1); infrastructure epoch untouched
```
- 실행 정책(`ExecutionMode`, 상태 위에 계층화): RUNNING→ENTRY_AND_EXIT · PAUSED / 손실한도 HALT(DAILY_LOSS_LIMIT, MAX_DRAWDOWN)→**EXIT_ONLY**(신규 BUY 금지, 보호성 스탑/트레일/채널/추세 청산 허용, 보유량 초과 매도 불가) · MAINTENANCE / 무결성 HALT(reconcile, UNKNOWN 주문, DB, stale, API, manual kill)→**FROZEN**(자동 주문 없음).
- Signal과 Execution 분리: 신호는 항상 기록, 실행은 `EXECUTED / BLOCKED(사유) / SUPPRESSED(이미 청산됨)`로 표시. `GET /api/decision` + Trading 탭 "Current Decision" 카드(고정 규칙의 현재 임계값만, 예측 없음).
- 승인 상태 3단계(`live_gate.strategy_status`): `research_approved` → `paper_approved` → `live_approved`. `live_guard`는 **`live_approved`만** 읽습니다. 어떤 자동화도 LIVE를 켜지 않습니다.
- 운영 파라미터는 `config/strategies/<version>.yaml`에 동결(선정 근거·WF 선택 빈도 주석). 운영 중 자동 변경 없음.
- Strategy epoch(`bot_state.strategy_epoch_PAPER`): strategy_id/version/parameter_hash/snapshot + counters(bars_processed, signals, paper_orders/fills/trades, running_sec, critical_errors). Infrastructure epoch(`runtime_PAPER`)와 분리.
- Challenge: `initial_capital=100000`, `status NOT_STARTED`→첫 LIVE BUY FILLED 시 `STARTED`+`start_date`(KST)+`live_activated_at`; LIVE reconcile에서 설명되지 않는 잔고 변동은 `external_deposit/withdrawal_after_start`로 기록.

## 안전 원칙 (요약)
- `TRADING_MODE`는 `.env`에서만 설정. LIVE는 `live_gate.enabled` + `LIVE_CONFIRM` 문구 + 증거 게이트 모두 통과해야 기동.
- LIVE V1 노출 상한: 포지션 30% / 주문 ₩30,000 / 동시 포지션 1. 손실 후 증액 없음. PAPER도 동일 사이징(LIVE fidelity).
- 전략은 `live_gate.approved_strategies`에 등록되어야 LIVE 가능(인프라 준비 ≠ 전략 준비).
- 출금 기능 없음. API 키에 출금 권한을 부여하지 마세요. 대시보드는 Tailnet 내부 전용.

## Risk Warning
암호화폐 자동매매는 원금 전액 손실이 가능합니다. 이 저장소의 어떤 결과도 미래 수익을 보장하지 않습니다.
