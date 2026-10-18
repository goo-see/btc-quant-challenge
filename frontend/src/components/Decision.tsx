import { api } from '../api/client';
import { usePoll } from '../hooks';
import { krw, kst } from '../format';

const Row = ({ ok, text }: { ok: boolean | null | undefined; text: string }) => (
  <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
    <span className={ok == null ? 'muted' : ok ? 'pos' : 'neg'} style={{ width: 14, fontWeight: 800 }}>{ok == null ? '·' : ok ? '✓' : '✗'}</span>
    <span>{text}</span>
  </div>
);

export function DecisionCard() {
  const { data: d } = usePoll(api.decision, 10000);
  if (!d || !d.decision) return <div className="panel muted">No strategy evaluation recorded yet (first {d?.timeframe ?? '4h'} bar close pending).</div>;
  const inPos = !!d.position_quantity;
  const risk = d.risk_status;
  const riskOk = risk ? risk.execution_mode === 'ENTRY_AND_EXIT' && risk.inflight === 0 && risk.unknown === 0 : null;
  const n = (v: number | string | null | undefined) => (v == null ? null : Number(v));
  return (
    <div className="panel">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: 8 }}>
        <div><span className="muted" style={{ fontSize: 11, letterSpacing: '.06em' }}>CURRENT DECISION</span><div style={{ fontSize: 26, fontWeight: 800 }}>{d.decision} <span className="muted" style={{ fontSize: 13 }}>{d.reason}</span></div></div>
        <div className="muted" style={{ textAlign: 'right', fontSize: 12 }}>as of {kst(d.market_bar_time)} KST ({d.timeframe} close)<br />next evaluation {kst(d.next_evaluation)} KST · {d.strategy_version}</div>
      </div>
      <div style={{ marginTop: 10, display: 'grid', gap: 4 }}>
        <Row ok={d.trend_ok} text={`Daily trend: close ${krw(n(d.daily_close))} ${d.trend_ok ? '>' : '≤'} EMA100 ${krw(n(d.daily_ema))}`} />
        {!inPos && <Row ok={d.breakout_ok} text={`30-bar breakout: 4H close ${krw(n(d.close_4h))} ${d.breakout_ok ? '>' : '≤'} ${krw(n(d.donchian_upper))}${d.breakout_ok ? '' : ` (rule threshold; +${d.close_4h && d.donchian_upper ? (((d.donchian_upper / d.close_4h) - 1) * 100).toFixed(2) : '—'}% above last close)`}`} />}
        <Row ok={!inPos ? true : null} text={inPos ? `Open position ${d.position_quantity} BTC · entry ${krw(n(d.entry_price))} · now ${krw(n(d.current_price))}` : 'No open position'} />
        <Row ok={riskOk} text={`Risk system: ${risk?.state ?? '—'} / ${risk?.execution_mode ?? '—'}${risk?.state_reason ? ` (${risk.state_reason})` : ''} · in-flight ${risk?.inflight ?? 0} · unknown ${risk?.unknown ?? 0}`} />
      </div>
      {inPos ? (
        <div className="grid" style={{ marginTop: 10 }}>
          <div className="card"><div className="k">ATR / current stop</div><div className="v">{krw(n(d.current_stop))}</div><div className="s">tick-executed</div></div>
          <div className="card"><div className="k">Trailing stop (rule)</div><div className="v">{krw(n(d.trailing_stop))}</div><div className="s">highest − 4×ATR, ratchets at 4H close</div></div>
          <div className="card"><div className="k">20-bar channel exit</div><div className="v">{krw(n(d.channel_exit))}</div><div className="s">4H close below → SELL</div></div>
          <div className="card"><div className="k">Daily EMA100 trend</div><div className={`v ${d.trend_loss ? 'neg' : 'pos'}`}>{d.trend_loss ? 'LOST → SELL at 4H close' : 'PASS'}</div></div>
        </div>
      ) : (
        <div className="grid" style={{ marginTop: 10 }}>
          <div className="card"><div className="k">4H close</div><div className="v">{krw(n(d.close_4h))}</div></div>
          <div className="card"><div className="k">Breakout threshold</div><div className="v">{krw(n(d.donchian_upper))}</div><div className="s">max high of previous 30 × 4H bars</div></div>
          <div className="card"><div className="k">ATR14 (4H)</div><div className="v">{krw(n(d.atr))}</div><div className="s">initial stop = close − 3×ATR</div></div>
          <div className="card"><div className="k">Exit channel (20)</div><div className="v">{krw(n(d.donchian_lower))}</div></div>
        </div>
      )}
      <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>Fixed-rule trigger thresholds only — not a forecast.</div>
    </div>
  );
}
