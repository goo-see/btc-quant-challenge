/** node --test src/view.test.ts  (Node 24 strips types natively) */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { challengeView, decisionLabel, exitKind, exitLabel, nowState, splitSignals, todaySummary, tradeCards } from './view.ts';
import type { Decision, Execution, Portfolio, Signal, Status, Trade } from './api/client.ts';

const NOW = new Date('2026-09-30T05:00:00Z').getTime(); // 14:00 KST
const pfFlat: Portfolio = { initial_equity: 100039, equity: 100039, krw: 100039, btc: 0, btc_value: 0, btc_price: 112905000, avg_entry: null, entry_time: null, stop_price: null, realized_pnl: 0, unrealized_pnl: 0, total_pnl: 0, total_return_pct: 0, today_pnl: 0, today_pnl_pct: 0, hwm: 100039, drawdown_pct: 0, max_drawdown_pct: 0, fees_total: 0, consecutive_losses: 0, has_position: false, entry_reason: '' };
const pfHold: Portfolio = { ...pfFlat, krw: 70040, btc: 0.00026417, btc_value: 30167, btc_price: 114200000, avg_entry: 113500000, entry_time: '2026-09-29T20:01:00Z', stop_price: 112800000, unrealized_pnl: 168, has_position: true, equity: 100207 };
const dec: Decision = { decision: 'HOLD', reason: 'INSIDE_CHANNEL', market_bar_time: '2026-09-29T12:00:00Z', next_evaluation: '2026-09-29T16:00:00Z', donchian_upper: 115920000, trend_ok: true, current_price: 112905000, timeframe: '4h' };
const execBuy: Execution = { id: 1, mode: 'LIVE', strategy_version: 'v1', client_order_id: 'c1', side: 'BUY', signal_time: '2026-09-29T20:00:00Z', signal_price: 113400000, order_sent_at: null, reference_price_at_send: null, actual_fill_price: 113500000, slippage_bps: 8.8, slippage_vs_reference_bps: null, fee: 15, quantity: 0.00026417, funds: 29984, fill_count: 1, atr: 900000, initial_stop: 110700000, stop_distance_from_signal: null, stop_distance_from_fill: null, exit_reason: null, exit_trigger_price: null, realized_pnl: null, return_pct: null, holding_sec: null, filled_at: '2026-09-29T20:01:00Z' };
const win: Trade = { id: 10, entry_time: '2026-09-29T20:01:00Z', exit_time: '2026-09-30T04:42:00Z', entry_price: 113500000, exit_price: 115160000, quantity: 0.00026417, amount: 29983, fees: 30, net_pnl: 428, return_pct: 1.43, holding_sec: 31260, strategy: 's', entry_reason: 'BREAKOUT>1 TREND_UP', exit_reason: 'STOP@114900000 (price 114880000)' };
const loss: Trade = { ...win, id: 11, exit_price: 112700000, net_pnl: -205, return_pct: -0.68, exit_reason: 'STOP@112800000 (price 112750000)' };

test('exit reasons map to user labels (initial vs trailing by stop vs entry)', () => {
  assert.equal(exitLabel('STOP@112800000 (price 112750000)', 113500000), '손절 기준 도달');
  assert.equal(exitLabel('STOP@114900000 (price 114880000)', 113500000), '수익 보호선 도달');
  assert.equal(exitLabel('CHANNEL_EXIT<112019000'), '최근 가격 범위 이탈');
  assert.equal(exitLabel('TREND_LOSS_EXIT'), '상승 추세 종료');
  assert.equal(exitKind('MANUAL_KILL'), 'MANUAL');
  assert.equal(exitLabel('RECONCILE_FAILED'), '시스템 보호 종료');
});

test('decision labels hide raw jargon', () => {
  assert.deepEqual(decisionLabel('HOLD', 'INSIDE_CHANNEL'), { title: '매수 안 함', detail: '최근 5일 고점 미돌파' });
  assert.deepEqual(decisionLabel('HOLD', 'TREND_DOWN'), { title: '매수 안 함', detail: '상승 추세 조건 미충족' });
  assert.equal(decisionLabel('BUY', 'BREAKOUT>115920000 TREND_UP').title, '매수');
  assert.equal(decisionLabel('SELL', 'CHANNEL_EXIT<112019000').detail, '최근 가격 범위 이탈');
  assert.equal(decisionLabel('HOLD', 'IN_POSITION_TRAIL').title, '보유 유지');
});

