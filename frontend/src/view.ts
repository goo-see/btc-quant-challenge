/** Pure display derivations for the user-facing Trading tab.
 *  Everything here is presentation only: the trading engine's values (decision snapshot,
 *  portfolio, trades, executions) are the source of truth; nothing re-implements strategy logic. */
import type { Decision, Execution, Portfolio, Signal, Status, Trade } from './api/client';

export type ExitKind = 'INITIAL_STOP' | 'TRAILING_STOP' | 'CHANNEL_EXIT' | 'TREND_LOSS' | 'MANUAL' | 'SYSTEM' | 'OTHER';

/** Map a raw exit reason to a user-facing kind. Stop exits: a stop at/above the entry price is
 *  the profit-protection (trailing) line, below it the initial stop-loss line. */
export function exitKind(reason: string | null | undefined, entryPrice?: number | null, exitStop?: number | null): ExitKind {
  const r = (reason ?? '').toUpperCase();
  if (!r) return 'OTHER';
  if (r.startsWith('STOP')) {
    const m = /STOP@(\d+)/.exec(r);
    const stop = exitStop ?? (m ? Number(m[1]) : null);
    if (stop != null && entryPrice != null) return stop >= entryPrice ? 'TRAILING_STOP' : 'INITIAL_STOP';
    return 'INITIAL_STOP';
  }
  if (r.startsWith('CHANNEL_EXIT')) return 'CHANNEL_EXIT';
  if (r.startsWith('TREND_LOSS')) return 'TREND_LOSS';
  if (r.includes('MANUAL')) return 'MANUAL';
  if (r.includes('RECONCIL') || r.includes('EMERGENCY') || r.includes('KILL') || r.includes('HALT')) return 'SYSTEM';
  return 'OTHER';
}

export const EXIT_LABEL: Record<ExitKind, string> = {
  INITIAL_STOP: '손절 기준 도달',
  TRAILING_STOP: '수익 보호선 도달',
  CHANNEL_EXIT: '최근 가격 범위 이탈',
  TREND_LOSS: '상승 추세 종료',
  MANUAL: '수동 종료',
  SYSTEM: '시스템 보호 종료',
  OTHER: '기타',
};

export function exitLabel(reason: string | null | undefined, entryPrice?: number | null, exitStop?: number | null): string {
  return EXIT_LABEL[exitKind(reason, entryPrice, exitStop)];
}

/** User-facing label for a strategy decision (HOLD/BUY/SELL + raw reason). */
export function decisionLabel(action: string | null | undefined, reason: string | null | undefined, entryPrice?: number | null): { title: string; detail: string } {
  const r = reason ?? '';
  if (action === 'BUY') return { title: '매수', detail: r.startsWith('BREAKOUT') ? '최근 5일 고점 돌파 · 상승 추세' : r };
  if (action === 'SELL') return { title: '매도', detail: exitLabel(r, entryPrice) };
  if (r.startsWith('INSIDE_CHANNEL')) return { title: '매수 안 함', detail: '최근 5일 고점 미돌파' };
  if (r.startsWith('TREND_DOWN')) return { title: '매수 안 함', detail: '상승 추세 조건 미충족' };
  if (r.startsWith('WARMUP')) return { title: '판단 보류', detail: '데이터 준비 중' };
  if (r.startsWith('IN_POSITION')) return { title: '보유 유지', detail: '매도 조건 미도달' };
  return { title: action === 'HOLD' ? '매수 안 함' : (action ?? '—'), detail: r };
}

export type NowState =
  | { kind: 'HOLDING'; entryTime: string | null; amount: number; qty: number; avg: number | null; price: number | null; value: number; pnl: number; pnlPct: number | null; heldSec: number | null; initialStop: number | null; stop: number | null; stopDistPct: number | null }
  | { kind: 'WAITING'; price: number | null; threshold: number | null; gapKrw: number | null; gapPct: number | null; trendOk: boolean | null; nextEval: string | null; evaluatedBar: string | null }
  | { kind: 'CLOSED_RECENTLY'; trade: TradeCard };

