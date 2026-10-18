import type { Status } from '../api/client';
import { krw, btc, kst, sign } from '../format';

export function HaltedWarning({ status }: { status: Status | null }) {
  const w = status?.halted_position_warning;
  if (!w) return null;
  return (
    <div className="warning">
      <h3>⚠ {w.title}</h3>
      <div className="grid">
        <div className="card"><div className="k">HALTED reason</div><div className="v">{w.halted_reason}</div><div className="s">since {kst(w.halted_since)}</div></div>
        <div className="card"><div className="k">Trading</div><div className="v neg">DISABLED</div><div className="s">manual RESUME required</div></div>
        <div className="card"><div className="k">Position</div><div className="v">{btc(w.quantity_btc)}</div><div className="s">{krw(w.position_value)}</div></div>
        <div className="card"><div className="k">Entry / Current</div><div className="v">{krw(w.entry_price)}</div><div className="s">now {krw(w.current_price)}</div></div>
        <div className="card"><div className="k">Unrealized PnL</div><div className={`v ${sign(w.unrealized_pnl)}`}>{krw(w.unrealized_pnl)}</div></div>
        <div className="card"><div className="k">Stop price</div><div className="v">{krw(w.stop_price)}</div><div className="s">not enforced while HALTED</div></div>
      </div>
    </div>
  );
}
