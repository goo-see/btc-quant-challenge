# Upbit Open API 정책 정리 (조회일: 2026-09-24)

> 출처: docs.upbit.com 공식 문서. 구현 시점에 각 항목을 다시 확인한다.
> 특히 "미확인" 표시 항목은 코드 작성 직전 재조회 필수.

## 1. Authentication (REST)

- 방식: JWT (`Authorization: Bearer {jwt}`)
- 서명 알고리즘: **HS512 권장** (secret key는 raw 문자열 그대로 사용, base64 decode 없음)
- Payload
  | field | 필수 | 내용 |
  |---|---|---|
  | `access_key` | O | API Access Key |
  | `nonce` | O | 요청마다 새 UUID |
  | `query_hash` | 파라미터/바디 있을 때 | 쿼리스트링의 SHA512 hex |
  | `query_hash_alg` | 선택 | 기본 `SHA512` |
- `query_hash` 계산
  - GET/DELETE: 실제 전송하는 query string을 **정렬하지 않고 그대로** 해시
  - 배열 파라미터: `states[]=wait&states[]=watch` (키 반복)
  - POST: JSON body를 `k=v&k=v` 형태로 변환 후 해시 → 실제 전송 body와 순서/값이 일치해야 함
- 주의: 2026-07 "요청 헤더 중복 전송 방지" 공지 존재 → 헤더 중복 시 거부 가능. 구현 시 원문 확인.
  (https://docs.upbit.com/kr/changelog/prevent-duplicate-request-headers)
- API Key 권한: 자산조회 / 주문 등 개별 부여. **출금 권한은 절대 부여하지 않음.** IP 화이트리스트 등록 필요(Upbit 콘솔).
- Rate Limit 적용 단위가 계정 → **포켓(Pocket)** 으로 변경됨(2026-06). 메인 포켓 API Key 하나만 사용할 예정이므로 영향 적음.

## 2. 계좌 / 주문 가능 정보

- `GET /v1/accounts` → `currency, balance, locked, avg_buy_price, avg_buy_price_modified, unit_currency`
- `GET /v1/orders/chance?market=KRW-BTC`
  → `bid_fee, ask_fee, maker_bid_fee, maker_ask_fee, market.bid.min_total, market.ask.min_total, market.max_total, market.state, bid_account, ask_account`
  - 문서 예시 수수료 **0.0005 (0.05%)**. 하드코딩하지 않고 **기동 시 이 API로 실제 수수료/최소금액을 읽어 config를 덮어쓴다.**

## 3. 주문 생성 `POST /v1/orders` (201)

| ord_type | 의미 | 필수 | 금지 |
|---|---|---|---|
| `limit` | 지정가 | `volume`, `price` | |
| `price` | **시장가 매수** (KRW 총액) | `price` | `volume` |
| `market` | **시장가 매도** (수량) | `volume` | `price` |
| `best` | 최유리 | `volume`(매도) 또는 `price`(매수) + `time_in_force`(ioc/fok) | |

- 공통: `market`, `side`(`bid`=매수/`ask`=매도), `ord_type`
- 선택: `identifier`(**클라이언트 지정 ID, 최대 64자, 계정 내 유일 → 멱등성 키로 사용**), `time_in_force`(`ioc`,`fok`,`post_only`), `smp_type`
- 응답: `uuid, state, created_at, volume, remaining_volume, executed_volume, locked, reserved_fee, remaining_fee, paid_fee, trades_count, identifier ...`
- 주문 상태: `wait`(체결대기) / `watch`(예약대기) / `done`(전량체결) / `cancel`(취소)
  - 시장가 주문은 즉시 `done` 또는 잔량 `cancel`이 될 수 있음 → 부분체결은 `executed_volume`으로 판단.

## 4. 주문 테스트 `POST /v1/orders/test` (201)

- 실제 주문과 동일한 파라미터/검증, **주문은 생성되지 않음**
- 검증 항목: 형식, 마켓 상태(`market_offline`), 매수 잔고(`insufficient_funds_bid`), 최대금액(`over_krw_funds_bid`), 마켓 존재(`notfoundmarket`), `invalid_time_in_force`
- 응답 `uuid`는 조회/취소에 사용 불가
- Rate limit: **order-test 그룹 초당 8회**

## 5. 주문 조회 / 취소

- `GET /v1/order?uuid=... | identifier=...` (둘 다 있으면 uuid 우선). `identifier` 조회는 2024-10-18 이후 주문만.
  - 응답에 `trades[]` (`market, uuid, price, volume, funds, trend, created_at, side`) 포함 → **체결 단가/수수료 정산의 기준 데이터**
- `GET /v1/orders/open` — `market, state|states[] (wait/watch), page, limit(≤100), order_by`
- `GET /v1/orders/closed` — `market, state|states[] (done/cancel), start_time, end_time, limit(≤1000)`, **최대 7일 범위**
- `DELETE /v1/order?uuid=...|identifier=...` → 200 + Order. 없으면 404 `order_not_found`
- `GET /v1/orders/uuids`, `DELETE /v1/orders/uuids` (다건) — V1에서는 미사용

## 6. Rate Limit (2026-08 상향 반영)

| 구분 | 그룹 | 한도 | 측정 단위 |
|---|---|---|---|
| Quotation | `market`, `candle`, `trade`, `ticker`, `orderbook` | **각 10회/초** | IP |
| Exchange | `default` (계좌, 주문조회/취소 등) | **30회/초** | 포켓 |
| Exchange | `order` (주문 생성) | **12회/초** | 포켓 |
| Exchange | `order-test` | **8회/초** | 포켓 |
| Exchange | `order-cancel-all` | 1회/2초 | 포켓 |
| WebSocket | `websocket-connect` | 5회/초 | IP / 포켓 |
| WebSocket | `websocket-message` | 5회/초, 100회/분 | 연결 |

- 응답 헤더 `Remaining-Req: group=default; min=1800; sec=29` → `sec`가 현재 잔여 횟수
- 초과: **429**. 반복 위반: **418 (I'm a teapot)** 일시 차단(차단 기간 정보 포함)
- 구현 원칙: 클라이언트 측 그룹별 token bucket을 **공식 한도의 50~60%** 로 설정 + `Remaining-Req` 헤더 파싱해 동적 감속 + 429/418 시 backoff. 418 발생 시 Kill Switch 후보.

## 7. WebSocket

- Public: `wss://api.upbit.com/websocket/v1` (ticker, trade, orderbook, candle, indicator …)
- Private: `wss://api.upbit.com/websocket/v1/private` (myOrder, myAsset) — JWT를 `Authorization: Bearer` 헤더로. V1은 Public만, myOrder는 V2 후보.
- 요청 포맷: `[{"ticket":"<uuid>"}, {"type":"ticker","codes":["KRW-BTC"]}, {"type":"candle.1m","codes":["KRW-BTC"]}, {"format":"DEFAULT"}]`
  - `codes`는 **대문자**. 옵션 `is_only_snapshot`, `is_only_realtime`
- 캔들 타입: `candle.1s, 1m, 3m, 5m, 10m, 15m, 30m, 60m, 240m` — 체결이 있는 초에만 1초 주기로 전송, 같은 timestamp가 여러 번 올 수 있음(마지막 값 채택), 첫 응답은 최근 완성 캔들 snapshot
- ticker 주요 필드: `code, trade_price, trade_timestamp, timestamp, acc_trade_volume_24h, change, stream_type`
- trade 주요 필드: `trade_price, trade_volume, ask_bid, sequential_id, trade_timestamp, timestamp, best_ask_price, best_bid_price, stream_type`
- **120초 무송수신 시 서버가 연결 종료** → PING 프레임 또는 `"PING"` 텍스트를 주기적으로 전송(정상 시 서버가 10초마다 `UP` 응답)
- 재접속: exponential backoff (1s→2s→…→60s cap, jitter), 초당 5회 연결 한도 준수

## 8. REST 캔들 (Historical)

- `GET /v1/candles/minutes/{unit}` unit ∈ {1,3,5,10,15,30,60,240}, `market`, `to`(ISO8601, 예 `2025-06-24T04:56:53Z`), `count`(기본 1, **최대 200**)
- 응답: `market, candle_date_time_utc, candle_date_time_kst, opening_price, high_price, low_price, trade_price, timestamp, candle_acc_trade_price, candle_acc_trade_volume, unit`
- **체결 없는 구간은 캔들이 생성되지 않음** → 저장 시 gap 허용, 지표 계산 시 forward-fill 여부를 명시적으로 결정
- 페이지네이션: `to`를 가장 오래된 캔들 시각으로 옮기며 역방향 반복. 1년치 1m ≈ 2,628 요청 → 8 req/s로 약 6분

## 9. 원화 마켓 호가 단위(Tick Size) / 최소 주문

| 가격대 (KRW) | 호가 단위 |
|---|---|
| ≥ 2,000,000 | 1,000 |
| 1,000,000 ~ 1,999,999 | 1,000 |
| 500,000 ~ 999,999 | 500 |
| 100,000 ~ 499,999 | 100 |
| 50,000 ~ 99,999 | 50 |
| 10,000 ~ 49,999 | 10 |
| 5,000 ~ 9,999 | 5 |
| 1,000 ~ 4,999 | 1 |
| 100 ~ 999 | 1 |
| 10 ~ 99 | 0.1 |
| 1 ~ 9 | 0.01 |
| (이하 생략, BTC 무관) | |

- BTC(현재 1억원대)는 **1,000원 단위**. 지정가 사용 시 반드시 라운딩.
- **최소 주문 금액 5,000 KRW** (매수/매도 동일, `orders/chance`의 `min_total`로 재확인)
- 최소 수량: 없음(금액 기준). 매도 시 `volume × price ≥ 5,000` 확인.

## 10. 구현 전 재확인 목록 (미확인/변동 가능)

- [ ] `identifier` 중복 전송 시 정확한 에러 코드 (멱등성 처리에 필요)
- [ ] "요청 헤더 중복 전송 방지" 공지 원문
- [ ] `orders/test`가 `identifier` 중복도 검증하는지
- [ ] 시장가 매수(`price`) 시 수수료가 `price`에 포함되는지 / 별도 차감인지 (`reserved_fee` 동작)
- [ ] 실제 계정 수수료율 (`orders/chance` 기동 시 조회)
- [ ] 418 차단 기간 헤더/바디 포맷

## 참고 링크

- 인증: https://docs.upbit.com/kr/reference/auth
- 주문: https://docs.upbit.com/kr/reference/주문하기
- 주문 테스트: https://docs.upbit.com/kr/reference/order-test
- 개별 주문 조회: https://docs.upbit.com/kr/reference/get-order
- 주문 가능 정보: https://docs.upbit.com/kr/reference/주문-가능-정보
- Rate Limit: https://docs.upbit.com/kr/reference/rate-limits
- WebSocket 가이드: https://docs.upbit.com/kr/reference/websocket-guide
- WebSocket 캔들: https://docs.upbit.com/kr/reference/websocket-candle
- 분 캔들: https://docs.upbit.com/kr/reference/list-candles-minutes
- 호가 단위/최소주문: https://docs.upbit.com/kr/docs/krw-market-info
- 변경 이력: https://docs.upbit.com/kr/changelog
