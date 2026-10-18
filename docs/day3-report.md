# DAY 3 보고 — 2026-09-25

## 1. Git checkpoint
- DAY 2: `b42cfa3 feat: day2 paper trading, risk, order lifecycle, 4-state ops` · tag `day2-infra` (data/logs/db/.env 미포함 확인 후 커밋)
- DAY 3 변경분(백엔드 25개 경로 + `frontend/` 신규)은 **미커밋** — 승인 대기. `frontend/package-lock.json`(1,365줄)·`.nvmrc` 포함, `frontend/dist`·`node_modules`는 gitignore.

## 2. Node / Tailscale 설치 및 버전
| | 버전 | 비고 |
|---|---|---|
| Node | **v24.21.0** (`node@24`, Homebrew) | nodejs.org 기준 v24 Krypton = Active LTS(권장). `.nvmrc`=24, `engines: node >=24 <25, npm >=10`, `packageManager: npm@11.19.0` |
| npm | 11.19.0 | `package-lock.json` 커밋 대상 |
| Tailscale | **1.102.4** (cask, `/Applications/Tailscale.app/Contents/MacOS/Tailscale`) | 사용자가 설치·로그인, HTTPS 인증서 활성화 |
| Vite / React / TS | 8.3 / 19.2 / 6.0 | `lightweight-charts@4.2.3` |

## 3. Tailscale 접속 구조
```
Phone (Tailscale ON) ──WireGuard──► <machine>.<tailnet>.ts.net:443 (Serve, TLS 자동발급, tailnet only)
                                         └─ proxy → http://127.0.0.1:8787 (FastAPI, loopback 강제)
                                                     ├─ GET /api/*  (DB 읽기 전용)
                                                     ├─ POST /api/control/*  (token + CSRF hdr + confirm + audit + TTL)
                                                     └─ /  React dist
```
- `tailscale serve status` → `https://<machine>.<tailnet>.ts.net (tailnet only) |-- / proxy http://127.0.0.1:8787`
- `tailscale funnel status` → 동일(tailnet only). **Funnel 미사용.**
- 리스너: `python 127.0.0.1:8787` 뿐(0.0.0.0 없음). `main_api.assert_loopback`이 비-loopback `API_HOST`를 거부. Tailnet IP는 코드/설정에 없음.
- 머신명 `<machine>-macbookpro` → `<machine>`로 변경(`tailscale set --hostname`). `.ts` 한 단어 도메인은 구조상 불가.
- 막혔던 지점: 무료 tailnet의 HTTPS Certificates 미활성 → `tailscale cert` 500 → 사용자가 admin console에서 활성화 후 `serve --bg 8787` 성공. Serve `--bg`는 재부팅 후에도 유지.

## 4. 외부 모바일 접속 결과 (사용자 실측, Galaxy)
- Tailscale ON 상태에서 `https://<machine>.<tailnet>.ts.net` 접속 **성공**
- Trading 대시보드 정상 렌더링, 2열 카드, 가로 스크롤 없음, BTC 가격 실시간, `PAPER / ONLINE / RUNNING` 표시 → **PASS**
- 자동 검증: 로컬 `/api/health` 200, Tailnet `/api/health` 200(0.45s), 위조 `Tailscale-User-Login` 헤더는 Serve가 제거하고 실제 로그인 `<user>@github`이 감사에 기록됨.
- 실측 후 UI 수정 3건 반영: MDD `0.00%`(중립)/`-X.XX%`(적색), 상단 상태 사유 사람이 읽는 문구(`RECOVERY_CHECKS_PASSED`→`Healthy`, 원본은 System/Event Log 유지), BTC 수량 `0 BTC`/trailing zero 제거. 재빌드 후 새 번들(`index-DC0IimSg.js`) 서빙 확인.

## 5. Dashboard 구현
- 헤더: Challenge 진행바(손실 시 0%로 클램프 + "below start" 표기), Equity/Total/Today/MDD, 모드·ONLINE·상태 배지
- HALTED+포지션 경고 배너(사유·거래중지·수량·평가금액·진입/현재가·미실현·스탑) — 청산 버튼 없음
- Trading 탭: Portfolio 12카드, Position, Statistics 8카드, Equity curve(lightweight-charts area + 시작선), BTC 캔들(1m/5m/15m/1h, BUY/SELL 마커, ENTRY 가격선), Recent trades, Recent signals(RSI/EMA/Risk 결과)
- System 탭: Trader/Mode/Uptime/WS/REST/DB/Tailscale/Market data age/Reconciliation/Kill switch, Control(토큰 입력 + 확인문구 prompt), Recent commands(상태·감사), Event log
- LIVE readiness 패널(8항목 + `LIVE BLOCKED — reason`)
- 폴링 5~30s, 다크 모바일 우선 CSS, API 장애·heartbeat stale 경고

## 6. Paper 누적 runtime / trades
- 상시 인스턴스: `scripts/paper_daemon.sh`(caffeinate -i, nohup) pid 56763, 05:01 KST 14:01 시작(05:09 재시작 1회 — epoch_start 도입)
- **Paper Runtime 1.5h / 48h**, **Trades 0 / 10**, 시그널 9(전부 HOLD), reconcile 34회 OK, epoch 이후 CRITICAL 0
- 거래를 유도하거나 조건을 완화하지 않음 → LIVE_BLOCKED 유지

