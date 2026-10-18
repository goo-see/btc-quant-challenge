import { useState } from 'react';
import { api, type Portfolio, type Stats, type Trade, type Signal, type Execution, type Status } from '../api/client';
import { usePoll } from '../hooks';
import { krw, pct, num, btc, kst, kdate, kshort, ktime, kdur, skrw, sign, dd, ddClass } from '../format';
import { EquityChart } from '../charts/EquityChart';
import { CandleChart } from '../charts/CandleChart';
import { DecisionCard } from './Decision';
import { challengeView, nowState, splitSignals, todaySummary, tradeCards, type TradeCard } from '../view';

function Card({ k, v, s, cls }: { k: string; v: string; s?: string; cls?: string }) {
  return <div className="card"><div className="k">{k}</div><div className={`v ${cls ?? ''}`}>{v}</div>{s && <div className="s">{s}</div>}</div>;
}
const pnlText = (v: number | null | undefined, p: number | null | undefined) => `${skrw(v)}${p != null ? ` (${pct(p)})` : ''}`;

/* ------------------------------------------------------------------ 1. Challenge summary */
function ChallengeSummary({ status, pf }: { status: Status | null; pf: Portfolio | null }) {
  const c = challengeView(status, pf);
  return (
    <div className="section"><h2>챌린지 진행률</h2>
      <div className="panel">
        <div className="grid">
          <Card k="시작 금액" v={krw(c.start)} s={c.activatedAt ? `LIVE 시작 ${kdate(c.activatedAt)}` : undefined} />
          <Card k="현재 자산" v={krw(c.equity)} />
          <Card k="누적 수익" v={skrw(c.pnl)} cls={sign(c.pnl)} s={c.pnl != null && c.start ? pct((c.pnl / c.start) * 100) : undefined} />
          <Card k="목표" v={krw(c.goal)} s={`목표까지 ${krw(c.remaining)}`} />
        </div>
        <div className="progress"><div style={{ width: `${c.pct ?? 0}%` }} /></div>
        <div className="progress-labels"><span>완료율 {c.pct == null ? '—' : `${c.pct.toFixed(1)}%`}</span><span>{c.firstTradePending ? '아직 첫 거래 전입니다.' : `챌린지 시작 ${kdate(c.startedAt)} (첫 매수 체결)`}</span></div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 2. Now card */
function NowCard({ pf, trades, execs, status }: { pf: Portfolio | null; trades: Trade[]; execs: Execution[]; status: Status | null }) {
  const d = usePoll(api.decision, 10000).data;
  const s = nowState(pf, d, trades, execs);
  const paused = status && status.state !== 'RUNNING';
  const stateNote = paused ? <div className="alert">자동매매 상태: {status?.state} — {status?.execution_mode === 'EXIT_ONLY' ? '신규 매수는 중지, 보유 중인 BTC의 보호 매도만 자동 실행됩니다.' : status?.execution_mode === 'FROZEN' ? '자동 주문이 정지되었습니다. 시스템 탭에서 확인하세요.' : ''}</div> : null;
  if (s.kind === 'HOLDING') {
    return (
      <div className="section"><h2>현재 상태</h2>{stateNote}
        <div className="panel now holding">
          <div className="now-title">🟢 BTC 보유 중</div>
          <div className="now-sub">{kdate(s.entryTime)} 매수 · 보유 {kdur(s.heldSec)}</div>
          <div className="grid" style={{ marginTop: 10 }}>
            <Card k="매수 금액" v={krw(s.amount)} s={btc(s.qty)} />
            <Card k="평균 매수가" v={krw(s.avg)} />
            <Card k="현재 가격" v={krw(s.price)} s={`평가금액 ${krw(s.value)}`} />
            <Card k="현재 손익" v={pnlText(s.pnl, s.pnlPct)} cls={sign(s.pnl)} />
          </div>
        </div>
      </div>
    );
  }
  if (s.kind === 'CLOSED_RECENTLY') {
    const t = s.trade;
    return (
      <div className="section"><h2>현재 상태</h2>{stateNote}
        <div className="panel now">
          <div className="now-title">최근 거래 완료</div>
          <div className="flow"><span>{kdate(t.entryTime)} 매수</span><span className="arrow">↓</span><span>{kdate(t.exitTime)} 매도</span></div>
          <div className="grid" style={{ marginTop: 10 }}>
            <Card k="투자금" v={krw(t.invested)} />
            <Card k="회수금" v={krw(t.recovered)} />
            <Card k="수익" v={pnlText(t.pnl, t.pnlPct)} cls={sign(t.pnl)} s={`수수료 ${krw(t.fees)}`} />
            <Card k="보유 시간" v={kdur(t.heldSec)} s={`매도 이유: ${t.exitLabel ?? '—'}`} />
          </div>
          <div className="muted" style={{ marginTop: 6 }}>다음 매수 조건은 아래 카드를 확인하세요.</div>
        </div>
      </div>
    );
  }
  return (
    <div className="section"><h2>현재 상태</h2>{stateNote}
      <div className="panel now waiting">
        <div className="now-title">🟡 BTC 매수 대기 중</div>
        <div className="grid" style={{ marginTop: 10 }}>
          <Card k="현재가" v={krw(s.price)} />
          <Card k="매수 기준" v={krw(s.threshold)} />
          <Card k="매수하려면" v={s.gapPct == null ? '—' : s.gapPct > 0 ? `${pct(s.gapPct)} 상승 필요` : '기준 도달 — 마감 확인 대기'} s={s.gapKrw != null && s.gapKrw > 0 ? `${krw(s.gapKrw)} 부족` : undefined} />
          <Card k="다음 판단" v={kdate(s.nextEval)} s={s.evaluatedBar ? `최근 판단 ${kdate(s.evaluatedBar)}` : '첫 4시간봉 마감 대기'} />
        </div>
        <div style={{ marginTop: 8 }} className={s.trendOk == null ? 'muted' : s.trendOk ? 'pos' : 'neg'}>현재 추세: {s.trendOk == null ? '판단 대기' : s.trendOk ? '상승 추세 조건 통과' : '상승 추세 조건 미충족 (매수 안 함)'}</div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 3. Holding / next-buy conditions */
function HoldingOrConditions({ pf, execs }: { pf: Portfolio | null; execs: Execution[] }) {
  const d = usePoll(api.decision, 10000).data;
  const s = nowState(pf, d, [], execs);
  if (s.kind === 'HOLDING') {
    return (
      <div className="section"><h2>현재 보유</h2>
        <div className="panel">
          <div className="grid">
            <Card k="매수 시각" v={kdate(s.entryTime)} s={`보유 ${kdur(s.heldSec)}`} />
            <Card k="매수 금액 / 수량" v={krw(s.amount)} s={btc(s.qty)} />
            <Card k="평균 매수가 / 현재가" v={krw(s.avg)} s={`현재 ${krw(s.price)}`} />
            <Card k="평가금액 / 손익" v={krw(s.value)} s={pnlText(s.pnl, s.pnlPct)} cls={sign(s.pnl)} />
            <Card k="손절 기준" v={krw(s.initialStop)} s="매수 직후 정해진 기준" />
            <Card k="현재 자동 매도 기준" v={krw(s.stop)} s={s.stopDistPct == null ? undefined : `현재가 대비 ${pct(s.stopDistPct)}`} cls={s.stopDistPct != null && s.stopDistPct > -0.5 ? 'neg' : ''} />
          </div>
          <div className="muted" style={{ marginTop: 8 }}>가격이 자동 매도 기준에 도달하면 사용자 확인 없이 자동으로 매도합니다. 4시간봉 마감 시 최근 가격 범위 이탈·상승 추세 종료 조건도 함께 확인합니다.</div>
        </div>
      </div>
    );
  }
  const w = s.kind === 'WAITING' ? s : null;
  return (
    <div className="section"><h2>다음 매수 조건</h2>
      <div className="panel">
        <div className="grid">
          <Card k="현재 BTC" v={krw(w?.price)} />
          <Card k="매수 기준 (최근 5일 고점)" v={krw(w?.threshold)} />
          <Card k="아직" v={w?.gapKrw == null ? '—' : w.gapKrw > 0 ? `${krw(w.gapKrw)} 부족` : '기준 도달'} s={w?.gapPct != null ? `(${pct(w.gapPct)})` : undefined} />
          <Card k="추세 조건" v={w?.trendOk == null ? '—' : w.trendOk ? '✅ 통과' : '❌ 미충족'} s="일봉 종가 > EMA100" />
          <Card k="다음 확인" v={w?.nextEval ? ktime(w.nextEval) : '—'} s={w?.nextEval ? kdate(w.nextEval) : undefined} />
        </div>
        <div className="muted" style={{ marginTop: 8 }}>가격이 매수 기준을 넘더라도 4시간봉이 마감될 때 조건을 확인합니다. 조건이 맞으면 자산의 30% (최대 ₩30,000)만 매수합니다.</div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 4. Today */
function Today({ execs, trades, pf }: { execs: Execution[]; trades: Trade[]; pf: Portfolio | null }) {
  const t = todaySummary(execs, trades, pf);
  return (
    <div className="section"><h2>오늘 요약</h2>
      <div className="grid">
        <Card k="오늘 매수" v={`${t.buys}회`} />
        <Card k="오늘 매도" v={`${t.sells}회`} />
        <Card k="오늘 확정 손익" v={skrw(t.realized)} cls={sign(t.realized)} s={`수수료 ${krw(t.fees)}`} />
        <Card k="현재 보유" v={t.holding ? 'BTC 보유 중' : '없음'} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ 5. Trade history */
function TradeRow({ t, idx, showRaw }: { t: TradeCard; idx: number; showRaw: boolean }) {
  return (
    <div className={`trade ${t.open ? 'open' : ''}`}>
      <div className="trade-head">
        <strong>{kshort(t.entryTime).slice(0, 5)} 거래 #{idx}{t.open ? ' — 진행 중' : ''}</strong>
        {t.beforeChallenge && <span className="badge warn">챌린지 시작 전</span>}
        {!t.open && <span className={`badge ${(t.pnl ?? 0) >= 0 ? 'ok' : 'bad'}`}>{(t.pnl ?? 0) >= 0 ? '수익' : '손실'}</span>}
      </div>
      <div className="trade-flow">
        <div><span className="muted">{ktime(t.entryTime)}</span> BTC {krw(t.invested)} 매수 <span className="muted">@ {krw(t.entryPrice)}</span></div>
        <div className="arrow">↓ {t.open ? `보유 ${kdur(t.heldSec)} (매도 아직 안 함)` : `${kdur(t.heldSec)} 보유`}</div>
        {t.open
          ? <div><span className="muted">현재</span> 평가금액 {krw(t.recovered)} · 자동 매도 기준 아래 카드 참고</div>
          : <div><span className="muted">{ktime(t.exitTime)}</span> {krw(t.recovered)} 매도 <span className="muted">@ {krw(t.exitPrice)}</span></div>}
      </div>
      <div className="grid cols3" style={{ marginTop: 8 }}>
        <Card k={t.open ? '현재 손익' : '수익'} v={pnlText(t.pnl, t.pnlPct)} cls={sign(t.pnl)} />
        <Card k="수수료" v={t.open ? '매도 후 확정' : krw(t.fees)} />
        <Card k="매도 이유" v={t.open ? '—' : (t.exitLabel ?? '—')} s={showRaw && t.rawExit ? t.rawExit : undefined} />
      </div>
    </div>
  );
}

function TradeHistory({ trades, pf, status, showRaw }: { trades: Trade[]; pf: Portfolio | null; status: Status | null; showRaw: boolean }) {
  const cards = tradeCards(trades, pf, status?.challenge.challenge_started_at ?? null);
  const total = cards.length;
  return (
    <div className="section"><h2>거래 내역</h2>
      {cards.length === 0 && <div className="panel muted">아직 거래가 없습니다. 첫 매수는 4시간봉 마감 시 조건이 맞을 때 자동으로 이루어집니다.</div>}
      {cards.map((t, i) => <TradeRow key={t.id} t={t} idx={total - i} showRaw={showRaw} />)}
    </div>
  );
}

/* ------------------------------------------------------------------ 8. Recent decisions */
function RecentDecisions({ signals, status, pf, showRaw }: { signals: Signal[]; status: Status | null; pf: Portfolio | null; showRaw: boolean }) {
  const { live, paper } = splitSignals(signals, status?.challenge.live_activated_at, status?.mode, pf?.avg_entry);
  const Row = ({ r }: { r: ReturnType<typeof splitSignals>['live'][number] }) => (
    <div className="decision-row">
      <span className="muted">{kshort(r.ts)}</span>
      <span className={`badge ${r.mode === 'LIVE' ? 'bad' : 'info'}`} style={{ fontSize: 10 }}>{r.mode}</span>
      <span style={{ fontWeight: 700 }}>{r.title}</span>
      <span className="muted">{r.detail}</span>
      {showRaw && <span className="muted">{r.raw.action} · {r.raw.reason}{r.raw.risk ? ` · ${r.raw.risk.execution}${r.raw.risk.reason ? ` ${r.raw.risk.reason}` : ''}` : ''}</span>}
    </div>
  );
  return (
    <div className="section"><h2>최근 판단</h2>
      <div className="panel">
        {status?.mode === 'LIVE' && <div className="divider">LIVE 시작 {kshort(status.challenge.live_activated_at)} 이후</div>}
        {live.length === 0 && <div className="muted">LIVE 판단 기록이 아직 없습니다 (첫 4시간봉 마감 대기).</div>}
        {live.slice(0, 8).map((r) => <Row key={r.id} r={r} />)}
        {paper.length > 0 && <details style={{ marginTop: 8 }}><summary className="muted">이전 PAPER 기록 (LIVE 전 검증, 현재 판단 아님) {paper.length}건</summary>{paper.slice(0, 10).map((r) => <Row key={r.id} r={r} />)}</details>}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ main */
export function Trading({ pf, stats, status }: { pf: Portfolio | null; stats: Stats | null; status: Status | null }) {
  const [tf, setTf] = useState('1h');
  const [showRaw, setShowRaw] = useState(false);
  const eq = usePoll(api.equity, 30000);
  const candles = usePoll(() => api.candles(tf), 30000, [tf]);
  const trades = usePoll(api.trades, 15000);
  const signals = usePoll(api.signals, 10000);
  const execs = usePoll(api.executions, 15000);
  const readiness = usePoll(api.readiness, 60000);
  const tr = trades.data ?? [];
  const ex = execs.data ?? [];
  return (
    <>
      <ChallengeSummary status={status} pf={pf} />
      <NowCard pf={pf} trades={tr} execs={ex} status={status} />
      <HoldingOrConditions pf={pf} execs={ex} />
      <Today execs={ex} trades={tr} pf={pf} />
      <TradeHistory trades={tr} pf={pf} status={status} showRaw={showRaw} />
      <div className="section"><h2>자산 변화</h2>
        <div className="grid cols3" style={{ marginBottom: 8 }}>
          <Card k="현재 자산" v={krw(pf?.equity)} />
          <Card k="누적 손익" v={skrw(pf?.total_pnl)} cls={sign(pf?.total_pnl)} s={pct(pf?.total_return_pct)} />
          <Card k="최대 낙폭" v={dd(pf?.max_drawdown_pct)} cls={ddClass(pf?.max_drawdown_pct)} />
        </div>
        <div className="panel">{eq.data && eq.data.length > 1 ? <EquityChart points={eq.data} initial={pf?.initial_equity ?? 100000} /> : <div className="muted">자산 기록이 쌓이면 표시됩니다…</div>}</div>
      </div>
      <div className="section"><h2>BTC 가격 차트</h2>
        <div className="tabs" style={{ position: 'static' }}>{['1m', '5m', '15m', '1h'].map((t) => <button key={t} className={t === tf ? 'active' : ''} onClick={() => setTf(t)}>{t}</button>)}</div>
        <div className="panel">{candles.data && candles.data.candles.length ? <CandleChart data={candles.data} /> : <div className="muted">불러오는 중…</div>}</div>
        <div className="muted" style={{ marginTop: 4 }}>차트 표시 주기({tf})이며, 자동매매 전략은 4시간봉 기준으로 판단합니다.</div>
      </div>
      <RecentDecisions signals={signals.data ?? []} status={status} pf={pf} showRaw={showRaw} />
      <div className="section"><h2>시스템 상태</h2>
        <div className="panel" style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
          <span>{readiness.data ? (readiness.data.groups.every((g) => g.status === 'PASS' || g.status === 'WARN') ? '✅ 정상' : '⚠ 확인 필요') : '…'} · 모드 <strong>{status?.mode ?? '…'}</strong> · {status?.state === 'RUNNING' ? '자동매매 실행 중' : status?.state ?? '…'}</span>
          <span className="muted">상세 항목(Research · Infrastructure · Private API · 사전 검증 기록)은 System 탭</span>
        </div>
      </div>
      <div className="section">
        <button className="toggle" onClick={() => setShowRaw((v) => !v)}>{showRaw ? '기술 정보 숨기기 ▲' : '기술 정보 보기 ▼'}</button>
        {showRaw && (
          <>
            <div className="section"><h2>판단 상세 (엔진 기준값)</h2><DecisionCard /></div>
            <div className="section"><h2>내 자산 (상세)</h2>
              <div className="grid">
                <Card k="Starting" v={krw(pf?.initial_equity)} />
                <Card k="Equity" v={krw(pf?.equity)} s={`HWM ${krw(pf?.hwm)}`} />
                <Card k="KRW" v={krw(pf?.krw)} />
                <Card k="BTC" v={btc(pf?.btc)} s={krw(pf?.btc_value)} />
                <Card k="Realized" v={krw(pf?.realized_pnl)} cls={sign(pf?.realized_pnl)} />
                <Card k="Unrealized" v={krw(pf?.unrealized_pnl)} cls={sign(pf?.unrealized_pnl)} />
                <Card k="Today" v={krw(pf?.today_pnl)} s={`7D ${krw(stats?.realized_7d)}`} cls={sign(pf?.today_pnl)} />
                <Card k="Fees" v={krw(pf?.fees_total)} s={`consec. losses ${pf?.consecutive_losses ?? 0}`} />
              </div>
            </div>
            <div className="section"><h2>거래 성과</h2>
              <div className="grid">
                <Card k="거래 수" v={String(stats?.trade_count ?? '—')} s={`승 ${stats?.wins ?? 0} / 패 ${stats?.losses ?? 0}`} />
                <Card k="승률" v={stats ? `${(stats.win_rate * 100).toFixed(1)}%` : '—'} />
                <Card k="Profit factor" v={stats?.profit_factor == null ? '—' : Number.isFinite(stats.profit_factor) ? num(stats.profit_factor) : '∞'} />
                <Card k="기대값" v={krw(stats?.expectancy)} cls={sign(stats?.expectancy)} />
                <Card k="평균 수익" v={krw(stats?.average_win)} cls="pos" />
                <Card k="평균 손실" v={krw(stats?.average_loss)} cls="neg" />
                <Card k="Payoff" v={num(stats?.payoff_ratio)} />
                <Card k="총 수수료" v={krw(stats?.fees_paid)} />
              </div>
            </div>
            <div className="section"><h2>체결 상세 (slippage)</h2>
              <div className="panel tablewrap">
                <table><thead><tr><th>체결</th><th>Side</th><th>Signal</th><th>Fill</th><th>Slip bps</th><th>Fee</th><th>Stop / dist</th><th>Exit</th></tr></thead>
                  <tbody>{ex.map((e: Execution) => (
                    <tr key={e.id}><td>{kst(e.filled_at)}</td><td className={e.side === 'BUY' ? 'pos' : 'neg'}>{e.side}</td><td>{krw(e.signal_price)}</td><td>{krw(e.actual_fill_price)}</td>
                      <td className={(e.slippage_bps ?? 0) > 0 ? 'neg' : ''}>{e.slippage_bps == null ? '—' : e.slippage_bps.toFixed(2)}</td><td>{krw(e.fee)}</td>
                      <td>{e.side === 'BUY' ? `${krw(e.initial_stop)}${e.stop_distance_from_fill != null ? ` (${(e.stop_distance_from_fill * 100).toFixed(2)}%)` : ''}` : '—'}</td>
                      <td>{e.side === 'SELL' ? `${e.exit_reason ?? ''} @ ${krw(e.exit_trigger_price)} → ${krw(e.realized_pnl)} (${pct(e.return_pct)})` : '—'}</td></tr>
                  ))}{!ex.length && <tr><td colSpan={8} className="muted">체결 기록 없음</td></tr>}</tbody></table>
              </div>
            </div>
            <div className="section"><h2>Raw signals</h2>
              <div className="panel tablewrap">
                <table><thead><tr><th>Time</th><th>Mode</th><th>Signal</th><th>Reason</th><th>Close</th><th>ATR</th><th>Execution</th></tr></thead>
                  <tbody>{(signals.data ?? []).map((s: Signal) => (
                    <tr key={s.id}><td>{kst(s.ts)}</td><td>{s.mode ?? '—'}</td><td className={s.action === 'BUY' ? 'pos' : s.action === 'SELL' ? 'neg' : ''}>{s.action}</td><td>{s.reason}</td>
                      <td>{krw(s.indicators.close)}</td><td>{krw(s.indicators.atr)}</td>
                      <td>{s.risk ? (s.risk.approved ? <span className="pos">EXECUTED {krw(s.risk.order_krw)}</span> : s.risk.execution === 'SUPPRESSED' ? <span className="muted">SUPPRESSED · {s.risk.reason}</span> : <span className="neg">BLOCKED · {s.risk.reason}</span>) : <span className="muted">—</span>}</td></tr>
                  ))}</tbody></table>
              </div>
            </div>
          </>
        )}
      </div>
    </>
  );
}
