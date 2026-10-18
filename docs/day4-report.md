# DAY 4 보고 — 2026-09-25

## 1. DAY 3 commit / tag
- `625585c feat: day3 dashboard, tailscale serve, command lifecycle, live readiness` · tag **`day3-dashboard`** (57 files, data/logs/db/.env/dist 미포함 확인)
- DAY 4 변경분 31개 경로는 **미커밋** — 승인 대기

## 2. Public IP / Upbit API 키 준비 상태
- `scripts/check_public_ip.py` 실행: 3개 소스(ipify / amazonaws / ifconfig.me) 모두 **203.0.113.10** (IPv4, 공인). 로컬 인터페이스 10.0.0.5(LAN)·100.64.0.5(Tailscale)은 거부 규칙에 걸림.
- 판정: `STATIC_PUBLIC_IP_NOT_VERIFIED (not_configured)` — `live_gate.static_public_ip` 미기입 상태(사용자 기입 항목). 트레이더가 1시간마다 관측을 누적 중(현재 3건, span 0h). 기입 후 **24h 이상 동일**해야 PASS.
- API 키: `.env`에 없음 → preflight `BLOCKED reason=CREDENTIALS_MISSING`. 사용자가 직접 발급(권한 자산조회+주문하기, 출금 제외, 허용 IP = 203.0.113.10)해 `.env`에 입력하도록 README에 절차 명시.
- 키가 없어도 시스템은 정상 동작하며 해당 게이트만 FAIL(readiness: Infrastructure FAIL / Static Public IP FAIL / Order Test FAIL(CREDENTIALS_MISSING)).

## 3. Live Gateway 구현
- `UpbitRestClient`: `test_order`(`/v1/orders/test`, order-test 그룹) · `create_order`(`/v1/orders`, 재시도 없음) · `cancel_order`(`DELETE /v1/order`) 추가. 바디 `market/side/ord_type/price|volume/identifier`.
- `app/exchange/upbit/live_gateway.py`: `ExchangeGateway` 구현 — 시장가 매수(`price`)/매도(`market`), identifier=client_order_id(멱등), 상태 매핑(wait/watch→OPEN, done→FILLED, cancel→CANCELLED), 주문 수수료 체결별 비례 배분, 잔고/미체결/reference price/ping. 오류 매핑: 네트워크·429→UNKNOWN 경로, 4xx→REJECTED, identifier 중복→Duplicate, 404→NotFound. **기존 OrderManager / RiskManager / Reconciler / RecoveryChecker 그대로 재사용.**
- `main_trader.build_live_service`: 정적 게이트 → readiness(증거 게이트) → preflight 순으로 전부 통과해야 LiveGateway 생성, 실패 시 `LIVE_BLOCKED reason=…`로 종료. `live_gate.enabled=false`, `approved_strategies=[]` 유지 → 현재 LIVE 기동 불가.
- 테스트 8건(respx): 요청 바디/identifier, 거부·네트워크·중복·404 매핑, 잔고/미체결, `orders/test`가 `/v1/orders`를 호출하지 않음.

## 4. orders/test 결과
- `scripts/private_smoke.py` → `FAIL credentials … / result: BLOCKED reason=CREDENTIALS_MISSING` (우회 없이 기록, `bot_state.order_test`)
- 키·고정IP 충족 시 경로(accounts → orders/chance → orders/test dry-run, 실주문 엔드포인트 미호출)는 mock 테스트로 검증(`test_preflight_runs_accounts_chance_and_orders_test_only`, auth 실패 기록 테스트 포함).

## 5. launchd 설치/실행
- `~/Library/LaunchAgents/com.quant.trader.plist`, `com.quant.api.plist` (`scripts/launchd_install.sh install`), `plutil -lint` OK, 테스트로 plist 구조 검증.
- 상태: `com.quant.trader state=running pid=58649`, `com.quant.api state=running pid=58781`. nohup 데몬은 중지(개발용으로 스크립트 유지).
- 재부팅 대응: gui 도메인 `RunAtLoad` → 로그인 시 자동 기동, `KeepAlive`로 종료 시 재기동. (FileVault 자동 로그인이 꺼져 있으면 로그인 전까지 정지 — 문서화)

## 6. Process restart
- 트레이더 `kill -9 58557` → 8초 내 launchd가 pid 58649로 재기동, DB에서 RUNNING 복원(`RECOVERY_CHECKS_PASSED`), 새 run_id, reconcile OK, `caffeinate` assertion이 새 pid에 다시 붙음.
- API `kill -9 58562` → 재기동(pid 58781), `/api/health` 200. 두 서비스는 서로 영향 없음.

## 7. Sleep / caffeinate 운영 상태
- `pmset`: AC Power, 배터리 100%, `sleep 1 (sleep prevented by caffeinate)`, `displaysleep 10`, `PreventUserIdleSystemSleep 1` — caffeinate가 launchd 트레이더의 부모 프로세스(`-i`).
- `/api/health.power` = `{ac_power: true, sleep_prevented: true, warnings: []}`; 배터리 구동·assertion 부재 시 대시보드 상단 경고 + System 카드. 영구 `pmset` 변경 없음. 뚜껑 닫힘 24/7 미지원(문서화).

## 8. Paper 누적
- Runtime **2.0h / 48h**(RUNNING 누적, epoch 05:09 KST 14:09), Trades **0 / 10**, 시그널 10(HOLD), reconcile 43회 OK, epoch 이후 CRITICAL 0. 거래 유도/조건 완화 없음.

