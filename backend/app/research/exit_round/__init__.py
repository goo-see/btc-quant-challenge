"""D0 exit-rule research (E0 control, E1 break-even, E2 3-ATR trail, E3 partial TP, E4 profit lock).

Research only: same entry rules as D0 (production TrendFollowing4h class, unmodified), exit
overlays applied inside an isolated engine; artifacts under docs/research/exit_round_*. No
production code, DB, strategy yaml or LIVE state is touched (tests/unit/test_exit_round.py).
"""
