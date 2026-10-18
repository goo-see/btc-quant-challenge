import { useState } from 'react';
import { api } from './api/client';
import { usePoll } from './hooks';
import { HaltedWarning } from './components/HaltedWarning';
import { Trading } from './components/Trading';
import { System } from './components/System';
import { Readiness } from './components/Readiness';
import { Hero, BtcCard, E1Card, EthCard, FundsCard, Timeline } from './components/Home';
import { EthLiveCard, CompoundingSection } from './components/EthLive';
import { EthResearch, ResearchStatus } from './components/Research';
import { krw, skrw, sign } from './format';

type Tab = 'home' | 'trading' | 'research' | 'system';

export default function App() {
  const [tab, setTab] = useState<Tab>('home');
  const [showRaw, setShowRaw] = useState(false);
  const status = usePoll(api.status, 5000);
  const pf = usePoll(api.portfolio, 5000);
  const stats = usePoll(api.stats, 15000);
  const health = usePoll(api.health, 5000);
  const sleeves = usePoll(api.sleeves, 15000);
  const e1 = usePoll(api.researchE1, 30000);
  const eth = usePoll(api.ethGrid, 60000);
  const ethLive = usePoll(api.ethLive, 5000);
  const overview = usePoll(api.overview, 5000);
  const st = status.data;
  const ov = overview.data;
  const online = st?.online ?? false;
  const stateCls = !st || !online ? 'bad' : st.state === 'RUNNING' ? 'ok' : st.state === 'HALTED' ? 'bad' : 'warn';
  const stateLabel = !st ? '…' : !online ? '오프라인' : st.state === 'RUNNING' ? '정상' : st.state === 'PAUSED' ? '일시정지' : st.state === 'MAINTENANCE' ? '점검 중' : '정지';
  return (
    <div className="app">
      <div className="statusbar">
        <span className={`badge ${st?.mode === 'LIVE' ? 'bad' : 'info'}`}>{st?.mode === 'LIVE' ? 'LIVE · 실제 돈' : st?.mode ?? '…'}</span>
        <span className={`badge ${stateCls}`}>{stateLabel}</span>
        <span className="sb-equity">{krw(ov?.total_equity ?? pf.data?.equity)}</span>
        <span className={`sb-pnl ${sign(ov?.today_pnl ?? pf.data?.today_pnl)}`}>오늘 {skrw(ov?.today_pnl ?? pf.data?.today_pnl)}</span>
      </div>
      {status.error && <div className="alert">API unreachable: {status.error}</div>}
      <HaltedWarning status={st} />
      {health.data?.power?.warnings?.length ? <div className="alert">Power policy: {health.data.power.warnings.join(' · ')}</div> : null}
      {st && !online && <div className="alert">트레이더 응답 없음 ({Math.round(st.heartbeat_age_sec ?? 0)}s) — 자동매매 프로세스가 오프라인입니다.</div>}

      {tab === 'home' && (
        <>
          <Hero status={st} pf={pf.data} sleeves={sleeves.data} ov={ov} />
          <div className="grid-cards">
            <BtcCard pf={pf.data} onTrades={() => setTab('trading')} onDetail={() => { setShowRaw(true); setTab('trading'); }} />
            <EthLiveCard e={ethLive.data} onDetail={() => setTab('trading')} />
            <E1Card e1={e1.data} />
            <FundsCard s={sleeves.data} ov={ov} />
            <Timeline pf={pf.data} showRaw={showRaw} />
          </div>
        </>
      )}
      {tab === 'trading' && (
        <>
          <div className="section"><h2>ETH 반복매매 — LIVE EXPERIMENT · 실제 돈</h2><EthLiveCard e={ethLive.data} full />{ethLive.data && ethLive.data.status !== 'NOT_STARTED' && <div style={{ marginTop: 10 }}><CompoundingSection e={ethLive.data} /></div>}</div>
          <div className="section"><h2>BTC 추세 전략 — LIVE · 실제 돈</h2></div>
          <Trading pf={pf.data} stats={stats.data} status={st} />
        </>
      )}
      {tab === 'research' && (
        <div className="grid-cards research">
          <ResearchStatus e1={e1.data} g={eth.data} />
          <div className="full"><E1Card e1={e1.data} /></div>
          <div className="full"><EthCard g={eth.data} onDetail={() => setTab('trading')} /></div>
          <EthResearch g={eth.data} />
        </div>
      )}
      {tab === 'system' && <><System status={st} health={health.data} /><Readiness /></>}

      <div className="footer">₩1,000,000은 콘텐츠 목표이며 수익을 보장하지 않습니다. LIVE = 실제 돈 · LIVE EXPERIMENT = 연구 Gate 미통과 전략의 소액 실제 실험(₩50,000 한도) · SHADOW = 연구용 가상 거래 · RESEARCH FAILED = 연구 종료. 서버 시각 {st?.server_time ?? ''}</div>
      <nav className="bottomnav">
        {([['home', '홈'], ['trading', '거래'], ['research', '연구'], ['system', '시스템']] as const).map(([k, label]) => (
          <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{label}</button>
        ))}
      </nav>
    </div>
  );
}
