"""Pure trailing indicators extracted from TSP (MIT)."""
from __future__ import annotations
import polars as pl

def _ema_alpha(span: int) -> float:
    return 2.0 / (span + 1)

_INDICATOR_DEPS: dict[str, set[str]] = {
    "macd_dif": {"_ema12", "_ema26"},
    "boll_upper": {"ma20", "_boll_std"},
    "boll_lower": {"ma20", "_boll_std"},
    "macd_dea": {"macd_dif"},
    "macd_hist": {"macd_dif", "macd_dea"},
    "kdj_k": {"_kdj_ln", "_kdj_hn"},
    "kdj_d": {"kdj_k"},
    "kdj_j": {"kdj_k", "kdj_d"},
    "atr_14": {"_tr"},
    "vol_ratio_5d": {"_vol_ma5"},
    "annual_vol_20d": {"_daily_pct"},
    "rsi_6": {"_delta", "_gain", "_loss"},
    "rsi_14": {"_delta", "_gain", "_loss"},
    "rsi_24": {"_delta", "_gain", "_loss"},
}

_ALL_INDICATOR_COLS: frozenset[str] = frozenset({
    "prev_close", "ma5", "ma10", "ma20", "ma30", "ma60",
    "ema5", "ema10", "ema20", "ema30", "ema60", "_ema12", "_ema26",
    "_boll_std", "_kdj_ln", "_kdj_hn", "_tr", "vol_ma5", "vol_ma10",
    "_vol_ma5", "high_60d", "low_60d",
    "macd_dif", "boll_upper", "boll_lower", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "atr_14", "vol_ratio_5d",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "change_pct", "change_amount", "amplitude", "_daily_pct", "annual_vol_20d",
    "rsi_6", "rsi_14", "rsi_24",
})

def _resolve_needed(needed: set[str] | None) -> set[str]:
    """把 needed 展开为闭包 (含所依赖的中间列)。needed=None → 全集。"""
    if needed is None:
        return set(_ALL_INDICATOR_COLS)
    want = set(needed)
    changed = True
    while changed:
        changed = False
        for target in list(want):
            deps = _INDICATOR_DEPS.get(target)
            if deps and not deps <= want:
                want |= deps
                changed = True
    return want

