import type { Portfolio, Status, Sleeves, ResearchE1, EthGrid, Activity, Overview } from '../api/client';
import { api } from '../api/client';
import { usePoll } from '../hooks';
import { krw, pct, btc, kdate, ktime, kdur, skrw, sign } from '../format';
import { nowState } from '../view';

export const Badge = ({ kind }: { kind: 'LIVE' | 'LIVE_EXPERIMENT' | 'SHADOW' | 'RESEARCH_FAILED' | 'PLANNED' | 'RESEARCH' }) => {
  const map = { LIVE: ['bad', 'LIVE · 실제 돈'], LIVE_EXPERIMENT: ['bad exp', 'LIVE EXPERIMENT · 실제 돈'], SHADOW: ['warn', 'SHADOW · 연구용 가상 거래'], RESEARCH_FAILED: ['failed', 'RESEARCH FAILED · 연구 종료'], PLANNED: ['info', 'PLANNED · 미시작'], RESEARCH: ['info', 'RESEARCH'] } as const;
  const [cls, label] = map[kind];
  return <span className={`badge ${cls}`}>{label}</span>;
};

const Big = ({ k, v, s, cls }: { k: string; v: string; s?: string; cls?: string }) => (
  <div className="big"><div className="k">{k}</div><div className={`v ${cls ?? ''}`}>{v}</div>{s && <div className="s">{s}</div>}</div>
);

export function Hero({ status, pf, sleeves, ov }: { status: Status | null; pf: Portfolio | null; sleeves: Sleeves | null; ov: Overview | null }) {
  const st = status?.state;
  const health = !status || !status.online ? ['bad', '오프라인'] : st === 'RUNNING' ? ['ok', 'LIVE 정상'] : st === 'PAUSED' || st === 'MAINTENANCE' ? ['warn', '주의 — ' + (st === 'PAUSED' ? '일시정지' : '점검/복구 중')] : ['bad', '정지'];
  const total = ov?.total_equity ?? pf?.equity ?? null;
  const start = ov?.challenge_start ?? status?.challenge.live_start_krw ?? pf?.initial_equity ?? null;
  const cum = ov ? ov.total_pnl : total != null && start != null ? total - start : null;
  const todayPnl = ov?.today_pnl ?? pf?.today_pnl ?? null;
  const g = sleeves?.global;
  const btcV = ov?.btc_value ?? g?.btc_value ?? pf?.btc_value ?? 0;
  const ethV = ov?.eth_value ?? g?.eth_value ?? 0;
  const d0Cash = pf?.krw ?? 0;
  const ethCash = ov?.eth.cash ?? 0;
  const totalKrw = ov?.total_krw ?? d0Cash + ethCash;
  const denom = Math.max(1, btcV + ethV + totalKrw);
  const ethStarted = !!ov?.eth.started || !!ov?.eth.carved;
  return (
    <div className="hero2">
      <div className="hero-top"><span>전체 자산</span><span className={`badge ${health[0]}`}>{health[1]}</span></div>
      <div className="hero-total">{krw(total)}</div>
      <div className="hero-row">
        <span className={sign(todayPnl)}>오늘 {skrw(todayPnl)}</span>
        <span className={sign(cum)}>전체 손익 {skrw(cum)} ({start ? pct(((cum ?? 0) / start) * 100) : '—'})</span>
      </div>
      <div className="alloc" title="자산 분배">
        <div className="alloc-btc" style={{ width: `${(btcV / denom) * 100}%` }} />
        <div className="alloc-d0cash" style={{ width: `${(d0Cash / denom) * 100}%` }} />
        <div className="alloc-ethv" style={{ width: `${(ethV / denom) * 100}%` }} />
        <div className="alloc-eth" style={{ width: `${(ethCash / denom) * 100}%` }} />
      </div>
      <div className="alloc-legend">
        <span><i className="dot btc" />BTC D0 {krw(ov?.d0.equity ?? btcV + d0Cash)} <small className={sign(ov?.d0.pnl)}>({skrw(ov?.d0.pnl)})</small></span>
        <span><i className="dot ethv" />ETH Grid {krw(ov?.eth.equity ?? ethCash)} <small className={sign(ov?.eth.pnl)}>({ethStarted ? skrw(ov?.eth.pnl) : '미시작'})</small></span>
        <span><i className="dot cash" />총 KRW {krw(totalKrw)}</span>
      </div>
      <div className="muted" style={{ marginTop: 6 }}>BTC D0 손익과 ETH Grid 손익은 별도 집계 · ETH Grid는 LIVE EXPERIMENT(실제 돈, ₩50,000 한도)</div>
    </div>
  );
}