export type TradeCard = {
  id: number; open: boolean; entryTime: string; exitTime: string | null; entryPrice: number; exitPrice: number | null;
  qty: number; invested: number; recovered: number | null; pnl: number | null; pnlPct: number | null; fees: number;
  heldSec: number | null; exitKind: ExitKind | null; exitLabel: string | null; rawExit: string | null; beforeChallenge: boolean;
};

const secBetween = (a: string | null | undefined, b: number | string | null | undefined) => {
  if (!a || b == null) return null;
  const t1 = new Date(a).getTime();
  const t2 = typeof b === 'number' ? b : new Date(b).getTime();
  return Number.isFinite(t1) && Number.isFinite(t2) ? Math.max(0, (t2 - t1) / 1000) : null;
};

export function tradeCards(trades: Trade[], pf: Portfolio | null, challengeStartedAt: string | null | undefined, now = Date.now()): TradeCard[] {
  const closed: TradeCard[] = trades
    .filter((t) => t.exit_time)
    .map((t) => {
      const invested = t.entry_price * t.quantity;
      const recovered = t.exit_price != null ? t.exit_price * t.quantity : null;
      return {
        id: t.id, open: false, entryTime: t.entry_time, exitTime: t.exit_time, entryPrice: t.entry_price, exitPrice: t.exit_price, qty: t.quantity,
        invested, recovered, pnl: t.net_pnl, pnlPct: t.return_pct, fees: t.fees, heldSec: t.holding_sec ?? secBetween(t.entry_time, t.exit_time),
        exitKind: exitKind(t.exit_reason, t.entry_price), exitLabel: exitLabel(t.exit_reason, t.entry_price), rawExit: t.exit_reason,
        beforeChallenge: !!challengeStartedAt && new Date(t.entry_time).getTime() < new Date(challengeStartedAt).getTime(),
      };
    });
  const cards = [...closed].sort((a, b) => new Date(b.entryTime).getTime() - new Date(a.entryTime).getTime());
  if (pf?.has_position && pf.entry_time && pf.avg_entry != null) {
    const invested = pf.avg_entry * pf.btc;
    cards.unshift({
      id: -1, open: true, entryTime: pf.entry_time, exitTime: null, entryPrice: pf.avg_entry, exitPrice: null, qty: pf.btc, invested,
      recovered: pf.btc_value, pnl: pf.unrealized_pnl, pnlPct: invested > 0 ? (pf.unrealized_pnl / invested) * 100 : null, fees: 0,
      heldSec: secBetween(pf.entry_time, now), exitKind: null, exitLabel: null, rawExit: null,
      beforeChallenge: !!challengeStartedAt && new Date(pf.entry_time).getTime() < new Date(challengeStartedAt).getTime(),
    });
  }
  return cards;
}

/** Latest BUY execution's initial stop (engine-recorded); null when unknown. */
export function latestInitialStop(execs: Execution[]): number | null {
  const buy = execs.find((e) => e.side === 'BUY');
  return buy?.initial_stop ?? null;
}