test('no position -> waiting state with gap to threshold and next evaluation', () => {
  const s = nowState(pfFlat, dec, [], [], 6 * 3600, NOW);
  assert.equal(s.kind, 'WAITING');
  if (s.kind === 'WAITING') {
    assert.equal(s.threshold, 115920000);
    assert.equal(s.gapKrw, 3015000);
    assert.ok(Math.abs((s.gapPct ?? 0) - 2.6704) < 0.001);
    assert.equal(s.trendOk, true);
    assert.equal(s.nextEval, '2026-09-29T16:00:00Z');
  }
});

test('open position -> holding state with pnl %, held time and stop distance', () => {
  const s = nowState(pfHold, dec, [], [execBuy], 6 * 3600, NOW);
  assert.equal(s.kind, 'HOLDING');
  if (s.kind === 'HOLDING') {
    assert.ok(Math.abs(s.amount - 29983.3) < 1);
    assert.equal(s.initialStop, 110700000);
    assert.equal(s.stop, 112800000);
    assert.ok(Math.abs((s.stopDistPct ?? 0) - -1.2259) < 0.001);
    assert.equal(s.heldSec, 9 * 3600 - 60);
    assert.ok((s.pnlPct ?? 0) > 0.5);
  }
});

test('closed trade within 6h -> recently closed card (profit and loss rendering)', () => {
  const s = nowState(pfFlat, dec, [win], [], 6 * 3600, NOW);
  assert.equal(s.kind, 'CLOSED_RECENTLY');
  if (s.kind === 'CLOSED_RECENTLY') {
    assert.equal(s.trade.pnl, 428);
    assert.equal(s.trade.exitLabel, '수익 보호선 도달');
    assert.ok(Math.abs((s.trade.recovered ?? 0) - 30421.8) < 1);
    assert.equal(s.trade.heldSec, 31260);
  }
  const later = nowState(pfFlat, dec, [win], [], 6 * 3600, NOW + 7 * 3600 * 1000);
  assert.equal(later.kind, 'WAITING'); // old trade no longer dominates the status card
  const l = tradeCards([loss], null, null, NOW)[0];
  assert.equal(l.pnl, -205);
  assert.equal(l.exitLabel, '손절 기준 도달');
});

test('trade cards: open position first as 진행 중, closed after; first-trade-before-challenge flag', () => {
  const cards = tradeCards([win], pfHold, '2026-09-30T00:00:00Z', NOW);
  assert.equal(cards.length, 2);
  assert.equal(cards[0].open, true);
  assert.equal(cards[0].beforeChallenge, true); // entry 09-29 20:01Z < challenge start
  assert.equal(cards[1].open, false);
  assert.equal(cards[1].exitKind, 'TRAILING_STOP');
});

test('today summary counts KST-day executions and realized pnl', () => {
  const sell: Execution = { ...execBuy, id: 2, side: 'SELL', filled_at: '2026-09-30T04:42:00Z', fee: 15, realized_pnl: 428 };
  const t = todaySummary([execBuy, sell], [win], pfFlat, NOW);
  assert.deepEqual(t, { buys: 1, sells: 1, realized: 428, fees: 30, holding: false }); // BUY 09-30 05:01 KST, SELL 13:42 KST
});

test('challenge view uses live_start_krw and flags first-trade-pending', () => {
  const st = { challenge: { initial: 100039, goal: 1000000, progress_pct: 0, raw_pct: 0, status: 'LIVE_ACTIVATED_WAITING_FIRST_BUY', live_start_krw: 100039, live_activated_at: '2026-09-29T15:09:53Z', challenge_started_at: null } } as unknown as Status;
  const c = challengeView(st, pfFlat);
  assert.equal(c.start, 100039);
  assert.equal(c.pnl, 0);
  assert.equal(c.remaining, 899961);
  assert.equal(c.firstTradePending, true);
});

test('signals split into LIVE and earlier PAPER records', () => {
  const sigs: Signal[] = [
    { id: 3, ts: '2026-09-29T16:00:00Z', mode: 'LIVE', action: 'HOLD', strength: 0, reason: 'INSIDE_CHANNEL', timeframe: '4h', indicators: {}, stop_price: null, risk: null },
    { id: 2, ts: '2026-09-29T12:00:00Z', mode: 'PAPER', action: 'HOLD', strength: 0, reason: 'INSIDE_CHANNEL', timeframe: '4h', indicators: {}, stop_price: null, risk: null },
    { id: 1, ts: '2026-09-29T08:00:00Z', action: 'HOLD', strength: 0, reason: 'INSIDE_CHANNEL', timeframe: '4h', indicators: {}, stop_price: null, risk: null }, // no mode field -> by time
  ];
  const r = splitSignals(sigs, '2026-09-29T15:09:53Z', 'LIVE');
  assert.deepEqual(r.live.map((x) => x.id), [3]);
  assert.deepEqual(r.paper.map((x) => x.id), [2, 1]);
  assert.equal(r.live[0].title, '매수 안 함');
});
