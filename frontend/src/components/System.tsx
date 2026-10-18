import { useState } from 'react';
import { api, type Health, type Status, type ResearchE1 } from '../api/client';
import { usePoll } from '../hooks';
import { ago, kst, num, krw } from '../format';

function B({ ok, text }: { ok: boolean | null; text: string }) {
  return <span className={`badge ${ok == null ? 'info' : ok ? 'ok' : 'bad'}`}>{text}</span>;
}

export function System({ status, health }: { status: Status | null; health: Health | null }) {
  const events = usePoll(api.events, 10000);
  const commands = usePoll(api.commands, 10000);
  const [token, setToken] = useState(() => { try { return sessionStorage.getItem('ctl') ?? ''; } catch { return ''; } });
  const [msg, setMsg] = useState<string | null>(null);
  const send = async (type: 'PAUSE' | 'RESUME' | 'KILL') => {
    const phrase = window.prompt(`Type ${type} to confirm. This is written to the audit log.`);
    if (phrase !== type) { setMsg('cancelled'); return; }
    try { sessionStorage.setItem('ctl', token); } catch { /* ignore */ }
    try { const r = await api.control(type, token); setMsg(`${type} queued: ${r.command_id} (expires ${kst(r.expires_at)})`); commands.refresh(); }
    catch (e) { setMsg(`failed: ${e instanceof Error ? e.message : String(e)}`); }
  };
  const ts = health?.tailscale;
  const e1 = usePoll(api.researchE1, 30000).data;
  return (
    <>
      <ResearchE1Card d={e1} />
      <div className="section"><h2>System health</h2>
        <div className="grid">
          <div className="card"><div className="k">Trader</div><div className="v"><B ok={health?.trader === 'ONLINE'} text={health?.trader ?? '…'} /></div><div className="s">heartbeat {ago(health?.heartbeat_age_sec)} ago · pid {status?.pid ?? '—'}</div></div>
          <div className="card"><div className="k">Mode / State</div><div className="v">{status?.mode} · {status?.state}</div><div className="s">{status?.state_reason} · since {kst(status?.state_since)}</div></div>
          <div className="card"><div className="k">Uptime</div><div className="v">{ago(health?.uptime_sec)}</div><div className="s">run {status?.run_id ?? '—'}</div></div>
          <div className="card"><div className="k">Upbit WebSocket</div><div className="v"><B ok={health?.websocket === 'CONNECTED'} text={health?.websocket ?? '…'} /></div><div className="s">reconnects {health?.reconnect_count ?? '—'} · msgs {health?.ws_messages ?? '—'}</div></div>
          <div className="card"><div className="k">Upbit REST</div><div className="v"><B ok={health?.rest === 'OK'} text={health?.rest ?? '…'} /></div><div className="s">latency {num(health?.api_latency_ms, 0)}ms · errors {health?.api_errors ?? '—'}</div></div>
          <div className="card"><div className="k">Database</div><div className="v"><B ok={health?.database === 'OK'} text={health?.database ?? '…'} /></div></div>
          <div className="card"><div className="k">Tailscale</div><div className="v"><B ok={ts ? ts.state === 'Running' : null} text={ts?.state ?? '…'} /></div><div className="s">{ts?.dns_name ?? (ts?.detail ?? '')}</div></div>
          <div className="card"><div className="k">Market data</div><div className="v">{ago(health?.market_data_age_sec)} ago</div><div className="s">{kst(health?.last_market_data)} · ₩{health?.market_price?.toLocaleString() ?? '—'}</div></div>
          <div className="card"><div className="k">Reconciliation</div><div className="v"><B ok={health?.reconciliation.status === 'OK'} text={health?.reconciliation.status ?? '…'} /></div><div className="s">{kst(health?.reconciliation.at)} · Δ₩{health?.reconciliation.krw_diff ?? '—'} Δ{health?.reconciliation.btc_diff ?? '—'} BTC</div></div>
          <div className="card"><div className="k">Power / sleep</div><div className="v"><B ok={health?.power ? health.power.warnings.length === 0 : null} text={health?.power ? (health.power.ac_power ? 'AC' : 'BATTERY') + (health.power.sleep_prevented ? ' · no-sleep' : ' · sleep!') : '…'} /></div><div className="s">{health?.power?.warnings.join(' · ') || `battery ${health?.power?.battery_pct ?? '—'}% · display sleep ${health?.power?.display_sleep_min ?? '—'}m`}</div></div>
          <div className="card"><div className="k">Kill switch</div><div className="v">{status?.state === 'HALTED' ? <span className="neg">ACTIVE</span> : <span className="pos">armed</span>}</div><div className="s">{status?.kill_switch?.reason ? String(status.kill_switch.reason) : ''}</div></div>
        </div>
      </div>
      <div className="section"><h2>Control (audited)</h2>
        <div className="panel">
          <input type="password" placeholder="control token (DASHBOARD_CONTROL_TOKEN)" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
          <div className="controls">
            <button onClick={() => send('PAUSE')}>Pause</button>
            <button onClick={() => send('RESUME')}>Resume</button>
            <button className="kill" onClick={() => send('KILL')}>KILL</button>
          </div>
          <div className="muted" style={{ marginTop: 6 }}>{msg ?? 'Commands are queued to the trader with a TTL; the trader acknowledges and executes them.'}</div>
        </div>
      </div>
      <div className="section"><h2>Recent commands</h2>
        <div className="panel tablewrap"><table><thead><tr><th>Requested</th><th>Type</th><th>Status</th><th>By</th><th>Executed</th><th>Note</th></tr></thead>
          <tbody>{(commands.data ?? []).map((c) => <tr key={c.command_id}><td>{kst(c.requested_at)}</td><td>{c.type}</td><td className={c.status === 'EXECUTED' ? 'pos' : c.status === 'PENDING' ? '' : 'neg'}>{c.status}</td><td>{c.requested_by}</td><td>{kst(c.executed_at)}</td><td>{c.failure_reason ?? c.result ?? ''}</td></tr>)}
          {!commands.data?.length && <tr><td colSpan={6} className="muted">none</td></tr>}</tbody></table></div>
      </div>
      <div className="section"><h2>Event log</h2>
        <div className="panel tablewrap"><table><thead><tr><th>Time</th><th>Level</th><th>Type</th><th>Message</th></tr></thead>
          <tbody>{(events.data ?? []).map((e) => <tr key={e.id}><td>{kst(e.ts)}</td><td className={`lvl-${e.level}`}>{e.level}</td><td>{e.type}</td><td style={{ whiteSpace: 'normal' }}>{e.message}</td></tr>)}</tbody></table></div>
      </div>
    </>
  );
}