## 7. Command channel
- 스키마(0002): `command_id` UNIQUE, `status`, `requested_at/expires_at/acknowledged_at/executed_at/failure_reason`, `audit` JSON
- 실측: PAUSE→EXECUTED(1s)→RESUME→MAINTENANCE→RUNNING, 4회 모두 EXECUTED, 감사에 로그인/IP/UA
- TTL: PAUSE/RESUME 120s, KILL 600s. 테스트: 재시작 후 만료된 KILL은 EXPIRED로 미실행, 유효 KILL은 실행, 같은 `command_id` 재전송은 멱등, 동일 상태 명령 FAILED(`already PAUSED`), 미확인 명령 REJECTED
- 트레이더 재시작 → 상태 RUNNING 복원·명령 처리 정상

## 8. Health monitoring
- `/api/health`: trader(heartbeat<30s) · WS · REST(latency EMA ~37ms) · DB · Tailscale(`status --json`) · market data age(수 초) · reconnect/API errors/msgs · reconciliation(상태·시각·Δ)
- 대시보드 OFFLINE 판정 테스트(heartbeat 5분 stale → OFFLINE, uptime null)

## 9. LIVE readiness 상태
```
Infrastructure       FAIL      (API 키 없음 — Day 4 입력)
Paper Runtime        PROGRESS  1.5 / 48h
Paper Trades         PROGRESS  0 / 10
Critical Errors      PASS      0 (paper epoch 이후)
Reconciliation       PASS
Order Test           NOT_RUN
Backtest             FAIL      recent=yes quality=fail (PF 0.40)
Strategy Approved    FAIL      NO
LIVE  BLOCKED — STRATEGY_NOT_APPROVED
```

## 10. Security 처리
- 읽기/제어 라우터 분리. 제어: `DASHBOARD_CONTROL_TOKEN`(≥16자, `.env`, hmac 비교) + `X-Requested-With: quant-dashboard`(CSRF) + 확인문구 + 감사(login/ip/ua) + `command_id` 멱등 + TTL. 쿠키 미사용.
- 실측: 토큰 없음 401 · 잘못된 토큰 401 · CSRF 헤더 없음 403 · 확인문구 불일치 400 · 토큰 미설정 503.
- Tailnet 접속만으로는 제어 불가(토큰 필수). Tailnet ACL은 기본(모든 멤버 허용) — 단일 사용자 tailnet이라 현 시점 추가 제한 없음(§12 참조).

## 11. 품질 게이트
`ruff` ✅ · `mypy --strict` ✅ 97 files · `pytest` ✅ **149 passed** (13.5s) · `npm run build` ✅ (tsc + vite)

## 12. 발견된 문제
1. 무료 tailnet은 **HTTPS Certificates가 기본 비활성** → Serve가 인증서 단계에서 멈춤. 문서화(README).
2. Tailscale 앱 CLI가 비-GUI 셸에서 `serve`를 실행하면 GUI 프롬프트/`CLIError 3`으로 실패 → Serve 설정은 사용자 터미널에서 수행해야 함.
3. `.env`가 저장소 루트에 있어 `backend/`에서 기동한 프로세스가 로드하지 못했음(제어 토큰 503) → `env_file=(".env","../.env")`로 수정. 상시 트레이더는 `TRADING_MODE`를 env로 넘겨 영향 없었음.
4. Readiness의 Critical Errors가 어제 로깅 블로킹 사고의 KILL_SWITCH를 세어 FAIL → 현재 paper epoch(재시작 시각) 이후로 범위 한정(상세에 epoch 표시). 과거 이벤트는 Event Log에 그대로 남음.
5. Paper runtime 카운터는 트레이더 프로세스가 살아 RUNNING일 때만 누적 — 맥이 잠들면 멈춤(현재 caffeinate -i로 방지, 뚜껑 닫힘/전원 정책은 Day 4 launchd와 함께 확인 필요).
6. `/api/candles`는 요청마다 1m을 리샘플(15m×300 → 4,500행) — 폴링 30s로 충분하지만 이후 캐시 고려.
7. 프론트엔드는 lint(oxlint) 설정만 있고 테스트 없음(스코프 밖).

## 13. DAY 4 전 결정 사항
1. **DAY 3 커밋** 승인: `feat: day3 dashboard, tailscale serve, command lifecycle, live readiness` + tag `day3-dashboard`
2. **Upbit API 키 발급/입력 시점**: Day 4의 `/v1/orders/test`·`GET /v1/accounts`·reconciliation(거래소 진실)에는 키가 필요. 권한은 **자산조회 + 주문**만(출금 제외), IP 화이트리스트에 맥 공인 IP 등록. 키를 `.env`에 넣는 시점을 Day 4 시작으로 할지.
3. **LIVE 게이트웨이 구현 범위**: `POST /v1/orders`(price/market) + `GET /v1/order` + `DELETE /v1/order` + `GET /v1/accounts` + `orders/chance` 로드. 구현하되 `live_gate.enabled=false` 유지 — 실제 LIVE 활성화는 전략 승인 전까지 없음(확인).
4. **launchd**: trader/api 두 plist + `caffeinate` 대체(`launchd` KeepAlive + `pmset`?) — 맥북 뚜껑 닫힘 정책(`sudo pmset -a disablesleep 1` vs 외부 전원+`caffeinate -s`) 결정 필요.
5. **Daily report / 콘텐츠 API**: `/api/content/today`는 있음. 매일 00:00 KST 자동 `daily_stats` 집계 + `reports/YYYY-MM-DD.md` 생성을 Day 4 스코프에 포함할지.
6. **전략 연구 트랙 착수 시점**: 인프라 완료 후 A(HTF Trend+Pullback)/B(Donchian+ATR)/C(Volatility Breakout) 백테스트·WF를 Day 5로 잡을지.
7. Tailnet ACL 강화(태그 기반 `<machine>` 노드만 443 허용) 적용 여부 — 현재 단일 사용자라 보류 권장.