## 9. Daily Report 샘플 (`reports/2026-09-25.md`, `daily_stats` 1행)
```
# 2026-09-25 — DAY — (PAPER)
| Starting equity | ₩100,000 | Ending equity | ₩100,000 | Daily PnL | ₩0 (+0.00%) |
| Total return | +0.00% | High water mark | ₩100,000 | Max drawdown | 0.00% |
| Trades | 0 (W 0 / L 0, win rate 0.0%) | Fees | ₩0 | Best / Worst | — / — |
| Current position | none | Bot state | RUNNING (RECOVERY_CHECKS_PASSED) | Trading mode | PAPER |
| Reconciliation | OK | Critical errors | 0 |
```
- Day number는 `challenge.start_date`(null, LIVE 시작일에 설정) 기준이라 "—". KST 자정에 트레이더가 전날분 자동 생성(`DAILY_REPORT` 이벤트), `/api/reports`, `/api/reports/{date}`.

## 10. LIVE readiness
```
Infrastructure       FAIL      no API keys
Static Public IP     FAIL      203.0.113.10 (not_configured)
Paper Runtime        PROGRESS  2.0 / 48h
Paper Trades         PROGRESS  0 / 10
Critical Errors      PASS      0
Reconciliation       PASS
Order Test           FAIL      CREDENTIALS_MISSING
Backtest             FAIL      recent=yes quality=fail
Strategy Approved    FAIL      NO
LIVE  BLOCKED — STRATEGY_NOT_APPROVED
```
- 차단 우선순위: STRATEGY_NOT_APPROVED → STATIC_PUBLIC_IP_NOT_VERIFIED → 기타 게이트. Infrastructure Ready와 Strategy Ready가 **모두** PASS해야 `live_allowed`.

## 11. Security 확인
- `.env`, `reports/`, `data/`, `logs/`, `frontend/dist` gitignore 확인. 소스/문서에 키 패턴 없음, 로그·DB에 제어 토큰 없음. `get_secret_value` 사용처 4곳(설정·제어 인증·preflight·LIVE 배선)뿐, preflight 기록에 시크릿 미포함(테스트).
- Tailnet only · Funnel off · loopback binding · 제어 토큰+CSRF+확인+감사+멱등+TTL 유지. Tailscale CLI가 launchd 환경에서 동작하지 않아 인터페이스 기반 상태 판정으로 폴백.

## 12. 품질 게이트
`ruff` ✅ · `mypy --strict` ✅ 107 files · `pytest` ✅ **168 passed** (18.8s) · `npm run build` ✅

## 13. 발견된 문제
1. **Tailscale 앱 CLI가 launchd(비-GUI) 컨텍스트에서 실패** → `tailscale status` 대신 utun 100.64/10 주소 검사로 폴백(DNS 이름은 표시 불가). Serve/hostname 조작은 사용자 터미널에서.
2. launchd 재기동 직후 60초간 `health_snapshots` 행이 없어 WS가 DISCONNECTED로 보임(transient). 트레이더 기동 시 즉시 1회 스냅샷 기록으로 개선 가능.
3. `challenge.start_date`가 null이라 Day number "—". LIVE 시작 시 설정 필요(또는 PAPER epoch 기준 옵션).
4. 재부팅 후 자동 복구는 **사용자 로그인**을 전제(gui LaunchAgent). 무인 재부팅 복구는 자동 로그인 설정 또는 LaunchDaemon(루트) 전환이 필요 — 보안상 현행 유지 권장.
5. 공인 IP 관측이 재시작마다 즉시 1회 기록되어 span 계산은 정확하나, 관측 빈도가 재시작에 따라 불규칙할 수 있음(판정에는 영향 없음).
6. Upbit `identifier` 중복 시 정확한 에러 이름은 미확인(문서 §10 항목) — 게이트웨이는 이름에 `identifier`/`duplicate`가 포함되면 Duplicate로 처리. 첫 Small LIVE 전 실제 응답 확인 필요.

## 14. DAY 5 전략 연구 전 결정 사항
1. **DAY 4 커밋** 승인: `feat: day4 launchd ops, upbit private gateway, static ip gate, daily report` + tag `day4-ops`
2. `live_gate.static_public_ip: "203.0.113.10"` 기입 시점 — 지금 기입하면 24h 관측이 시작됨(키 없이도 가능). 기입 승인 여부.
3. Upbit API 키 발급 시점(발급 후 사용자가 `.env`에 입력 → `private_smoke.py`로 accounts/chance/orders-test 검증). Day 5 연구와 병행 가능.
4. 전략 연구 트랙 설계: 후보 A(HTF Trend + Pullback) / B(Donchian Breakout + ATR stop) / C(Volatility Breakout)를 같은 백테스터·WF(15m base, 180/60d)로 비교, 승인 기준 제안 — OOS 누적 수익 > 0, OOS 수익 윈도우 ≥ 60%, PF ≥ 1.2, MDD ≤ 10%, 거래 ≥ 30/2y. 기준 승인 필요.
5. 연구 중 PAPER 인스턴스는 baseline으로 계속 운영(runtime 누적) vs 후보 전략 확정 후 PAPER 전략 교체(교체 시 paper epoch 리셋 여부).
6. `challenge.start_date` 설정 정책(LIVE 첫 주문일 자동 기록 권장).