def compute_indicators(
    df: pl.DataFrame,
    needed: set[str] | None = None,
    *,
    assume_sorted: bool = False,
) -> pl.DataFrame:
    """从 OHLCV 数据计算全套技术指标。

    输入必须包含: symbol, date, open, high, low, close, volume
    返回添加了所有指标列的 DataFrame。

    needed:
        None (默认) — 计算全部指标, 行为与历史逐位一致 (所有 gate 为真, 表达式/顺序不变)。
        列名集合   — 仅计算这些列及其依赖闭包, 跳过无关的 EMA/KDJ/RSI 等 pass;
                     输出保留输入列 + 所需指标列。被保留列的数值与全量计算逐位一致
                     (逐列 window/rolling 相互独立, 跳过其它列不影响保留列)。
    """
    if df.is_empty():
        return df

    import time as _time
    _t0 = _time.perf_counter()

    want = _resolve_needed(needed)

    df = df if assume_sorted else df.sort(["symbol", "date"])

    # Pass 1: 均线 + EMA + MACD 基础 + BOLL 基础 + KDJ 基础 + ATR 基础 + 量价 + 极值
    prev_close = pl.col("close").shift(1).over("symbol")
    _p1: list[pl.Expr] = []
    if "prev_close" in want:
        _p1.append(prev_close.alias("prev_close"))
    if "ma5" in want:
        _p1.append(pl.col("close").rolling_mean(5).over("symbol").alias("ma5"))
    if "ma10" in want:
        _p1.append(pl.col("close").rolling_mean(10).over("symbol").alias("ma10"))
    if "ma20" in want:
        _p1.append(pl.col("close").rolling_mean(20).over("symbol").alias("ma20"))
    if "ma30" in want:
        _p1.append(pl.col("close").rolling_mean(30).over("symbol").alias("ma30"))
    if "ma60" in want:
        _p1.append(pl.col("close").rolling_mean(60).over("symbol").alias("ma60"))
    if "ema5" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(5), adjust=False).over("symbol").alias("ema5"))
    if "ema10" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(10), adjust=False).over("symbol").alias("ema10"))
    if "ema20" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(20), adjust=False).over("symbol").alias("ema20"))
    if "ema30" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(30), adjust=False).over("symbol").alias("ema30"))
    if "ema60" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(60), adjust=False).over("symbol").alias("ema60"))
    if "_ema12" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(12), adjust=False).over("symbol").alias("_ema12"))
    if "_ema26" in want:
        _p1.append(pl.col("close").ewm_mean(alpha=_ema_alpha(26), adjust=False).over("symbol").alias("_ema26"))
    if "_boll_std" in want:
        _p1.append(pl.col("close").rolling_std(20).over("symbol").alias("_boll_std"))
    if "_kdj_ln" in want:
        _p1.append(pl.col("low").rolling_min(9).over("symbol").alias("_kdj_ln"))
    if "_kdj_hn" in want:
        _p1.append(pl.col("high").rolling_max(9).over("symbol").alias("_kdj_hn"))
    if "_tr" in want:
        _p1.append(pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - prev_close).abs(),
            (pl.col("low") - prev_close).abs(),
        ).alias("_tr"))
    if "vol_ma5" in want:
        _p1.append(pl.col("volume").rolling_mean(5).over("symbol").alias("vol_ma5"))
    if "vol_ma10" in want:
        _p1.append(pl.col("volume").rolling_mean(10).over("symbol").alias("vol_ma10"))
    if "_vol_ma5" in want:
        _p1.append(pl.col("volume").rolling_mean(5).over("symbol").alias("_vol_ma5"))
    if "vol_ratio_5d" in want:
        # 前5日平均成交量(不含当天), 标准量比分母: volume.shift(1).rolling_mean(5)
        _p1.append(pl.col("volume").shift(1).rolling_mean(5).over("symbol").alias("_vol_ma5_prev"))
    if "high_60d" in want:
        _p1.append(pl.col("close").rolling_max(60).over("symbol").alias("high_60d"))
    if "low_60d" in want:
        _p1.append(pl.col("close").rolling_min(60).over("symbol").alias("low_60d"))
    if _p1:
        df = df.with_columns(_p1)

    # Pass 2: MACD + BOLL (基于 Pass 1 基础列)
    _p2: list[pl.Expr] = []
    if "macd_dif" in want:
        _p2.append((pl.col("_ema12") - pl.col("_ema26")).alias("macd_dif"))
    if "boll_upper" in want:
        _p2.append((pl.col("ma20") + 2 * pl.col("_boll_std")).alias("boll_upper"))
    if "boll_lower" in want:
        _p2.append((pl.col("ma20") - 2 * pl.col("_boll_std")).alias("boll_lower"))
    if _p2:
        df = df.with_columns(_p2)
    if "macd_dea" in want:
        df = df.with_columns(
            pl.col("macd_dif").ewm_mean(alpha=_ema_alpha(9), adjust=False).over("symbol").alias("macd_dea"),
        )
    if "macd_hist" in want:
        df = df.with_columns(
            ((pl.col("macd_dif") - pl.col("macd_dea")) * 2).alias("macd_hist"),
        )

    # Pass 3: KDJ
    if "kdj_k" in want:
        # 9 日内最高价=最低价 (场内货币 ETF、长期无成交标的) 时分母是 0 而不是空值,
        # fill_null 拦不住: 0/0 得到 NaN, 再被 ewm 递推永久传染。与矩阵路径口径一致 ——
        # 该日 RSV 置空, EWM 跳过空值后继续递推。
        _kdj_range = pl.col("_kdj_hn") - pl.col("_kdj_ln")
        _kdj_rsv = pl.when(_kdj_range > 0).then(
            100 * (pl.col("close") - pl.col("_kdj_ln")) / _kdj_range
        )
        df = df.with_columns([
            _kdj_rsv.ewm_mean(alpha=1.0 / 3, adjust=False, ignore_nulls=True)
            .over("symbol").alias("kdj_k"),
        ])
    if "kdj_d" in want:
        df = df.with_columns([
            pl.col("kdj_k").ewm_mean(alpha=1.0 / 3, adjust=False, ignore_nulls=True)
            .over("symbol").alias("kdj_d"),
        ])
    if "kdj_j" in want:
        df = df.with_columns([
            (3 * pl.col("kdj_k") - 2 * pl.col("kdj_d")).alias("kdj_j"),
        ])

    # Pass 4: ATR + 量比 + 动量 + 波动 + 涨跌幅 + 涨跌额 + 振幅
    if "atr_14" in want:
        df = df.with_columns(
            pl.col("_tr").ewm_mean(alpha=1.0 / 14, adjust=False).over("symbol").alias("atr_14"),
        )
    if "vol_ratio_5d" in want:
        # 标准量比(同花顺/东财): 今日成交量 / 前5日均量(不含当天)
        # 盘后全量路径: 当日 volume 是完整全天量, 无需时间折算
        df = df.with_columns(
            (pl.col("volume") / pl.col("_vol_ma5_prev")).alias("vol_ratio_5d"),
        )
    _p4mom: list[pl.Expr] = []
    if "momentum_5d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(5).over("symbol") - 1).alias("momentum_5d"))
    if "momentum_10d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(10).over("symbol") - 1).alias("momentum_10d"))
    if "momentum_20d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(20).over("symbol") - 1).alias("momentum_20d"))
    if "momentum_30d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(30).over("symbol") - 1).alias("momentum_30d"))
    if "momentum_60d" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(60).over("symbol") - 1).alias("momentum_60d"))
    if "change_pct" in want:
        _p4mom.append((pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("change_pct"))
    if _p4mom:
        df = df.with_columns(_p4mom)
    if "change_amount" in want:
        df = df.with_columns(
            (pl.col("close") - pl.col("close").shift(1).over("symbol")).alias("change_amount"),
        )
    if "amplitude" in want:
        df = df.with_columns(
            pl.when(pl.col("close").shift(1).over("symbol") > 0)
              .then((pl.col("high") - pl.col("low")) / pl.col("close").shift(1).over("symbol"))
              .otherwise(None)
              .alias("amplitude"),
        )
    if "_daily_pct" in want:
        df = df.with_columns(
            pl.col("close").pct_change().over("symbol").alias("_daily_pct"),
        )
    if "annual_vol_20d" in want:
        df = df.with_columns(
            (pl.col("_daily_pct").rolling_std(20).over("symbol") * (252 ** 0.5))
                .alias("annual_vol_20d"),
        )

    # Pass 5: RSI
    if want & {"rsi_6", "rsi_14", "rsi_24"}:
        df = df.with_columns(
            pl.col("close").diff().over("symbol").alias("_delta"),
        ).with_columns([
            pl.when(pl.col("_delta") > 0).then(pl.col("_delta")).otherwise(0.0).alias("_gain"),
            pl.when(pl.col("_delta") < 0).then(-pl.col("_delta")).otherwise(0.0).alias("_loss"),
        ])
        for n in (6, 14, 24):
            if f"rsi_{n}" not in want:
                continue
            a = 1.0 / n
            df = df.with_columns([
                pl.col("_gain").ewm_mean(alpha=a, adjust=False).over("symbol").alias(f"_rsi_avg_gain_{n}"),
                pl.col("_loss").ewm_mean(alpha=a, adjust=False).over("symbol").alias(f"_rsi_avg_loss_{n}"),
            ]).with_columns(
                (100 - 100 / (1 + pl.col(f"_rsi_avg_gain_{n}") /
                             pl.when(pl.col(f"_rsi_avg_loss_{n}") == 0)
                               .then(1e-12)
                               .otherwise(pl.col(f"_rsi_avg_loss_{n}"))
                             )).alias(f"rsi_{n}"),
            )

    # Pass 6: 换手率 (需要 float_shares, 后续在 compute_all 中 JOIN instruments 后补充)

    # 清理临时列 (只丢弃实际存在的临时列)
    _temp_cols = ["_boll_std", "_tr", "_ema12", "_ema26",
                  "_kdj_ln", "_kdj_hn", "_vol_ma5", "_vol_ma5_prev", "_daily_pct",
                  "_delta", "_gain", "_loss",
                  "_rsi_avg_gain_6", "_rsi_avg_loss_6",
                  "_rsi_avg_gain_14", "_rsi_avg_loss_14",
                  "_rsi_avg_gain_24", "_rsi_avg_loss_24"]
    df = df.drop([c for c in _temp_cols if c in df.columns])

    _elapsed = (_time.perf_counter() - _t0) * 1000
    import logging as _logging
    _logging.getLogger(__name__).debug("compute_indicators: %.1fms, %d rows", _elapsed, len(df))

    return df
