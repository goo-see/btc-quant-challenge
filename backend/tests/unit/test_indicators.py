from __future__ import annotations

import numpy as np
import pandas as pd

from app.market.candle_engine import candles_to_frame
from app.market.indicators import ATR, EMA, RSI, SMA, IndicatorSet
from tests.conftest import synthetic_1m


def _wilder_rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0)
    loss = -d.clip(upper=0)
    ag = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    # seed with SMA like our implementation
    ag.iloc[n] = gain.iloc[1 : n + 1].mean()
    al.iloc[n] = loss.iloc[1 : n + 1].mean()
    for i in range(n + 1, len(close)):
        ag.iloc[i] = (ag.iloc[i - 1] * (n - 1) + gain.iloc[i]) / n
        al.iloc[i] = (al.iloc[i - 1] * (n - 1) + loss.iloc[i]) / n
    rs = ag / al
    return 100 - 100 / (1 + rs)


def test_ema_matches_pandas_after_seed() -> None:
    candles = synthetic_1m(500)
    df = candles_to_frame(candles)
    ema = EMA(20)
    ours = [ema.update(x) for x in df["close"]]
    ref = df["close"].rolling(20).mean()
    # after seed, apply recursive EMA in pandas manually
    k = 2 / 21
    ref_vals = ref.to_numpy().copy()
    for i in range(20, len(ref_vals)):
        ref_vals[i] = (df["close"].iloc[i] - ref_vals[i - 1]) * k + ref_vals[i - 1]
    assert ours[18] is None
    assert np.allclose([o for o in ours[19:] if o is not None], ref_vals[19:], rtol=1e-9)


def test_sma() -> None:
    s = SMA(3)
    assert s.update(1) is None and s.update(2) is None
    assert s.update(3) == 2.0
    assert s.update(6) == (2 + 3 + 6) / 3


def test_rsi_matches_wilder_reference() -> None:
    candles = synthetic_1m(400)
    close = candles_to_frame(candles)["close"]
    rsi = RSI(14)
    ours = [rsi.update(x) for x in close]
    ref = _wilder_rsi(close, 14)
    for i in range(30, len(close)):
        assert ours[i] is not None
        assert abs(ours[i] - ref.iloc[i]) < 1e-6
    assert all(0 <= v <= 100 for v in ours if v is not None)


def test_atr_true_range_and_smoothing() -> None:
    atr = ATR(3)
    assert atr.update(10, 8, 9) is None  # TR=2
    assert atr.update(11, 9, 10) is None  # TR=max(2, |11-9|, |9-9|)=2
    assert atr.update(13, 10, 12) == (2 + 2 + 3) / 3  # TR=max(3,|13-10|,|10-10|)=3
    v = atr.update(12, 11, 11.5)  # TR=max(1,|12-12|,|11-12|)=1
    assert v is not None and abs(v - ((7 / 3) * 2 + 1) / 3) < 1e-12


def test_indicator_set_ready_and_prev_values() -> None:
    p: dict[str, float] = {
        "ema_fast": 3,
        "ema_slow": 5,
        "trend_ema": 5,
        "rsi_period": 3,
        "atr_period": 3,
        "volume_sma_period": 3,
    }
    s = IndicatorSet.from_params(p)
    out: dict[str, float] = {}
    for c in synthetic_1m(10):
        out = s.update(c)
    assert s.ready
    assert {
        "ema_fast",
        "ema_slow",
        "ema_trend",
        "rsi",
        "atr",
        "vol_sma",
        "prev_ema_fast",
        "prev_ema_slow",
    } <= set(out)