export function BtcCard({ pf, onTrades, onDetail }: { pf: Portfolio | null; onTrades: () => void; onDetail: () => void }) {
  const d = usePoll(api.decision, 10000).data;
  const s = nowState(pf, d, [], []);
  const holding = s.kind === 'HOLDING' ? s : null;
  const waiting = s.kind === 'WAITING' ? s : null;
  const nextEval = d?.next_evaluation ?? null;
  const upper = d?.donchian_upper ?? null;
  const price = pf?.btc_price ?? d?.current_price ?? null;
  const dist = upper != null && price ? ((upper - price) / price) * 100 : null;
  const stop = pf?.stop_price ?? null;
  const stopDist = stop != null && price ? ((stop - price) / price) * 100 : null;
  return (
    <div className="scard">
      <div className="scard-head"><strong>BTC 추세 전략</strong><Badge kind="LIVE" /></div>
      {holding ? (
        <>
          <div className="grid2">
            <Big k="현재 BTC" v={btc(holding.qty)} />
            <Big k="매수가" v={krw(holding.avg)} />
            <Big k="현재가" v={krw(holding.price)} />
            <Big k="현재 손익" v={`${skrw(holding.pnl)} (${pct(holding.pnlPct)})`} cls={sign(holding.pnl)} />
            <Big k="자동 매도 기준" v={krw(stop)} s={stopDist != null ? `현재가 대비 ${pct(stopDist)}` : undefined} />
            <Big k="다음 판단" v={nextEval ? ktime(nextEval) : '—'} s={nextEval ? kdate(nextEval) : undefined} />
          </div>
          <div className="row-between"><span className="muted">상태</span><strong>보유 중 · {kdur(holding.heldSec)}</strong></div>
          <details className="acc"><summary>언제 팔까?</summary>
            <ul className="plain">
              <li>현재 자동 매도선 <strong>{krw(stop)}</strong> (현재가와 거리 {pct(stopDist)}) — 가격이 닿으면 즉시 자동 매도</li>
              <li>4시간봉 마감 시 최근 가격 범위(20봉 저점 {krw(d?.donchian_lower)}) 아래로 떨어지면 매도</li>
              <li>일봉 종가가 EMA100({krw(d?.daily_ema)}) 아래로 가면(상승 추세 종료) 매도</li>
            </ul>
          </details>
        </>
      ) : (
        <>
          <div className="grid2">
            <Big k="현재가" v={krw(waiting?.price ?? price)} />
            <Big k="매수 기준" v={krw(upper)} s="최근 5일 고점" />
            <Big k="매수하려면" v={dist == null ? '—' : dist > 0 ? `${pct(dist)} 상승 필요` : '기준 도달 — 마감 확인'} />
            <Big k="다음 판단" v={nextEval ? ktime(nextEval) : '—'} s={nextEval ? kdate(nextEval) : undefined} />
          </div>
          <div className="row-between"><span className="muted">상태</span><strong>{s.kind === 'CLOSED_RECENTLY' ? '최근 매도 완료 · 매수 대기' : '매수 대기 중'}</strong></div>
        </>
      )}
      <details className="acc"><summary>언제 살까?</summary>
        <ul className="plain">
          <li>4시간봉 종가가 최근 5일 고점({krw(upper)})을 넘으면 매수 — 현재 거리 {pct(dist)}</li>
          <li>단, 일봉 종가가 EMA100 위(상승 추세)일 때만 — 현재 {d?.trend_ok == null ? '—' : d.trend_ok ? '✅ 통과' : '❌ 미충족'}</li>
          <li>매수 금액은 전략 배정의 30% (현재 포지션 종료 후 다음 매수부터 최대 ₩15,000)</li>
        </ul>
      </details>
      <div className="btns"><button onClick={onTrades}>거래 내역</button><button onClick={onDetail}>상세 보기</button></div>
    </div>
  );
}

