"""Bounded-memory, read-only import of a published TSP stock dataset."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.parquet as pq

from app.dataset import BUILTINS, SCHEMA_VERSION, built_in_signals, validate_bars
from app.indicators.pipeline import compute_indicators

FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume", "raw_close", "raw_high", "raw_low",
          "consecutive_limit_ups", "consecutive_limit_downs"]


def import_tsp_market(source: Path, root: Path, *, max_symbols: int | None = None) -> dict:
    """Import published stock bars in batches, then atomically switch the manifest.

    A ready generation is required: the source is not a transactional snapshot otherwise.
    max_symbols is intended for a bounded smoke test, not a representative sample.
    """
    source, root = source.expanduser().resolve(), root.expanduser().resolve()
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("导入目标与TSP源目录不得互相包含")
    marker = source / ".matrix_generation_stock.json"
    try:
        marker_bytes = marker.read_bytes()
        state = json.loads(marker_bytes)
    except (OSError, ValueError) as exc:
        raise ValueError("TSP行情缺少有效发布标记，无法保证数据一致性") from exc
    if state.get("state") != "ready" or not state.get("generation"):
        raise ValueError("TSP行情正在发布或缺少版本号，请同步完成后重试")
    paths = sorted((source / "kline_daily_enriched").rglob("*.parquet"))
    if not paths:
        raise ValueError("TSP数据目录缺少日线")
    if max_symbols is not None and not 1 <= max_symbols <= 10000:
        raise ValueError("股票数量上限须在1至10000之间")
    instrument = source / "instruments" / "instruments.parquet"
    stat = instrument.stat() if instrument.is_file() else None
    instrument_revision = (stat.st_size, stat.st_mtime_ns) if stat else None
    names = None
    if instrument_revision:
        names = pl.read_parquet(instrument, columns=["symbol", "name"]).unique("symbol", keep="first")

    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < 8 * 1024**3 and max_symbols is None:
        raise ValueError("全市场导入至少需要 8 GiB 可用磁盘空间，实际需要量取决于行情规模")
    generation = uuid.uuid4().hex
    version = root / "datasets" / generation
    version.mkdir(parents=True, exist_ok=False)
    projection = version / "projection.tmp.parquet"
    bar_writer = signal_writer = None
    try:
        with duckdb.connect(config={"threads": 2, "memory_limit": "512MB"}) as db:
            db.execute("SET temp_directory = ?", [str(version)])
            db.read_parquet([str(path) for path in paths], union_by_name=True, hive_partitioning=False).create_view("source_bars")
            columns = {row[0] for row in db.execute("DESCRIBE source_bars").fetchall()}
            required = set(FIELDS[:10])
            if required - columns:
                raise ValueError("TSP行情缺少字段: " + ", ".join(sorted(required - columns)))
            selected = [name for name in FIELDS if name in columns]
            where = ""
            if max_symbols is not None:
                where = f" WHERE symbol IN (SELECT DISTINCT symbol FROM source_bars ORDER BY symbol LIMIT {max_symbols})"
            dest = str(projection).replace("'", "''")
            db.execute(f"COPY (SELECT {','.join(selected)} FROM source_bars{where} ORDER BY symbol,date) "
                       f"TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 65536)")

        rows = symbols = 0
        start = end = None
        counters_available = True
        pending = None

        def write(frame: pl.DataFrame) -> None:
            nonlocal rows, symbols, start, end, counters_available, bar_writer, signal_writer
            if names is not None:
                frame = frame.join(names, on="symbol", how="left")
            bars = validate_bars(frame, "qfq")
            bars = compute_indicators(bars, assume_sorted=True)
            bars = bars.with_columns(pl.col("close").rolling_mean(125).over("symbol").alias("ma125"))
            signals = built_in_signals(bars)
            bars_table, signals_table = bars.to_arrow(), signals.to_arrow()
            if bar_writer is None:
                bar_writer = pq.ParquetWriter(version / "bars.parquet", bars_table.schema, compression="zstd")
                signal_writer = pq.ParquetWriter(version / "signals.parquet", signals_table.schema, compression="zstd")
            bar_writer.write_table(bars_table, row_group_size=65536)
            signal_writer.write_table(signals_table, row_group_size=65536)
            rows += bars.height
            symbols += bars["symbol"].n_unique()
            lo, hi = bars["date"].min(), bars["date"].max()
            start = lo if start is None else min(start, lo)
            end = hi if end is None else max(end, hi)
            counters_available &= (bars["consecutive_limit_ups"].null_count() == 0
                                   and bars["consecutive_limit_downs"].null_count() == 0)

        for batch in pq.ParquetFile(projection).iter_batches(batch_size=65536):
            frame = pl.from_arrow(batch)
            if pending is not None:
                frame = pl.concat([pending, frame])
            last = frame["symbol"][-1]
            complete = frame.filter(pl.col("symbol") != last)
            pending = frame.filter(pl.col("symbol") == last)
            if complete.height:
                write(complete)
        if pending is not None and pending.height:
            write(pending)
        if bar_writer is None:
            raise ValueError("TSP行情为空")
        bar_writer.close()
        signal_writer.close()
        bar_writer = signal_writer = None
        current = instrument.stat() if instrument.is_file() else None
        current_revision = (current.st_size, current.st_mtime_ns) if current else None
        if marker.read_bytes() != marker_bytes or current_revision != instrument_revision:
            raise ValueError("TSP行情或股票名称在读取期间发生更新，请同步结束后重试")
        projection.unlink()
        manifest = dict(schema_version=SCHEMA_VERSION, generation=generation, label="TSP本地行情",
                        price_mode="qfq", rows=rows, symbols=symbols, start=str(start), end=str(end),
                        strategy_names=BUILTINS, signal_origin="built_in_v1", limit_counters_available=counters_available,
                        source_generation=state["generation"])
        temp = root / f"manifest-{generation}.tmp"
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, root / "manifest.json")
        return manifest
    except BaseException:
        if bar_writer is not None:
            bar_writer.close()
        if signal_writer is not None:
            signal_writer.close()
        shutil.rmtree(version, ignore_errors=True)
        raise
