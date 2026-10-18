import type { Portfolio, Status } from '../api/client';
import { krw, pct, ago, sign, dd, ddClass, humanReason, kst } from '../format';

const stateBadge = (s: string) => (s === 'RUNNING' ? 'ok' : s === 'PAUSED' || s === 'MAINTENANCE' ? 'warn' : s === 'HALTED' ? 'bad' : 'info');

export function Header({ status, pf, mdd }: { status: Status | null; pf: Portfolio | null; mdd: number | null }) {
  const st = status?.state ?? '…';
  const online = status?.online ?? false;
  const p = status?.challenge.progress_pct ?? 0;
  const elapsed = status?.uptime_sec ?? null;
  return (
    <div className={`hero ${status && !online ? 'offline' : ''}`}>
      <h1>₩100K → ₩1M 자동매매 챌린지</h1>
      <div className="equity">{krw(pf?.equity)}</div>
      <div className="row">
        <span className={sign(pf?.total_return_pct)} style={{ fontWeight: 700 }}>누적 {pct(pf?.total_return_pct)}</span>
        <span className={sign(pf?.today_pnl)}>오늘 {krw(pf?.today_pnl)} ({pct(pf?.today_pnl_pct)})</span>
        <span className={ddClass(mdd)}>최대 낙폭 {dd(mdd)}</span>
      </div>
      <div className="progress"><div style={{ width: `${Math.max(0, Math.min(100, p))}%` }} /></div>
      <div className="progress-labels">
        <span>시작 {krw(status?.challenge.live_start_krw ?? pf?.initial_equity)}</span>
        <span>{krw(pf?.equity)} / ₩1,000,000 ({p.toFixed(1)}%{(status?.challenge.raw_pct ?? 0) < 0 ? ', 시작 금액 아래' : ''})</span>
        <span>목표</span>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <span className={`badge ${status?.mode === 'LIVE' ? 'bad' : 'info'}`} style={status?.mode === 'LIVE' ? { fontWeight: 800 } : undefined}>{status?.mode === 'LIVE' ? 'LIVE · 실제 자금' : status?.mode === 'PAPER' ? 'PAPER · 모의' : (status?.mode ?? '…')}</span>
        {status?.execution_mode && <span className="badge info">{status.execution_mode}</span>}
        <span className={`badge ${online ? 'ok' : 'bad'}`}>{online ? '연결됨' : '오프라인'}</span>
        <span className={`badge ${stateBadge(st)}`}>{st === 'RUNNING' ? '자동매매 실행 중' : st === 'PAUSED' ? '일시정지 (매도만)' : st === 'MAINTENANCE' ? '점검/복구 중' : st === 'HALTED' ? '정지됨' : st}</span>
        {status?.state_reason && <span className="muted">{humanReason(status.state, status.state_reason)}</span>}
        <span className="muted">가동 {ago(elapsed)}</span>
      </div>
      {status?.mode === 'LIVE' && (
        <div className="row muted" style={{ marginTop: 6, fontSize: 12 }}>
          <span>LIVE 시작 {status.challenge.live_activated_at ? kst(status.challenge.live_activated_at) : '—'}</span>
          <span>{status.challenge.challenge_started_at ? `챌린지 시작 ${kst(status.challenge.challenge_started_at)}` : '아직 첫 거래 전입니다'}</span>
          <span className={sign(status.challenge.challenge_pnl)}>챌린지 손익 {krw(status.challenge.challenge_pnl)}</span>
          {status.challenge.external_flows ? <span className="neg">외부 입출금 감지 {status.challenge.external_flows}</span> : null}
        </div>
      )}
    </div>
  );
}
