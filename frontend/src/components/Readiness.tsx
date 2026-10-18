import { api } from '../api/client';
import { usePoll } from '../hooks';

const gcls = (s: string) => (s === 'PASS' ? 'ok' : s === 'FAIL' || s === 'BLOCKED' ? 'bad' : 'warn');
// Display names: 'Strategy PAPER' is the pre-LIVE validation evidence, not the current mode.
const GROUP_LABEL: Record<string, string> = { Research: 'Research (연구 승인)', Infrastructure: 'Infrastructure (인프라)', 'Private API': 'Private API (거래소 연결)', 'Strategy PAPER': '사전 검증 기록 (PAPER Validation Evidence)', LIVE: 'LIVE 승인 상태' };

export function Readiness() {
  const { data } = usePoll(api.readiness, 30000);
  if (!data) return null;
  return (
    <div className="section"><h2>LIVE readiness (승격 검증 기록 — 현재 모드와 별개)</h2>
      {data.groups.map((g) => (
        <div className="panel" key={g.name} style={{ marginBottom: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
            <strong>{GROUP_LABEL[g.name] ?? g.name}</strong><span className={`badge ${gcls(g.status)}`}>{g.status}</span>
          </div>
          <div className="ready">
            {g.items.map((i) => <><span key={g.name + i.name + 'n'} title={i.detail}>{i.name}</span><span key={g.name + i.name + 'v'} className="muted" title={i.detail}>{i.value}</span><span key={g.name + i.name + 's'} className={`st ${i.status}`}>{i.status}</span></>)}
          </div>
        </div>
      ))}
      <div className={`live ${data.live_allowed ? 'allowed' : 'blocked'}`}>LIVE {data.live_allowed ? 'ALLOWED' : 'BLOCKED'}{data.block_reason ? ` — ${data.block_reason}` : ''}</div>
    </div>
  );
}