export function E1Card({ e1 }: { e1: ResearchE1 | null }) {
  const b = e1?.books;
  return (
    <div className="scard">
      <div className="scard-head"><strong>BTC 매도 개선 연구 (E1)</strong><Badge kind="SHADOW" /></div>
      <div className="grid2">
        <Big k="연구 규칙" v="+1R 도달 시 본전 보호" s="매수 후 1R 수익이 나면 손절선을 본전(수수료 포함)으로 올림" />
        <Big k="Primary 거래" v={`${e1?.matched_entries ?? 0}건`} s={`S0 ${b?.S0.completed ?? 0} / S1 ${b?.S1.completed ?? 0} 완료`} />
        <Big k="+1R 이벤트" v={`${b?.S1.be_activated ?? 0}건`} s={`BE 매도 ${b?.S1.be_exits ?? 0}`} />
        <Big k="상태" v={e1?.status === 'SHADOW' ? (e1.matched_entries ? '관찰 중' : '다음 신규 D0 매수 대기') : '미시작'} s={e1?.runner ? `runner ${e1.runner.ws_connected ? '연결됨' : '끊김'} · 오류 ${e1.runner.errors}` : undefined} />
      </div>
      <div className="muted">실제 BTC 매매에는 영향 없음 · 현재 LIVE 거래는 관찰 전용(통계 제외)</div>
    </div>
  );
}

export function EthCard({ g, onDetail }: { g: EthGrid | null; onDetail: () => void }) {
  const h = g?.historical;
  const failed = g?.display_status === 'RESEARCH_FAILED' || h?.status === 'FAILED';
  return (
    <div className="scard">
      <div className="scard-head"><strong>ETH 반복매매 (Grid)</strong>{failed ? <Badge kind="RESEARCH_FAILED" /> : g?.display_status === 'SHADOW' ? <Badge kind="SHADOW" /> : <Badge kind="PLANNED" />}</div>
      <div className="grid2">
        <Big k="Historical Result" v={failed ? 'FAILED' : (h?.status ?? '—')} cls={failed ? 'neg' : ''} s="과거 9년 시뮬레이션" />
        <Big k="실제 ETH / 실제 주문" v={`${h?.actual_eth ?? 0} / ${h?.actual_orders ?? 0}`} s="실제 ETH 매매 없음" />
        <Big k="최대 MDD" v={h?.mdd_range_pct ? `${h.mdd_range_pct[0].toFixed(0)}~${h.mdd_range_pct[1].toFixed(0)}%` : '—'} s="기준 12% 초과" cls="neg" />
        <Big k="최근 2Y 최악 MDD" v={h?.recent_2y_worst_mdd_pct != null ? `약 ${h.recent_2y_worst_mdd_pct.toFixed(0)}%` : '—'} cls="neg" />
      </div>
      <div className="row-between"><span className="muted">실패 이유</span><strong>하락장에서 ETH 재고 평가손실 증가</strong></div>
      <div className="row-between"><span className="muted">연구 결론</span><strong>Gate 미통과 (연구 결과는 변경되지 않음)</strong></div>
      <div className="notice">이 연구 결과와 별개로, 사용자 승인 하에 동일 G0 전략의 ₩50,000 LIVE EXPERIMENT가 거래 탭에서 실제 돈으로 운용 중입니다. 실험 시작이 연구 실패를 PASS로 바꾸지 않습니다.</div>
      <div className="btns"><button onClick={onDetail}>연구 상세</button></div>
    </div>
  );
}

