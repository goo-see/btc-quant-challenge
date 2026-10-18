import type { ResearchE1, EthGrid, EthCandidate } from '../api/client';
import { krw, pct, kst } from '../format';
import { Badge } from './Home';

const FAIL_LABEL: Record<string, string> = {
  mdd_le_12: 'MDD 기준(12%) 초과',
  bear_crash_survived: '하락장·급락 구간 재고 위험',
  max_inventory_within_policy: '재고 한도(₩40,000) 초과',
  cash_reserve_violations_0: '현금 reserve 위반',
  total_return_gt_0: '9년 수익 마이너스',
  recent_2y_gt_0: '최근 2년 수익 마이너스',
  recent_6m_gt_0: '최근 6개월 수익 마이너스',
  'pf_ge_1.3': 'Profit factor 1.3 미만',
  cost_2x_gt_0: '수수료 2배 시 적자',
  'fee_ratio_le_0.5': '수수료 비중 과다',
};
const gapLabel = (c: EthCandidate) => (c.gap_pct ? `Fixed Gap ${(Number(c.gap_pct) * 100).toFixed(2)}%` : 'ATR 적응형 간격 (0.75–2.0%)');

function Row({ k, v, cls }: { k: string; v: string; cls?: string }) {
  return <><span className="k">{k}</span><span className={`v ${cls ?? ''}`}>{v}</span></>;
}

function Cand({ c }: { c: EthCandidate }) {
  return (
    <div className={`scard cand ${c.gate_pass ? 'ok' : 'fail'}`}>
      <div className="scard-head"><div><strong style={{ fontSize: 18 }}>{c.key}</strong><div className="muted">{gapLabel(c)}</div></div><span className={`badge ${c.gate_pass ? 'ok' : 'failed'}`}>{c.gate_pass ? 'PASS' : 'FAIL'}</span></div>
      <div className="kv big-kv">
        <Row k="9년 수익" v={pct(c.return_pct, 1)} cls={c.return_pct >= 0 ? 'pos' : 'neg'} />
        <Row k="MDD" v={`${c.mdd_pct.toFixed(1)}%`} cls="neg" />
        <Row k="최근 2년" v={pct(c.recent_2y_return_pct, 1)} cls={c.recent_2y_return_pct >= 0 ? 'pos' : 'neg'} />
        <Row k="최근 2Y MDD" v={`${c.recent_2y_mdd_pct.toFixed(1)}%`} cls="neg" />
        <Row k="최근 6개월" v={pct(c.recent_6m_return_pct, 1)} cls={c.recent_6m_return_pct >= 0 ? 'pos' : 'neg'} />
        <Row k="완료 Cycle" v={String(c.cycles)} />
        <Row k="수수료" v={krw(c.fees)} />
        <Row k="수수료 2배 시 수익" v={pct(c.cost_2x_return_pct, 1)} />
      </div>
      <div className="h3">실패 이유</div>
      <ul className="plain">{c.gate_failed.length ? c.gate_failed.map((f) => <li key={f}>• {FAIL_LABEL[f] ?? f}</li>) : <li>• 없음</li>}</ul>
    </div>
  );
}

