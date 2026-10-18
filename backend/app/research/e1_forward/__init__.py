"""D0 v2 candidate E1 (break-even at +1R) forward SHADOW validation.

S0 = D0 v1 baseline shadow (mirrors the LIVE trader's confirmed BUY entries, trailing-stop ratchets
and SELL decisions), S1 = S0 + fee/slippage-adjusted break-even stop once highest tick >= entry+1R.
Virtual books only: entries come from the production LIVE orders table (read-only), prices from the
public WebSocket ticker; writes go to data/d0_v2_e1_forward.db. No order path exists in this package
(tests/unit/test_e1_forward.py).
"""
