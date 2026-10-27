"""Local CLI; imports copy only selected, explicitly provided market inputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import polars as pl
import duckdb

from app.dataset import data_root, publish, read_table
from app.demo import demo_bars
from app.market_import import import_tsp_market as import_tsp_market


def import_tsp(source: Path, root: Path, strategy_ids: list[str], max_symbols: int):
    """Read-only adapter. Does not import TSP secrets, strategies or account records."""
    cache_path = source / "user_data" / "strategy_history_cache.json"
    cache_revision = cache_path.stat().st_mtime_ns
    marker = source / ".matrix_generation_stock.json"
    marker_bytes = marker.read_bytes() if marker.exists() else None
    generation = json.loads(marker_bytes) if marker_bytes else {}
    if generation and generation.get("state") != "ready":
        raise ValueError("TSP行情正在发布，请完成同步后再导入")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    selected, names = [], {}
    for day, strategies in payload.get("days", {}).items():
        for sid in strategy_ids:
            for hit in strategies.get(sid, []):
                selected.append(dict(date=day, symbol=hit["symbol"], strategy_id=sid))
                names[hit["symbol"]] = str(hit.get("name") or hit["symbol"])
    if not selected:
        raise ValueError("指定策略没有历史信号")
    symbols = sorted(names)[:max_symbols]
    signals = pl.DataFrame(selected).filter(pl.col("symbol").is_in(symbols))
    # Prefer TSP's published per-symbol projection; never use backup/fixed/research folders.
    index_dir = source / "user_data" / "cache" / "enriched_symbol_index"
    indices = sorted(index_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if index_dir.exists() else []
    scan = None
    for index in indices:
        parquet = index.with_suffix(".parquet")
        metadata = json.loads(index.read_text(encoding="utf-8"))
        if parquet.exists() and generation.get("generation") and metadata.get("generation") == generation["generation"]:
            scan = pl.scan_parquet(parquet)
            break
    if scan is None:
        paths = list((source / "kline_daily_enriched").rglob("*.parquet"))
        if not paths:
            raise ValueError("TSP数据目录缺少日线")
        # Union by name handles TSP's old/new partition schemas without loading unrelated fields.
        with duckdb.connect(config={"threads": 2, "memory_limit": "512MB"}) as connection:
            connection.read_parquet([str(p) for p in paths], union_by_name=True, hive_partitioning=False).create_view("source")
            schema = {r[0] for r in connection.execute("DESCRIBE source").fetchall()}
            columns = [c for c in TSP_COLUMNS if c in schema]
            bars = connection.execute("SELECT " + ",".join(columns) + " FROM source WHERE symbol IN (SELECT unnest(?))",
                                      [symbols]).pl()
    else:
        schema = scan.collect_schema().names()
        columns = [c for c in TSP_COLUMNS if c in schema]
        bars = scan.filter(pl.col("symbol").is_in(symbols)).select(columns).collect(engine="streaming")
    bars = bars.with_columns(pl.col("symbol").replace_strict(names, default=pl.col("symbol")).alias("name"))
    # Only signals actually covered by this data snapshot may enter training.
    signals = signals.with_columns(pl.col("date").str.to_date()).join(
        bars.select("symbol", "date"), on=["symbol", "date"], how="semi")
    if (marker.read_bytes() if marker.exists() else None) != marker_bytes or cache_path.stat().st_mtime_ns != cache_revision:
        raise ValueError("TSP行情或信号在读取期间发生更新，未发布导入数据，请同步结束后重试")
    return publish(root, bars, signals, label="TSP本地导入", price_mode="qfq")


TSP_COLUMNS = ("symbol", "date", "open", "high", "low", "close", "raw_close", "raw_high", "raw_low",
               "volume", "consecutive_limit_ups", "consecutive_limit_downs")


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="Kline Trainer — 本地盘感训练")
    parser.add_argument("--data-dir", type=Path, help="行情与账户数据目录")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="创建合成演示行情，不覆盖账户记录")
    demo.add_argument("--replace", action="store_true", help="已有行情时发布新版本")
    csv = commands.add_parser("import-csv", help="导入CSV或Parquet的前复权行情")
    csv.add_argument("bars", type=Path)
    csv.add_argument("--signals", type=Path, help="可选，date/symbol/strategy_id三列")
    tsp = commands.add_parser("import-tsp", help="从本地TSP复制指定策略信号及对应行情（只读源目录）")
    tsp.add_argument("--source", required=True, type=Path, help="TSP的data目录")
    tsp.add_argument("--strategy", action="append", required=True, dest="strategies")
    tsp.add_argument("--max-symbols", type=int, default=100)
    serve = commands.add_parser("serve", help="启动本地训练服务，空目录自动生成演示行情")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.data_dir.expanduser().resolve() if args.data_dir else data_root()
    os.environ["TRAINER_DATA_DIR"] = str(root)
    if args.command == "serve":
        import uvicorn
        uvicorn.run("app.main:app", host=args.host, port=args.port, access_log=False)
        return
    if args.command == "demo":
        if (root / "manifest.json").exists() and not args.replace:
            parser.error("已有行情；如需发布新的演示版本，请显式传 --replace")
        result = publish(root, demo_bars(), label="合成演示行情", price_mode="raw")
    elif args.command == "import-tsp":
        if not 1 <= args.max_symbols <= 10000:
            parser.error("--max-symbols 必须在1至10000之间")
        if root.is_relative_to(args.source.resolve()) or args.source.resolve().is_relative_to(root):
            parser.error("导入目标与TSP源目录不得互相包含，请选择独立目录")
        result = import_tsp(args.source.resolve(), root, args.strategies, args.max_symbols)
    else:
        result = publish(root, read_table(args.bars), read_table(args.signals) if args.signals else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("数据已发布。请重启训练服务后使用新数据；原有账户和记录保留。")


if __name__ == "__main__":
    main()