export function EthResearch({ g }: { g: EthGrid | null }) {
  const h = g?.historical;
  const p = g?.preview;
  const cands = h?.candidates ?? [];
  const best = cands.length ? Math.max(...cands.map((c) => c.return_pct)) : null;
  const minMdd = cands.length ? Math.min(...cands.map((c) => c.mdd_pct)) : null;
  const best6m = cands.length ? Math.max(...cands.map((c) => c.recent_6m_return_pct)) : null;
  return (
    <>
      <div className="scard full">
        <div className="scard-head"><div><strong style={{ fontSize: 18 }}>ETH Grid v1</strong><div className="muted">{h?.dataset ?? ''}</div></div><Badge kind={h?.status === 'FAILED' ? 'RESEARCH_FAILED' : 'RESEARCH'} /></div>
        <p className="lead">횡보장 수익은 확인됐지만 장기 하락장에서 보유 ETH 평가손실이 커 실전 적용을 중단했습니다.</p>
        <div className="h3">핵심 결과</div>
        <div className="grid2 grid4">
          <div className="big"><div className="k">9년 최고 수익</div><div className="v pos">{best != null ? pct(best, 1) : '—'}</div></div>
          <div className="big"><div className="k">최저 MDD</div><div className="v neg">{minMdd != null ? `${minMdd.toFixed(1)}%` : '—'}</div></div>
          <div className="big"><div className="k">최근 6개월 최고</div><div className="v pos">{best6m != null ? pct(best6m, 1) : '—'}</div></div>
          <div className="big"><div className="k">실제 ETH 주문</div><div className="v">{h?.actual_orders ?? 0}</div></div>
        </div>
      </div>
      <div className="scard full">
        <div className="scard-head"><strong style={{ fontSize: 17 }}>왜 실전 적용하지 않았나요?</strong></div>
        <ul className="plain reasons">
          <li>✅ 횡보장에서는 반복 수익 확인</li>
          <li>✅ 최근 6개월 플러스</li>
          <li>❌ ETH 장기 하락 시 재고 손실 증가</li>
          <li>❌ 최대 낙폭 기준 12% 초과</li>
          <li>❌ 신규 매수를 멈춰도 기존 ETH 손실은 남음</li>
        </ul>
        <div className="notice">결론: Grid 간격보다 재고 보호 구조가 문제</div>
      </div>
      <div className="scard full">
        <div className="scard-head"><strong>Grid 가격 레벨</strong><span className="badge warn">{p?.badge ?? '과거 연구 시뮬레이션'}</span></div>
        {p?.available ? (
          <>
            <div className="muted">ETH 현재가 {krw(p.price)} · 간격 {((p.gap_pct ?? 0) * 100).toFixed(1)}% (연구 후보 G1)</div>
            <div className="ladder">
              {(p.levels ?? []).map((l) => (
                <div key={l.price} className={`rung ${l.side.toLowerCase()}`}><span className="side">{l.side}</span><span className="price">{krw(l.price)}</span></div>
              ))}
            </div>
            <div className="muted">{p.note} — 연구 시뮬레이션 화면입니다. 실제 LIVE EXPERIMENT 레벨은 거래 탭의 ETH 카드에서 확인하세요.</div>
          </>
        ) : <div className="muted">가격 정보를 불러오지 못했습니다 (표시 전용).</div>}
      </div>
      <div className="full"><div className="h3">후보별 결과 (총 sleeve equity 기준)</div></div>
      <div className="cands full">{cands.map((c) => <Cand key={c.key} c={c} />)}</div>
    </>
  );
}

export function ResearchStatus({ e1, g }: { e1: ResearchE1 | null; g: EthGrid | null }) {
  const e1ok = e1?.status === 'SHADOW' && (e1.runner?.errors ?? 0) === 0;
  return (
    <div className="scard full">
      <div className="scard-head"><strong>연구 현황</strong></div>
      <div className="kv wide">
        <Row k="E1 Break-even (BTC 매도 개선)" v={`${e1ok ? '🟢 RUNNING' : '🔴 CHECK'} · SHADOW${e1?.forward_since ? ` · since ${kst(e1.forward_since)}` : ''}`} />
        <Row k="ETH Grid v1 (연구)" v={`${g?.historical?.status === 'FAILED' ? '🔴 FAILED' : g?.display_status === 'SHADOW' ? '🟡 SHADOW' : '⚪ PLANNED'} · 연구 종료`} />
        <Row k="ETH Grid G0 (LIVE EXPERIMENT)" v="🟠 실제 돈 소액 실험 진행 — 거래 탭 참조" />
        <Row k="Next Research" v="🟡 NOT_STARTED · 사용자 승인 필요" />
      </div>
    </div>
  );
}
