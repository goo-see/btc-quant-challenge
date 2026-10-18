import type { EthLive, EthFill, EthLadderRow } from '../api/client';
import { krw, pct, kdate, ktime, kdur, skrw, sign } from '../format';

/** LIVE EXPERIMENT · 실제 돈 — never "검증 완료 / 안전 / 수익 보장". */
export const ExpBadge = () => <span className="badge bad exp">LIVE EXPERIMENT · 실제 돈</span>;

const STATE_KO: Record<string, string> = { OPEN: '주문 중', WAIT: '대기' };

function ethQty(v: number | string | null | undefined) {
  if (v == null) return '—';
  const n = Number(v);
  return Number.isNaN(n) ? '—' : `${n.toFixed(8).replace(/\.?0+$/, '')} ETH`;
}

export function statusLabel(e: EthLive | null): { cls: string; text: string } {
  if (!e || e.status === 'NOT_STARTED') return { cls: 'info', text: '미시작' };
  if (!e.online) return { cls: 'bad', text: '프로세스 오프라인' };
  if (e.buy_pause?.paused && e.status === 'BUY_PAUSED') return { cls: 'warn', text: 'BUY PAUSED · 재고 위험 (매도 유지)' };
  switch (e.status) {
    case 'RUNNING': return { cls: 'ok', text: 'RUNNING · 운용 중' };
    case 'BUY_PAUSED': return { cls: 'warn', text: 'BUY_PAUSED · 매수 일시중지' };
    case 'HALTED': return { cls: 'bad', text: 'HALTED · MDD 중단(매도만)' };
    case 'FROZEN': return { cls: 'bad', text: 'FROZEN · 자동 주문 정지' };
    case 'SEEDING': return { cls: 'warn', text: 'SEEDING · 초기 매수 중' };
    case 'WAITING_ENABLE': return { cls: 'info', text: '활성화 대기 · 주문 없음' };
    default: return { cls: 'info', text: e.status };
  }
}

export function Ladder({ e, compact }: { e: EthLive; compact?: boolean }) {
  const l = e.ladder;
  if (!l || !e.price) return <div className="muted">가격 수신 대기 중…</div>;
  const sells = [...(l.sells ?? [])].sort((a, b) => Number(a.price) - Number(b.price)).slice(0, compact ? 2 : 5).reverse();
  const buys = (l.buys ?? []).slice(0, compact ? 2 : 3);
  const label = (r: EthLadderRow) => {
    const g = r.gap_pct != null ? `${(Number(r.gap_pct) * 100).toFixed(2)}%` : '';
    if (r.side === 'SELL') return r.legacy ? `LEGACY SELL · ${g}` : `V2 SELL · ${g}`;
    return (r.strategy_epoch ?? '').startsWith('AGGRESSIVE_V2') ? `V2 BUY · ${g}` : `BUY · ${g}`;
  };
  const Row = ({ r }: { r: EthLadderRow }) => (
    <div className={`rung ${r.side.toLowerCase()} ${r.state === 'OPEN' ? 'open' : ''} ${r.legacy ? 'legacy' : ''}`}>
      <span className="side">{label(r)}</span>
      <span className="price">{krw(Number(r.price))}</span>
      <span className="amt">{krw(Number(r.amount))}</span>
      <span className={`st ${r.state}`}>{STATE_KO[r.state] ?? r.state}</span>
    </div>
  );
  return (
    <div className="ladder live">
      {sells.map((r) => <Row key={`s${r.lot_id ?? r.price}`} r={r} />)}
      <div className="rung current"><span className="side">CURRENT</span><span className="price">{krw(e.price)}</span><span className="amt">ETH 현재가</span><span className="st" /></div>
      {buys.map((r) => <Row key={`b${r.level ?? r.price}`} r={r} />)}
      {!sells.length && !buys.length && <div className="muted">레벨 없음 (seed 전)</div>}
    </div>
  );
}

