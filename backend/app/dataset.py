"""Versioned local datasets. No TSP installation or network provider is needed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import duckdb
import polars as pl

from app.indicators.pipeline import compute_indicators
from app.market_time import cn_today

SCHEMA_VERSION = 1
SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
PRICE_COLUMNS = ["open", "high", "low", "close", "raw_close", "raw_high", "raw_low"]
REQUIRED = ["symbol", "date", *PRICE_COLUMNS, "volume"]
BUILTINS = {"demo_breakout": "20日放量突破", "demo_ma_reclaim": "站回MA5", "demo_trend": "均线多头"}


def data_root() -> Path:
    default = Path(__file__).resolve().parents[2] / "runtime"
    return Path(os.environ.get("TRAINER_DATA_DIR", default)).expanduser().resolve()


def read_table(path: Path) -> pl.DataFrame:
    if path.suffix.lower() == ".csv":
        return pl.read_csv(path, try_parse_dates=True, infer_schema_length=10000)
    if path.suffix.lower() == ".parquet":
        return pl.read_parquet(path)
    raise ValueError("只支持 CSV 或 Parquet 文件")


def validate_bars(frame: pl.DataFrame, price_mode: str = "qfq") -> pl.DataFrame:
    """Require raw prices explicitly; adjusted bars cannot infer exchange limits."""
    if price_mode not in {"qfq", "raw"}:
        raise ValueError("price_mode 只支持 qfq 或 raw")
    if price_mode == "raw":
        for key in ("close", "high", "low"):
            if f"raw_{key}" not in frame.columns and key in frame.columns:
                frame = frame.with_columns(pl.col(key).alias(f"raw_{key}"))
    missing = set(REQUIRED) - set(frame.columns)
    if missing:
        raise ValueError("行情缺少字段: " + ", ".join(sorted(missing)))
    fields = [*REQUIRED, *[c for c in ("name", "consecutive_limit_ups", "consecutive_limit_downs") if c in frame.columns]]
    frame = frame.select(fields).with_columns(
        pl.col("symbol").cast(pl.String),
        pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date(strict=True),
        *[pl.col(c).cast(pl.Float64) for c in [*PRICE_COLUMNS, "volume"]],
    )
    if frame.is_empty() or frame.select(pl.any_horizontal([pl.col(c).is_null() for c in REQUIRED]).any()).item():
        raise ValueError("行情为空或含缺失值")
    if frame.filter(~pl.col("symbol").str.contains(SYMBOL.pattern)).height:
        raise ValueError("symbol 格式须为 6位代码.SH/.SZ/.BJ")
    if frame.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("同一股票同一日期存在重复行情，拒绝静默覆盖")
    bad = frame.filter(
        pl.any_horizontal([~pl.col(c).is_finite() | (pl.col(c) <= 0) for c in PRICE_COLUMNS])
        | ~pl.col("volume").is_finite() | (pl.col("volume") < 0)
        | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        | (pl.col("raw_high") < pl.max_horizontal("raw_close", "raw_low"))
        | (pl.col("raw_low") > pl.min_horizontal("raw_close", "raw_high"))
        | (pl.col("date") >= cn_today())
    )
    if bad.height:
        raise ValueError("行情含非正价格、无效OHLC、负成交量或未结束的日期")
    if "name" not in frame.columns:
        frame = frame.with_columns(pl.col("symbol").alias("name"))
    else:
        frame = frame.with_columns(pl.col("name").fill_null(pl.col("symbol")))
    # Imported counters must be point-in-time values, never recomputed from adjusted returns.
    for key in ("consecutive_limit_ups", "consecutive_limit_downs"):
        if key not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Int32).alias(key))
        else:
            frame = frame.with_columns(pl.col(key).cast(pl.Int32))
            if frame.filter(pl.col(key) < 0).height:
                raise ValueError("涨跌停连续计数不能为负数")
    return frame.sort(["symbol", "date"])


def built_in_signals(frame: pl.DataFrame) -> pl.DataFrame:
    frame = frame.with_columns(
        pl.col("close").shift(1).rolling_max(20).over("symbol").alias("_prior_high"),
        pl.col("volume").shift(1).rolling_mean(5).over("symbol").alias("_prior_volume"),
        (pl.col("close").shift(1).over("symbol") <= pl.col("ma5").shift(1).over("symbol")).alias("_was_below"),
    )
    masks = {
        "demo_breakout": (pl.col("close") > pl.col("_prior_high")) & (pl.col("volume") >= 1.2 * pl.col("_prior_volume")),
        "demo_ma_reclaim": pl.col("_was_below") & (pl.col("close") > pl.col("ma5")),
        "demo_trend": (pl.col("close") > pl.col("ma5")) & (pl.col("ma5") > pl.col("ma10"))
        & (pl.col("ma10") > pl.col("ma20")) & (pl.col("ma20") > pl.col("ma60")),
    }
    return pl.concat([frame.filter(mask).select("date", "symbol", pl.lit(key).alias("strategy_id"))
                      for key, mask in masks.items()])


def validate_signals(signals: pl.DataFrame, bars: pl.DataFrame) -> pl.DataFrame:
    if not {"date", "symbol", "strategy_id"} <= set(signals.columns):
        raise ValueError("信号需要 date,symbol,strategy_id 三列")
    signals = signals.select("date", "symbol", "strategy_id").with_columns(
        pl.col("date").cast(pl.String).str.slice(0, 10).str.to_date(strict=True),
        pl.col("symbol").cast(pl.String), pl.col("strategy_id").cast(pl.String))
    if signals.select(pl.any_horizontal(pl.all().is_null()).any()).item():
        raise ValueError("信号不能含缺失字段")
    if signals.filter(~pl.col("strategy_id").str.contains(r"^[A-Za-z0-9_-]{1,100}$")).height:
        raise ValueError("策略ID只允许字母、数字、下划线和短横线，最长100字符")
    if signals.join(bars.select("date", "symbol"), on=["date", "symbol"], how="anti").height:
        raise ValueError("信号存在无对应行情的日期或股票")
    return signals.unique().sort(["date", "strategy_id", "symbol"])


def publish(root: Path, bars: pl.DataFrame, signals: pl.DataFrame | None = None,
            *, label="本地导入", price_mode="qfq", strategy_names=None) -> dict:
    """Publish a new immutable generation. Existing accounts are never touched."""
    bars = validate_bars(bars, price_mode)
    bars = compute_indicators(bars)
    bars = bars.with_columns(pl.col("close").rolling_mean(125).over("symbol").alias("ma125"))
    imported = signals is not None
    signals = validate_signals(signals if imported else built_in_signals(bars), bars)
    generation = uuid.uuid4().hex
    version = root / "datasets" / generation
    version.mkdir(parents=True, exist_ok=False)
    bars.write_parquet(version / "bars.parquet", compression="zstd", row_group_size=65536)
    signals.write_parquet(version / "signals.parquet", compression="zstd")
    names = strategy_names or ({} if imported else BUILTINS)
    manifest = dict(schema_version=SCHEMA_VERSION, generation=generation, label=label,
                    price_mode=price_mode, rows=bars.height, symbols=bars["symbol"].n_unique(),
                    start=str(bars["date"].min()), end=str(bars["date"].max()),
                    strategy_names=names, signal_origin="imported" if imported else "built_in_v1",
                    limit_counters_available=bars["consecutive_limit_ups"].null_count() == 0
                    and bars["consecutive_limit_downs"].null_count() == 0)
    temp = root / f"manifest-{generation}.tmp"
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, root / "manifest.json")
    return manifest


class LocalRepository:
    """Column projections and row-group pushdown, no all-market warmup per question."""
    def __init__(self, root: Path):
        self.store = SimpleNamespace(data_dir=root)
        self.manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("不支持的数据集版本")
        self.generation = self.manifest["generation"]
        if not re.fullmatch(r"[a-f0-9]{32}", self.generation):
            raise ValueError("数据版本格式无效")
        self.version = root / "datasets" / self.generation
        self.bars_path = self.version / "bars.parquet"
        self._instruments = pl.scan_parquet(self.bars_path).select("symbol", "name").unique().collect()

    def get_matrix_data_generation(self, asset_type):
        return self.generation

    def read_enriched_calendar(self):
        return pl.scan_parquet(self.bars_path).select("date").unique().sort("date").collect()["date"].to_list()

    def execute_all(self, sql, params=None):
        with duckdb.connect(config={"threads": 2, "memory_limit": "256MB"}) as connection:
            connection.read_parquet(str(self.bars_path)).create_view("kline_enriched")
            return connection.execute(sql, params or []).fetchall()

    def get_instruments_asset(self, asset_type):
        return self._instruments

    def read_enriched_panel(self, start, end, columns, symbols=None):
        query = pl.scan_parquet(self.bars_path).filter(pl.col("date").is_between(start, end))
        if symbols is not None:
            query = query.filter(pl.col("symbol").is_in(symbols))
        available = query.collect_schema().names()
        return query.select([c for c in columns if c in available]).collect()

    def get_daily(self, symbol, start, end):
        return pl.scan_parquet(self.bars_path).filter(
            (pl.col("symbol") == symbol) & pl.col("date").is_between(start, end)).collect()

    def read_enriched_before(self, symbol, before, count, columns, warmup_days=150):
        return (pl.scan_parquet(self.bars_path).filter((pl.col("symbol") == symbol) & (pl.col("date") < before))
                .sort("date").tail(count).select(columns).collect())

    def read_enriched_before_many(self, requests, columns, *, warmup_days=150, include_start=None, include_end=None):
        result = {}
        for symbol, (before, count) in requests.items():
            older = self.read_enriched_before(symbol, before, count, columns)
            span = self.read_enriched_panel(include_start, include_end, columns, [symbol])
            result[symbol] = pl.concat([older, span]).unique(["symbol", "date"]).sort("date")
        return result


class LocalStrategies:
    """Frozen, dated signal files. No arbitrary Python execution on import."""
    def __init__(self, repo: LocalRepository):
        self.repo = repo
        self.signals = pl.read_parquet(repo.version / "signals.parquet")
        self.ids = sorted(self.signals["strategy_id"].unique().to_list())

    def freeze_one(self, sid):
        if sid not in self.ids:
            raise ValueError("未知策略")
        name = self.repo.manifest["strategy_names"].get(sid, sid)
        fingerprint = hashlib.sha256(f"{self.repo.generation}:{sid}".encode()).hexdigest()
        return dict(id=sid, name=name, fingerprint=fingerprint, params={}, overrides={})

    def list_strategies(self):
        return [dict(id=sid, name=self.freeze_one(sid)["name"], available=True, reason="") for sid in self.ids]

    def scan(self, ids, as_of):
        frame = self.signals.filter((pl.col("date") == date.fromisoformat(as_of)) & pl.col("strategy_id").is_in(ids))
        names = dict(self.repo._instruments.iter_rows())
        return [(s, names.get(s, "")) for s in sorted(frame["symbol"].unique().to_list())]

    def cached_index(self, ids):
        names = dict(self.repo._instruments.iter_rows())
        days = {}
        for day, symbol, sid in self.signals.filter(pl.col("strategy_id").is_in(ids)).iter_rows():
            days.setdefault(str(day), {key: [] for key in ids})[sid].append((symbol, names.get(symbol, "")))
        origin = self.repo.manifest.get("signal_origin")
        return dict(days=days, version=1, updated_at=None, strategy_versions={},
                    parameter_version="unrecorded" if origin == "imported" else "recorded",
                    fingerprint=hashlib.sha256(json.dumps(days, sort_keys=True).encode()).hexdigest(),
                    cache_revision=[self.repo.generation])
