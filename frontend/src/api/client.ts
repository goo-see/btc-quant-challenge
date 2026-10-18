export type Status = {
  mode: string; state: string; state_reason: string | null; state_since: string | null; online: boolean;
  heartbeat_age_sec: number | null; run_id: string | null; pid: number | null; uptime_sec: number | null; strategy: string | null;
  kill_switch: Record<string, unknown> | null;
  halted_position_warning: null | {
    title: string; halted_reason: string; halted_since: string; quantity_btc: number; position_value: number;
    entry_price: number | null; current_price: number | null; unrealized_pnl: number; stop_price: number | null; trading_disabled: boolean;
  };
  challenge: {
    initial: number; goal: number; progress_pct: number; raw_pct: number; status?: string; initial_challenge_capital?: number | null;
    live_start_krw?: number | null; live_activated_at?: string | null; challenge_started_at?: string | null; start_date?: string | null;
    challenge_pnl?: number | null; external_flows?: number;
  };
  execution_mode?: string | null;
  server_time: string;
};
export type Execution = {
  id: number; mode: string; strategy_version: string; client_order_id: string; side: 'BUY' | 'SELL'; signal_time: string | null; signal_price: number | null;
  order_sent_at: string | null; reference_price_at_send: number | null; actual_fill_price: number; slippage_bps: number | null; slippage_vs_reference_bps: number | null;
  fee: number; quantity: number; funds: number; fill_count: number; atr: number | null; initial_stop: number | null; stop_distance_from_signal: number | null;
  stop_distance_from_fill: number | null; exit_reason: string | null; exit_trigger_price: number | null; realized_pnl: number | null; return_pct: number | null;
  holding_sec: number | null; filled_at: string;
};
export type Portfolio = {
  initial_equity: number; equity: number; krw: number; btc: number; btc_value: number; btc_price: number | null;
  avg_entry: number | null; entry_time: string | null; stop_price: number | null; realized_pnl: number; unrealized_pnl: number;
  total_pnl: number; total_return_pct: number; today_pnl: number; today_pnl_pct: number; hwm: number; drawdown_pct: number;
  max_drawdown_pct: number; fees_total: number; consecutive_losses: number; has_position: boolean; entry_reason: string;
};
export type Stats = {
  trade_count: number; wins: number; losses: number; win_rate: number; average_win: number; average_loss: number;
  payoff_ratio: number | null; profit_factor: number | null; expectancy: number; fees_paid: number; realized_today: number; realized_7d: number;
};
export type Trade = { id: number; entry_time: string; exit_time: string | null; entry_price: number; exit_price: number | null; quantity: number; amount: number; fees: number; net_pnl: number | null; return_pct: number | null; holding_sec: number | null; strategy: string; entry_reason: string; exit_reason: string | null };
export type Signal = { id: number; ts: string; mode?: string; action: string; strength: number; reason: string; timeframe: string; indicators: Record<string, number>; stop_price: number | null; risk: { approved: boolean; reason: string | null; order_krw: number | null; execution: 'EXECUTED' | 'BLOCKED' | 'SUPPRESSED' } | null };
export type Decision = {
  strategy_version?: string; evaluated_at?: string; market_bar_time?: string; timeframe?: string; decision?: string; reason?: string;
  daily_close?: number | null; daily_ema?: number | null; trend_ok?: boolean | null; close_4h?: number | null; donchian_upper?: number | null; breakout_ok?: boolean | null;
  donchian_lower?: number | null; atr?: number | null; position_quantity?: string | null; entry_price?: string | null; initial_stop?: string | null; current_stop?: string | null;
  trailing_stop?: number | null; channel_exit?: number | null; trend_loss?: boolean | null;
  risk_status?: { state: string; state_reason: string; execution_mode: string; inflight: number; unknown: number; daily_pnl: string | null; drawdown: string | null };
  current_price?: number | null; next_evaluation?: string | null; bot_state?: string; state_reason?: string;
};
export type ResearchE1 = {
  status: string; candidate?: string | null; baseline?: string | null; forward_since?: string | null; be_formula?: string | null;
  runner?: { pid: number; last_tick_at: string | null; ticks: number; ws_connected: boolean; errors: number; last_poll_at: string | null; open_positions: number } | null;
  entries_total?: number; matched_entries?: number;
  books?: Record<'S0' | 'S1', { completed: number; realized_pnl: number; wins: number; plus_1r_then_loss: number; meaningful_reversals: number; be_activated: number; be_exits: number; open: number; equity: number }>;
  observation_only?: { key: string; status: string; entry_fill?: string; initial_stop?: string | null; r_unit?: string | null; hypothetical_1r_trigger?: string | null; hypothetical_be_stop?: string; current_mfe_r?: string | null; live_current_stop?: string | null }[];
  candidate_status?: string; note?: string;
};
export type EthGrid = {
  status: string; candidate?: string | null; started_at?: string | null; seed_price?: string | null;
  runner?: { pid: number; last_tick_at: string | null; ticks: number; ws_connected: boolean; errors: number } | null;
  virtual_capital?: number; cash?: number; inventory_qty?: number; inventory_value?: number; inventory_cost?: number; equity?: number;
  realized?: number; unrealized?: number; fees?: number; completed_cycles?: number; cycle_avg_net?: number; buys?: number; sells?: number; units?: number;
  anchor?: string | null; gap?: string | null; mdd_pct?: number; halted?: boolean; halt_reason?: string | null; day_paused?: boolean; trend_ok?: boolean;
  last_price?: number; trades_today?: number; next_sell?: string; candidate_status?: string; note?: string;
  historical?: EthHistorical; preview?: EthPreview; display_status?: 'SHADOW' | 'RESEARCH_FAILED' | 'PLANNED';
};
export type Sleeves = {
  enabled: boolean;
  sleeves: {
    BTC_D0?: { allocated_capital: string; cash_reserved: string; cash_actual: string; position_quantity: string; position_value: string; realized_pnl: string; unrealized_pnl: string; fees: string; equity: string; available_to_strategy: string; next_max_entry_krw: string; next_entry_estimate_krw?: string; grandfathered_position: boolean; stop_price?: number | null; entry_time?: string | null; mode: string };
    ETH_GRID?: { allocated_capital: string; mode: string; shadow: EthGrid; ledger?: Record<string, unknown>; carve_out?: Record<string, unknown> | null };
  };
  global: { actual_krw: number; actual_btc: number; actual_eth: number; btc_value: number; eth_value?: number; total_equity: number; total_realized: number; total_unrealized: number; total_fees: number; global_mdd_pct: number; eth_reserved_krw: number; eth_live?: boolean; note: string };
};
export type Activity = { ts: string; kind: string; title: string; detail: string; raw: string; sleeve?: string };
export type EthLadderRow = { price: string; side: 'SELL' | 'BUY'; amount: string; state: 'OPEN' | 'WAIT'; quantity?: string; lot_id?: number; level?: number | null; seed?: boolean; strategy_epoch?: string; gap_pct?: string; legacy?: boolean };
export type EthFill = { ts: string; side: 'BUY' | 'SELL'; kind: 'SEED' | 'GRID'; krw: number | null; price: number | null; quantity: number | null; fee: number | null; net_pnl: number | null; client_order_id: string; uuid: string | null; state: string };
export type EthBook = {
  starting_equity: number | null; current_equity: number | null; high_watermark: number | null; mdd_pct: number | null; current_drawdown_pct: number | null;
  realized_pnl: number | null; unrealized_pnl: number | null; total_fees: number | null; gross_grid_profit: number | null; net_profit: number | null; return_pct: number | null;
  completed_cycles: number; buy_count: number; sell_count: number; turnover: number | null; eth_inventory_qty: number | null; eth_inventory_value: number | null; eth_inventory_cost: number | null;
  krw_inventory: number | null; avg_cycle_profit: number | null; median_cycle_profit: number | null; avg_cycle_duration_sec: number | null; best_cycle: number | null; worst_cycle: number | null;
  today_pnl: number | null; today_buys: number; today_sells: number; lots: number; status: string;
};
export type EthCompounding = {
  enabled: boolean; auto_rebalance: boolean; starting_capital: string; compounding_capital: string; current_equity: string; realized_net_pnl: string; unrealized_pnl: string;
  current_unit: string; base_unit: string; unit_ratio: string; max_single_order_krw: string | null; max_inventory_ratio: string; min_cash_ratio: string; max_inventory_krw: string; min_cash_krw: string;
  round_krw: string | null; max_step_up_pct: string | null; sizing_epoch: string; sizing_history: { epoch: string; at: string; capital: string; unit: string; prev_unit: string | null; reason: string; paused: boolean }[];
  paused_reason: string | null; capital_growth_realized_pct: string; capital_growth_equity_pct: string; profit_reinvested: string;
  attribution: { grid_realized_profit: string; unrealized_pnl: string; compounding_contribution: string | null; cycles_with_larger_unit: number };
  next_unit?: string | null; next_unit_threshold_capital?: string | null; realized_needed_for_next_unit?: string | null; projected_units?: { capital: string; unit: string }[];
};
export type EthLive = {
  display_status: 'LIVE_EXPERIMENT'; badge: string; warning: string; sleeve_mode: string | null; allocated_capital: number; strategy: string; research_gate: string;
  status: string; status_ko: string; status_reasons?: string[]; online: boolean; heartbeat_age_sec?: number | null; enabled?: boolean; enabled_at?: string | null;
  pid?: number | null; ws_connected?: boolean | null; last_tick_at?: string | null; errors?: number | null; last_error?: string | null; carve_out_done?: boolean | null;
  trend?: { ok: boolean; last_day: string; close: string; ema: string; days: number } | null; reconcile?: { status: string; at: string; detail?: string } | null;
  price?: number | null; anchor?: number | null; gap_pct?: number | null; seeded_at?: string | null; seed_price?: number | null; started_at?: string | null;
  pending?: { client_order_id: string; kind: string; side: string } | null; book?: EthBook; ladder?: { price: string; anchor: string; gap_pct: string; sells: EthLadderRow[]; buys: EthLadderRow[]; buys_allowed?: string };
  lots?: { lot_id: number; level: number | null; seed: boolean; quantity: string; target: string; buy_price: string; strategy_epoch?: string; gap_pct?: string; exit_only?: boolean }[]; recent_fills?: EthFill[]; cycles_recent?: Record<string, unknown>[];
  limits?: { unit_krw: number; base_unit_krw?: number; max_inventory_krw: number; min_cash_krw: number; daily_loss_pause_pct: number; mdd_halt_pct: number; ema_period: number; levels_each_side: number };
  compounding?: EthCompounding;
  buy_pause?: { paused: boolean; reason: string | null; at: string | null; by: string | null; sell_enabled: boolean; nearest_sell_target: number | null; nearest_sell_distance_pct: number | null; inventory_ratio_pct: number | null; cash_ratio_pct: number | null; open_lots: number };
  active_strategy?: { epoch: string; gap_pct: number | null; label: string; sell_target_rule: string; target_epoch?: string; cutover_pending?: boolean };
  legacy?: { lots: number; label: string | null; gap_pct: number | null; mode: string };
  cutover_history?: { at: string; from_epoch: string; from_gap: string; to_epoch: string; to_gap: string; activation_price: string; legacy_lots: number }[];
  server_time: string;
};
export type Overview = {
  total_equity: number; total_krw: number; btc_value: number; eth_value: number; challenge_start: number; total_pnl: number; total_pnl_pct: number; today_pnl: number;
  d0: { equity: number; pnl: number; today_pnl: number; allocated: number | null; carve_out: Record<string, unknown> | null };
  eth: { equity: number; cash: number; value: number; pnl: number; today_pnl: number; allocated: number; status: string | null; status_ko: string | null; online: boolean | null; started: boolean; carved: boolean };
  server_time: string;
};
export type EthCandidate = { key: string; description: string; gap_pct: string | null; return_pct: number; mdd_pct: number; pf: number; cycles: number; fees: number; recent_2y_return_pct: number; recent_2y_mdd_pct: number; recent_6m_return_pct: number; recent_6m_mdd_pct: number; cost_2x_return_pct: number; halted: boolean; gate_pass: boolean; gate_failed: string[] };
export type EthHistorical = { status: string; selected?: string; candidates?: EthCandidate[]; mdd_range_pct?: [number, number] | null; recent_2y_worst_mdd_pct?: number | null; reasons_ok?: string[]; reasons_fail?: string[]; conclusion?: string; dataset?: string; actual_eth?: number; actual_orders?: number };
export type EthPreview = { available: boolean; price?: number; gap_pct?: number; levels?: { price: number; side: 'SELL' | 'BUY' | 'CURRENT' }[]; badge?: string; note?: string };
export type EquityPoint = { time: number; equity: number; drawdown: number; btc_price: number };
export type Candles = { tf: string; candles: { time: number; open: number; high: number; low: number; close: number; volume: number }[]; markers: { time: number; side: 'BUY' | 'SELL'; price: number; text: string }[]; open_position: { entry_price: number; quantity: number } | null };
export type Health = {
  trader: string; heartbeat_age_sec: number | null; uptime_sec: number | null; websocket: string; rest: string; database: string;
  tailscale: { installed: boolean; state: string; dns_name?: string; tailscale_ips?: string[]; online?: boolean; detail?: string };
  last_market_data: string | null; market_data_age_sec: number | null; market_price: number | null; api_latency_ms: number | null;
  reconnect_count: number | null; api_errors: number | null; ws_messages: number | null;
  reconciliation: { status: string; at: string | null; krw_diff: number | null; btc_diff: number | null }; state: string; state_reason: string | null;
  power: { ac_power: boolean | null; battery_pct: number | null; sleep_prevented: boolean | null; system_sleep_min: number | null; display_sleep_min: number | null; warnings: string[] };
};
export type Event = { id: number; ts: string; level: string; type: string; message: string };
export type Command = { command_id: string; type: string; status: string; requested_by: string; requested_at: string; expires_at: string | null; executed_at: string | null; failure_reason: string | null; result: string | null };
export type ReadinessItem = { name: string; status: string; value: string; detail: string };
export type Readiness = { live_allowed: boolean; block_reason: string | null; groups: { name: string; status: string; items: ReadinessItem[] }[]; items: ReadinessItem[]; evaluated_at: string };

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return (await r.json()) as T;
}

