"""Research Round 3 — BTC trend following: timeframe & breakout-horizon study.

Everything in this package is SHADOW ONLY: it reads the market DB read-only, writes only to the
Round 3 research DB, builds strategies directly (never through the trader) and has no import path
to the exchange gateways, OrderManager, PaperGateway or RiskManager. See tests/unit/test_round3_*.
"""