export function Fills({ fills, limit = 6, showRaw }: { fills: EthFill[]; limit?: number; showRaw?: boolean }) {
  const rows = fills.slice(0, limit);
  if (!rows.length) return <div className="muted">아직 체결 없음</div>;
  return (
    <div className="fills">
      {rows.map((f) => (
        <div className={`fill ${f.side.toLowerCase()}`} key={f.client_order_id}>
          <div className="fill-l"><div className="fill-t">{ktime(f.ts)} <strong>{f.side}</strong>{f.kind === 'SEED' ? ' · 초기' : ''}</div><div className="muted">{kdate(f.ts)}</div></div>
          <div className="fill-r">
            <div className="fill-krw">{krw(f.krw)}</div>
            <div className="muted">@ {krw(f.price)} · {ethQty(f.quantity)}</div>
            {f.side === 'SELL' && f.net_pnl != null && <div className={`fill-pnl ${sign(f.net_pnl)}`}>순익 {skrw(f.net_pnl)}</div>}
            {showRaw && <div className="muted raw">{f.client_order_id} · {f.uuid ?? '—'} · fee {krw(f.fee)}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

const n = (v: string | number | null | undefined) => (v == null ? null : Number(v));

export function CompoundingRows({ e }: { e: EthLive }) {
  const c = e.compounding;
  if (!c) return null;
  const unit = n(c.current_unit); const nxt = n(c.next_unit); const need = n(c.realized_needed_for_next_unit); const cap = n(c.max_single_order_krw);
  const nextText = nxt != null && need != null ? `다음 unit ${krw(nxt)}까지 ${need > 0 ? `+${krw(need)} 실현 필요` : '도달 — 다음 경계에서 적용'}` : unit != null && cap != null && unit >= cap ? `상한 ${krw(cap)} 도달` : '—';
  return (
    <>
      <div className="h3">복리 자금 관리 <span className={`badge ${c.enabled ? 'ok' : 'info'}`}>{c.enabled ? '복리 모드 ON' : '복리 모드 OFF'}</span></div>
      <div className="kv">
        <span className="k">초기 전략 자금</span><span className="v">{krw(n(c.starting_capital))}</span>
        <span className="k">현재 총자산 (GRID TOTAL EQUITY)</span><span className="v">{krw(n(c.current_equity))}</span>
        <span className="k">복리 기준 자본 (실현 기준)</span><span className="v">{krw(n(c.compounding_capital))}</span>
        <span className="k">현재 주문 단위</span><span className="v">{krw(unit)} <small className="muted">({c.sizing_epoch})</small></span>
        <span className="k">다음 주문 단위 상승 기준</span><span className="v">{nextText}</span>
        <span className="k">누적 실현 순익</span><span className={`v ${sign(n(c.realized_net_pnl))}`}>{skrw(n(c.realized_net_pnl))}</span>
        <span className="k">평가손익 (증액 기준 제외)</span><span className={`v ${sign(n(c.unrealized_pnl))}`}>{skrw(n(c.unrealized_pnl))}</span>
      </div>
      {c.paused_reason && <div className="notice">복리 기준 자본이 최소 주문 단위보다 작아 신규 매수가 중지되었습니다 (COMPOUNDING_CAPITAL_TOO_LOW).</div>}
    </>
  );
}

export function CompoundingSection({ e }: { e: EthLive }) {
  const c = e.compounding;
  if (!c) return null;
  const a = c.attribution;
  return (
    <div className="scard">
      <div className="scard-head"><strong>COMPOUNDING — 돈으로 돈을 번다</strong><span className={`badge ${c.enabled ? 'ok' : 'info'}`}>{c.enabled ? 'ENABLED' : 'OFF'} · AUTO REBALANCE OFF</span></div>
      <div className="grid2">
        <Big k="Starting Capital" v={krw(n(c.starting_capital))} />
        <Big k="Realized Capital" v={krw(n(c.compounding_capital))} s="₩50,000 + 실현 순손익 (수수료 차감)" />
        <Big k="Current Equity" v={krw(n(c.current_equity))} s="KRW + ETH 평가" />
        <Big k="Current Unit" v={krw(n(c.current_unit))} s={`unit ratio ${(Number(c.unit_ratio) * 100).toFixed(0)}% · 상한 ${krw(n(c.max_single_order_krw))}`} />
        <Big k="Capital Growth" v={`${pct(n(c.capital_growth_realized_pct))} realized`} s={`${pct(n(c.capital_growth_equity_pct))} total equity`} cls={sign(n(c.capital_growth_realized_pct))} />
        <Big k="Profit Reinvested" v={krw(n(c.profit_reinvested))} s="재투자된 실현 순이익" />
      </div>
      <div className="h3">성과 Attribution</div>
      <div className="kv">
        <span className="k">A. Grid Realized Profit (완료 cycle 확정)</span><span className={`v ${sign(n(a.grid_realized_profit))}`}>{skrw(n(a.grid_realized_profit))}</span>
        <span className="k">B. Unrealized PnL (보유 ETH 가격 변동)</span><span className={`v ${sign(n(a.unrealized_pnl))}`}>{skrw(n(a.unrealized_pnl))}</span>
        <span className="k">C. Compounding Contribution (unit 확대로 추가된 실현손익)</span><span className="v">{a.compounding_contribution == null ? 'N/A (큰 unit cycle 없음)' : skrw(n(a.compounding_contribution))}</span>
      </div>
      <div className="h3">해당 자본에 도달할 경우 주문 단위 (예상 수익 아님)</div>
      <ul className="plain">{(c.projected_units ?? []).map((p) => <li key={p.capital}>{krw(Number(p.capital))} → unit {krw(Number(p.unit))}</li>)}</ul>
      <div className="h3">한도 (비율 고정)</div>
      <div className="kv">
        <span className="k">최대 ETH 재고 ({(Number(c.max_inventory_ratio) * 100).toFixed(0)}%)</span><span className="v">{krw(n(c.max_inventory_krw))}</span>
        <span className="k">최소 KRW 보유 ({(Number(c.min_cash_ratio) * 100).toFixed(0)}%)</span><span className="v">{krw(n(c.min_cash_krw))}</span>
        <span className="k">1회 증액 한도</span><span className="v">+{(Number(c.max_step_up_pct ?? 0) * 100).toFixed(0)}% · ₩{Number(c.round_krw ?? 1000).toLocaleString('ko-KR')} 단위 내림</span>
        <span className="k">Sizing epoch 이력</span><span className="v">{c.sizing_history.map((h) => `${h.epoch} ${krw(Number(h.unit))} @ ${krw(Number(h.capital))}`).join(' → ') || '—'}</span>
      </div>
      <div className="muted" style={{ marginTop: 8 }}>회전량 {krw(e.book?.turnover)} · cycles {e.book?.completed_cycles ?? 0} · gross grid profit {skrw(e.book?.gross_grid_profit)} · fees {krw(e.book?.total_fees)} · net realized {skrw(e.book?.realized_pnl)} · GRID TOTAL EQUITY {krw(e.book?.current_equity)} — 회전량은 수익이 아닙니다.</div>
    </div>
  );
}

const Big = ({ k, v, s, cls }: { k: string; v: string; s?: string; cls?: string }) => (
  <div className="big"><div className="k">{k}</div><div className={`v ${cls ?? ''}`}>{v}</div>{s && <div className="s">{s}</div>}</div>
);

export function EthLiveCard({ e, onDetail, full }: { e: EthLive | null; onDetail?: () => void; full?: boolean }) {
  const b = e?.book;
  const st = statusLabel(e);
  const eq = b?.current_equity ?? null;
  const start = b?.starting_equity ?? e?.allocated_capital ?? 50000;
  const net = eq != null ? eq - start : null;
  const notStarted = !e || e.status === 'NOT_STARTED';
  return (
    <div className={`scard ethlive ${st.cls}`}>
      <div className="scard-head"><div><strong>ETH 반복매매</strong><div className="muted">연구 Gate 미통과 전략 · 실제 돈 소액 실험</div></div><div className="badges">{e?.buy_pause?.paused && <span className="badge warn exp">BUY PAUSED · 재고 위험</span>}<ExpBadge /></div></div>
      {e?.buy_pause?.paused && (
        <div className="pause-box">
          <div className="pause-title">신규 매수 일시중단 — {e.buy_pause.reason}</div>
          <div className="kv">
            <span className="k">Grid Total Equity</span><span className="v">{krw(e.book?.current_equity)}</span>
            <span className="k">Realized PnL</span><span className={`v ${sign(e.book?.realized_pnl)}`}>{skrw(e.book?.realized_pnl)}</span>
            <span className="k">Unrealized PnL</span><span className={`v ${sign(e.book?.unrealized_pnl)}`}>{skrw(e.book?.unrealized_pnl)}</span>
            <span className="k">ETH inventory</span><span className="v">{e.buy_pause.inventory_ratio_pct != null ? `${e.buy_pause.inventory_ratio_pct.toFixed(1)}%` : '—'} ({krw(e.book?.eth_inventory_value)})</span>
            <span className="k">KRW reserve</span><span className="v">{e.buy_pause.cash_ratio_pct != null ? `${e.buy_pause.cash_ratio_pct.toFixed(1)}%` : '—'} ({krw(e.book?.krw_inventory)})</span>
            <span className="k">Open lots</span><span className="v">{e.buy_pause.open_lots}</span>
            <span className="k">가장 가까운 SELL 목표</span><span className="v">{krw(e.buy_pause.nearest_sell_target)}{e.buy_pause.nearest_sell_distance_pct != null ? ` (현재가 대비 ${pct(e.buy_pause.nearest_sell_distance_pct)})` : ''}</span>
          </div>
          <div className="pause-note">신규 매수는 중단되었지만 기존 ETH는 목표가 도달 시 자동 매도됩니다. 강제 청산은 하지 않습니다.</div>
        </div>
      )}
      {e && e.status !== 'NOT_STARTED' && (
        <div className="kv strat">
          <span className="k">Active Strategy</span><span className="v">{e.active_strategy?.label ?? '—'}{e.active_strategy?.cutover_pending ? ' (전환 대기)' : ''}</span>
          <span className="k">Compounding</span><span className="v">{e.compounding?.enabled ? 'ON' : 'OFF'}</span>
          <span className="k">Legacy</span><span className="v">{e.legacy?.lots ? `G0 ${e.legacy.gap_pct != null ? (e.legacy.gap_pct * 100).toFixed(2) : '0.75'}% · ${e.legacy.lots} lots · 매도 전용` : '없음'}</span>
          <span className="k">현재 주문 단위</span><span className="v">{krw(e.limits?.unit_krw)}</span>
        </div>
      )}
      <div className="row-between"><span className="muted">현재 상태</span><span className={`badge ${st.cls}`}>{st.text}</span></div>
      {e?.status_reasons?.length ? <div className="notice">{e.status_reasons.join(' · ')}</div> : null}
      {notStarted ? (
        <div className="muted" style={{ marginTop: 8 }}>ETH Grid 프로세스가 아직 시작되지 않았습니다. 시작금 {krw(start)} · 실제 ETH 주문 0.</div>
      ) : (
        <>
          <div className="grid2">
            <Big k="시작금" v={krw(start)} />
            <Big k="현재 전략 자산 (GRID TOTAL EQUITY)" v={krw(eq)} cls={eq != null && eq < start ? 'neg' : eq != null && eq > start ? 'pos' : ''} />
            <Big k="누적 순손익" v={net != null ? `${skrw(net)} (${pct(start ? (net / start) * 100 : null)})` : '—'} cls={sign(net)} />
            <Big k="오늘 순손익" v={skrw(b?.today_pnl)} cls={sign(b?.today_pnl)} />
            <Big k="오늘 거래" v={`BUY ${b?.today_buys ?? 0} / SELL ${b?.today_sells ?? 0}`} />
            <Big k="완료 Cycle" v={String(b?.completed_cycles ?? 0)} s={b?.avg_cycle_profit != null ? `평균 ${skrw(b.avg_cycle_profit)} · 중앙값 ${skrw(b.median_cycle_profit)}` : undefined} />
            <Big k="현재 구성 ETH" v={krw(b?.eth_inventory_value)} s={ethQty(b?.eth_inventory_qty)} />
            <Big k="현재 구성 KRW" v={krw(b?.krw_inventory)} />
            <Big k="누적 수수료" v={krw(b?.total_fees)} />
            <Big k="MDD" v={b?.mdd_pct != null ? `${Number(b.mdd_pct).toFixed(2)}%` : '—'} s={b?.current_drawdown_pct != null ? `현재 낙폭 ${Number(b.current_drawdown_pct).toFixed(2)}% · 고점 ${krw(b.high_watermark)}` : undefined} cls={b?.mdd_pct ? 'neg' : ''} />
          </div>
          {full && (
            <div className="grid2" style={{ marginTop: 10 }}>
              <Big k="실현 손익" v={skrw(b?.realized_pnl)} cls={sign(b?.realized_pnl)} />
              <Big k="평가 손익" v={skrw(b?.unrealized_pnl)} cls={sign(b?.unrealized_pnl)} />
              <Big k="Grid 총이익(수수료 전)" v={skrw(b?.gross_grid_profit)} />
              <Big k="거래대금" v={krw(b?.turnover)} s={`매수 ${b?.buy_count ?? 0} / 매도 ${b?.sell_count ?? 0}`} />
              <Big k="최고 / 최악 Cycle" v={`${skrw(b?.best_cycle)} / ${skrw(b?.worst_cycle)}`} />
              <Big k="평균 Cycle 시간" v={b?.avg_cycle_duration_sec != null ? kdur(b.avg_cycle_duration_sec) : '—'} />
            </div>
          )}
          <CompoundingRows e={e} />
          <div className="h3">실제 Grid 가격 레벨 <span className="muted">(현재 unit ₩{(e?.limits?.unit_krw ?? 5000).toLocaleString('ko-KR')} · 간격 {e?.gap_pct != null ? (e.gap_pct * 100).toFixed(2) : '0.75'}%)</span></div>
          <Ladder e={e} compact={!full} />
          <div className="h3">최근 체결</div>
          <Fills fills={e.recent_fills ?? []} limit={full ? 20 : 4} showRaw={full} />
          {full && (
            <details className="acc"><summary>상세 (프로세스·대조·추세)</summary>
              <div className="kv">
                <span className="k">프로세스</span><span className="v">{e.online ? '온라인' : '오프라인'} · pid {e.pid ?? '—'} · WS {e.ws_connected ? '연결' : '끊김'}</span>
                <span className="k">마지막 틱</span><span className="v">{e.last_tick_at ? kdate(e.last_tick_at) : '—'}</span>
                <span className="k">잔고 대조</span><span className="v">{e.reconcile?.status ?? '—'} {e.reconcile?.at ? `· ${kdate(e.reconcile.at)}` : ''}</span>
                <span className="k">일봉 EMA100 가드</span><span className="v">{e.trend ? `${e.trend.ok ? '통과' : '하락 — 매수 중지'} · 종가 ${krw(Number(e.trend.close))} / EMA ${krw(Number(e.trend.ema))}` : '—'}</span>
                <span className="k">Grid 기준가</span><span className="v">{krw(e.anchor)} {e.seeded_at ? `· 시작 ${kdate(e.seeded_at)} @ ${krw(e.seed_price)}` : ''}</span>
                <span className="k">한도</span><span className="v">재고 ≤ {krw(e.limits?.max_inventory_krw)} · 현금 ≥ {krw(e.limits?.min_cash_krw)} · 일손실 -5% 매수중지 · MDD 12% 중단</span>
                <span className="k">오류</span><span className="v">{e.errors ?? 0}{e.last_error ? ` · ${e.last_error}` : ''}</span>
              </div>
            </details>
          )}
        </>
      )}
      <div className="notice warn-exp">실전 실험 단계입니다. Historical Grid v1은 장기 하락장에서 큰 재고 평가손실이 확인됐습니다.</div>
      {onDetail && <div className="btns"><button onClick={onDetail}>ETH 상세 · 체결</button></div>}
    </div>
  );
}