export const api = {
  status: () => get<Status>('/api/status'),
  portfolio: () => get<Portfolio>('/api/portfolio'),
  stats: () => get<Stats>('/api/stats'),
  trades: () => get<Trade[]>('/api/trades?limit=30'),
  signals: () => get<Signal[]>('/api/signals?limit=20'),
  equity: () => get<EquityPoint[]>('/api/equity?hours=336'),
  candles: (tf: string) => get<Candles>(`/api/candles?tf=${tf}&limit=200`),
  health: () => get<Health>('/api/health'),
  events: () => get<Event[]>('/api/events?limit=40'),
  commands: () => get<Command[]>('/api/commands?limit=15'),
  readiness: () => get<Readiness>('/api/readiness'),
  decision: () => get<Decision>('/api/decision'),
  researchE1: () => get<ResearchE1>('/api/research/e1'),
  sleeves: () => get<Sleeves>('/api/portfolio/sleeves'),
  ethGrid: () => get<EthGrid>('/api/research/eth_grid'),
  activity: () => get<Activity[]>('/api/activity?limit=40'),
  ethLive: () => get<EthLive>('/api/eth/live'),
  overview: () => get<Overview>('/api/overview'),
  executions: () => get<Execution[]>('/api/executions?limit=20'),
  control: async (type: 'PAUSE' | 'RESUME' | 'KILL', token: string) => {
    const command_id = `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const r = await fetch(`/api/control/${type}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Control-Token': token, 'X-Requested-With': 'quant-dashboard' },
      body: JSON.stringify({ command_id, confirm: type }),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },
};
