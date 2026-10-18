export const krw = (v: number | null | undefined, digits = 0) =>
  v == null || Number.isNaN(v) ? '—' : `₩${v.toLocaleString('ko-KR', { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
export const pct = (v: number | null | undefined, digits = 2) => (v == null || Number.isNaN(v) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`);
export const num = (v: number | null | undefined, digits = 2) => (v == null || Number.isNaN(v) ? '—' : v.toFixed(digits));
export const ago = (sec: number | null | undefined) => {
  if (sec == null) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  return `${Math.floor(sec / 86400)}d ${Math.floor((sec % 86400) / 3600)}h`;
};
export const kst = (iso: string | null | undefined) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
};
export const sign = (v: number | null | undefined) => (v == null ? '' : v > 0 ? 'pos' : v < 0 ? 'neg' : '');

/** Drawdown: 0 -> "0.00%" (neutral), otherwise "-X.XX%" (negative). Input is a positive magnitude in %. */
export const dd = (v: number | null | undefined, digits = 2) => {
  if (v == null || Number.isNaN(v)) return '—';
  const m = Math.abs(v);
  return m < 0.005 ? `${(0).toFixed(digits)}%` : `-${m.toFixed(digits)}%`;
};
export const ddClass = (v: number | null | undefined) => (v == null || Math.abs(v) < 0.005 ? '' : 'neg');
/** BTC quantity: 0 -> "0 BTC"; otherwise up to 8 decimals without trailing zeros. */
export const btc = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—';
  if (v === 0) return '0 BTC';
  return `${v.toFixed(8).replace(/\.?0+$/, '')} BTC`;
};
/** Human-readable label for the top header; raw enum values stay visible in System / Event Log. */
export const humanReason = (_state: string | null | undefined, reason: string | null | undefined) => {
  if (!reason) return '';
  const map: Record<string, string> = {
    RECOVERY_CHECKS_PASSED: 'Healthy',
    STARTUP: 'Starting up',
    MANUAL_PAUSE: 'Paused by operator',
    MANUAL_RESUME: 'Resuming — running checks',
    EXCHANGE_OUTAGE: 'Exchange outage — waiting',
    MANUAL_KILL: 'Stopped by operator',
    DAILY_LOSS_LIMIT: 'Daily loss limit hit',
    MAX_DRAWDOWN: 'Max drawdown hit',
    RECONCILE_FAILED: 'Balance mismatch',
    RECONCILE_FAILED_DURING_RECOVERY: 'Balance mismatch',
    UNEXPLAINED_STALE: 'Market data lost',
    UNEXPLAINED_STALE_LOCAL_FAULT: 'Market data feed lost',
    API_ERRORS: 'Exchange API errors',
    DATABASE_FAILURE: 'Database failure',
    ORDER_UNKNOWN_UNRESOLVED: 'Order state unknown',
  };
  if (map[reason]) return map[reason];
  return reason.toLowerCase().replace(/_/g, ' ');
};

/** "09월 30일 01:00" (KST) */
export const kdate = (iso: string | null | undefined) => {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(d);
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? '';
  return `${g('month')}월 ${g('day')}일 ${g('hour')}:${g('minute')}`;
};
/** "09/30 05:01" (KST) */
export const kshort = (iso: string | null | undefined) => {
  if (!iso) return '—';
  const p = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(new Date(iso));
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? '';
  return `${g('month')}/${g('day')} ${g('hour')}:${g('minute')}`;
};
/** KST time only "05:01" */
export const ktime = (iso: string | null | undefined) => {
  if (!iso) return '—';
  const p = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(new Date(iso));
  const g = (t: string) => p.find((x) => x.type === t)?.value ?? '';
  return `${g('hour')}:${g('minute')}`;
};
/** Korean duration: "3시간 21분", "2일 4시간", "45분" */
export const kdur = (sec: number | null | undefined) => {
  if (sec == null || Number.isNaN(sec) || sec < 0) return '—';
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}분`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 ${m % 60}분`;
  const d = Math.floor(h / 24);
  return `${d}일 ${h % 24}시간`;
};
/** signed KRW "+₩428" / "-₩205" / "₩0" */
export const skrw = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—';
  const r = Math.round(v);
  return `${r > 0 ? '+' : r < 0 ? '-' : ''}₩${Math.abs(r).toLocaleString('ko-KR')}`;
};