export function FundsCard({ s, ov }: { s: Sleeves | null; ov: Overview | null }) {
  const d0 = s?.sleeves.BTC_D0;
  const n = (v: string | number | null | undefined) => (v == null ? null : Number(v));
  const used = n(d0?.position_value) ?? 0;
  const alloc = n(d0?.allocated_capital) ?? 50000;
  const ethAlloc = n(s?.sleeves.ETH_GRID?.allocated_capital) ?? 50000;
  const carved = !!ov?.eth.carved;
  return (
    <div className="scard">
      <div className="scard-head"><strong>전략별 자금</strong></div>
      <div className="grid2">
        <Big k="BTC D0 배정" v={krw(alloc)} s={`현재 BTC ${krw(used)} · D0 현금 ${krw(s?.global ? n(d0?.cash_actual) : null)}`} />
        <Big k="ETH Grid 배정" v={krw(ethAlloc)} s={carved ? `ETH ${krw(ov?.eth.value)} · KRW ${krw(ov?.eth.cash)}` : '분리 대기 (재시작 전)'} />
        <Big k="총 KRW (실제 지갑)" v={krw(s?.global.actual_krw)} s={`BTC D0 ${krw(n(d0?.cash_actual))} + ETH Grid ${krw(ov?.eth.cash)}`} />
        <Big k="다음 D0 매수 한도" v={krw(n(d0?.next_max_entry_krw))} s={`${d0?.next_entry_estimate_krw ? `현재 자산 기준 예상 ${krw(n(d0.next_entry_estimate_krw))}` : ''}${d0?.grandfathered_position ? ' · 현재 포지션은 기존 정책 유지(축소 없음)' : ''}`} />
      </div>
      <div className="notice">{carved ? 'BTC D0 ₩50,000 · ETH Grid ₩50,000 sleeve 적용 중. 두 전략의 현금은 서로 침범하지 않습니다 (ETH Grid 최소 KRW ₩10,000 · 최대 ETH ₩40,000).' : '새 자금 한도(₩50,000 sleeve · 다음 매수 최대 ₩15,000)는 다음 안전 재시작 후 적용됩니다.'}</div>
    </div>
  );
}

export function Timeline({ pf, showRaw }: { pf: Portfolio | null; showRaw: boolean }) {
  const { data } = usePoll(api.activity, 15000);
  const items = (data ?? []) as Activity[];
  return (
    <div className="scard">
      <div className="scard-head"><strong>최근 활동</strong></div>
      <div className="tl">
        {pf?.has_position && (
          <div className="tl-item now"><div className="tl-time">현재</div><div><div className="tl-title">BTC 보유 중</div><div className="tl-detail">{btc(pf.btc)} · 손익 <span className={sign(pf.unrealized_pnl)}>{skrw(pf.unrealized_pnl)}</span> · 자동 매도선 {krw(pf.stop_price)}</div></div></div>
        )}
        {items.map((a, i) => (
          <div className={`tl-item k-${a.kind} ${a.sleeve === 'ETH_GRID' ? 'eth' : ''}`} key={`${a.ts}-${i}`}><div className="tl-time">{kdate(a.ts)}</div><div><div className="tl-title">{a.sleeve === 'ETH_GRID' && <span className="tag">ETH</span>}{a.title}</div><div className="tl-detail">{a.detail}</div>{showRaw && <div className="muted">{a.raw}</div>}</div></div>
        ))}
        {!items.length && !pf?.has_position && <div className="muted">아직 활동이 없습니다.</div>}
      </div>
    </div>
  );
}
