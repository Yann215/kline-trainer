"""Deterministic synthetic OHLCV. Not downloaded or real security price history."""
from datetime import date, timedelta

import numpy as np
import polars as pl


def demo_bars(symbols=12, days=1000):
    rng = np.random.default_rng(20261023)
    dates, day = [], date(2019, 1, 2)
    while len(dates) < days:
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    rows = []
    for index in range(symbols):
        prefix, suffix = ("600", "SH") if index % 2 == 0 else ("000", "SZ")
        symbol = f"{prefix}{index+101:03d}.{suffix}"
        close = 10 + index * 3
        for i, day in enumerate(dates):
            opening = close * (1 + rng.normal(0, .004))
            cycle = np.sin(i/37 + index) * .004
            close = max(1, opening * (1 + rng.normal(.0002 + cycle, .016)))
            high = max(opening, close) * (1 + rng.uniform(0, .02))
            low = min(opening, close) * (1 - rng.uniform(0, .02))
            rows.append(dict(symbol=symbol, name=f"合成样本{index+1:02d}", date=day,
                             open=round(opening, 2), high=round(high, 2), low=round(low, 2),
                             close=round(close, 2), raw_close=round(close, 2), raw_high=round(high, 2),
                             raw_low=round(low, 2), volume=float(rng.integers(10000, 200000))))
    return pl.DataFrame(rows)
