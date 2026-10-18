"""ETH/KRW micro-grid sleeve research + shadow (spot only, no leverage, no real orders here).

Engine semantics are shared by the historical backtest (5m bars -> pessimistic two-tick feed) and
the forward shadow (public WebSocket ticks). Total sleeve equity (cash + ETH mark-to-market − fees)
is the only performance measure; grid realised profit alone is never reported as "the result".
"""