export function nowState(pf: Portfolio | null, d: Decision | null, trades: Trade[], execs: Execution[], recentWindowSec = 6 * 3600, now = Date.now()): NowState {
  if (pf?.has_position && pf.avg_entry != null) {
    const invested = pf.avg_entry * pf.btc;
    const price = pf.btc_price ?? (d?.current_price ?? null);
    const stop = pf.stop_price ?? null;
    return {
      kind: 'HOLDING', entryTime: pf.entry_time, amount: invested, qty: pf.btc, avg: pf.avg_entry, price, value: pf.btc_value,
      pnl: pf.unrealized_pnl, pnlPct: invested > 0 ? (pf.unrealized_pnl / invested) * 100 : null, heldSec: secBetween(pf.entry_time, now),
      initialStop: latestInitialStop(execs), stop, stopDistPct: stop != null && price ? ((stop - price) / price) * 100 : null,
    };
  }
  const last = trades.find((t) => t.exit_time);
  if (last?.exit_time && secBetween(last.exit_time, now) !== null && (secBetween(last.exit_time, now) as number) <= recentWindowSec) {
    return { kind: 'CLOSED_RECENTLY', trade: tradeCards([last], null, null, now)[0] };
  }
  const price = pf?.btc_price ?? d?.current_price ?? null;
  const threshold = d?.donchian_upper ?? null;
  const gapKrw = price != null && threshold != null ? threshold - price : null;
  const gapPct = price != null && threshold != null && price > 0 ? ((threshold - price) / price) * 100 : null;
  return { kind: 'WAITING', price, threshold, gapKrw, gapPct, trendOk: d?.trend_ok ?? null, nextEval: d?.next_evaluation ?? null, evaluatedBar: d?.market_bar_time ?? null };
}

export type TodaySummary = { buys: number; sells: number; realized: number; fees: number; holding: boolean };

const kstDay = (iso: string) => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(iso));

export function todaySummary(execs: Execution[], trades: Trade[], pf: Portfolio | null, now = Date.now()): TodaySummary {
  const today = kstDay(new Date(now).toISOString());
  const todayExecs = execs.filter((e) => kstDay(e.filled_at) === today);
  const todayTrades = trades.filter((t) => t.exit_time && kstDay(t.exit_time) === today);
  return {
    buys: todayExecs.filter((e) => e.side === 'BUY').length,
    sells: todayExecs.filter((e) => e.side === 'SELL').length,
    realized: todayTrades.reduce((a, t) => a + (t.net_pnl ?? 0), 0),
    fees: todayExecs.reduce((a, e) => a + (e.fee ?? 0), 0),
    holding: !!pf?.has_position,
  };
}

export type ChallengeView = { start: number | null; equity: number | null; pnl: number | null; goal: number; remaining: number | null; pct: number | null; started: boolean; startedAt: string | null; activatedAt: string | null; firstTradePending: boolean };

export function challengeView(status: Status | null, pf: Portfolio | null): ChallengeView {
  const c = status?.challenge;
  const start = c?.live_start_krw ?? pf?.initial_equity ?? null;
  const equity = pf?.equity ?? null;
  const goal = c?.goal ?? 1_000_000;
  const pnl = start != null && equity != null ? equity - start : null;
  const remaining = equity != null ? Math.max(0, goal - equity) : null;
  const pct = start != null && equity != null && goal > start ? Math.max(0, Math.min(100, ((equity - start) / (goal - start)) * 100)) : null;
  return {
    start, equity, pnl, goal, remaining, pct, started: c?.status === 'STARTED', startedAt: c?.challenge_started_at ?? null, activatedAt: c?.live_activated_at ?? null,
    firstTradePending: c?.status !== 'STARTED',
  };
}

export type SignalRow = { id: number; ts: string; mode: string; title: string; detail: string; raw: Signal; live: boolean };

/** Split signals into LIVE (after activation) and earlier PAPER records; PAPER rows never look like current decisions. */
export function splitSignals(signals: Signal[], liveActivatedAt: string | null | undefined, currentMode: string | null | undefined, entryPrice?: number | null): { live: SignalRow[]; paper: SignalRow[] } {
  const act = liveActivatedAt ? new Date(liveActivatedAt).getTime() : null;
  const rows = signals.map((s) => {
    const mode = s.mode ?? (act != null && new Date(s.ts).getTime() >= act ? 'LIVE' : 'PAPER');
    const live = mode === 'LIVE' || (currentMode !== 'LIVE' && mode === currentMode);
    const l = decisionLabel(s.action, s.reason, entryPrice);
    return { id: s.id, ts: s.ts, mode, title: l.title, detail: l.detail, raw: s, live };
  });
  return { live: rows.filter((r) => r.live), paper: rows.filter((r) => !r.live) };
}