function ResearchE1Card({ d }: { d: ResearchE1 | null }) {
  if (!d || d.status === 'NOT_INITIALIZED') return null;
  const b = d.books;
  const ob = d.observation_only?.[0];
  return (
    <div className="section"><h2>D0 v2 E1 Research — 연구용 가상 거래 (실제 주문 없음)</h2>
      <div className="panel">
        <div className="row" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}>
          <span className="badge warn">SHADOW</span>
          <span className="badge info">{d.candidate_status ?? 'UNDER_VALIDATION'}</span>
          <span className="muted">forward since {kst(d.forward_since)} · runner pid {d.runner?.pid ?? '—'} · ticks {d.runner?.ticks ?? '—'} · WS {d.runner?.ws_connected ? 'connected' : 'down'} · errors {d.runner?.errors ?? '—'}</span>
        </div>
        <div className="grid">
          <div className="card"><div className="k">Matched entries</div><div className="v">{d.matched_entries ?? 0}</div><div className="s">primary sample (LIVE 확정 BUY 기준)</div></div>
          <div className="card"><div className="k">S0 / S1 completed</div><div className="v">{b?.S0.completed ?? 0} / {b?.S1.completed ?? 0}</div><div className="s">open {b?.S0.open ?? 0} / {b?.S1.open ?? 0}</div></div>
          <div className="card"><div className="k">BE activated / BE exits</div><div className="v">{b?.S1.be_activated ?? 0} / {b?.S1.be_exits ?? 0}</div></div>
          <div className="card"><div className="k">+1R→Loss S0 / S1</div><div className="v">{b?.S0.plus_1r_then_loss ?? 0} / {b?.S1.plus_1r_then_loss ?? 0}</div><div className="s">meaningful reversals {b?.S0.meaningful_reversals ?? 0} / {b?.S1.meaningful_reversals ?? 0}</div></div>
          <div className="card"><div className="k">S0 equity</div><div className="v">{krw(b?.S0.equity)}</div></div>
          <div className="card"><div className="k">S1 equity</div><div className="v">{krw(b?.S1.equity)}</div></div>
        </div>
        {ob && <div className="muted" style={{ marginTop: 8 }}>현재 LIVE 거래는 OBSERVATION_ONLY (primary 제외): entry {ob.entry_fill} · initial stop {ob.initial_stop ?? '—'} · R {ob.r_unit ?? '—'} · 가상 +1R {ob.hypothetical_1r_trigger ?? '—'} · 가상 BE stop {ob.hypothetical_be_stop ?? '—'} · 현재 MFE {ob.current_mfe_r ? Number(ob.current_mfe_r).toFixed(2) + 'R' : '—'}</div>}
        <div className="muted" style={{ marginTop: 4 }}>{d.note} · BE: {d.be_formula}</div>
      </div>
    </div>
  );
}
