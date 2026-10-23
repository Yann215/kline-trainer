import json

import polars as pl
import pytest

from app.cli import import_tsp
from app.dataset import LocalRepository
from app.demo import demo_bars


def test_tsp_import_ignores_stale_projection_and_never_touches_original(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    user = source / "user_data"
    user.mkdir(parents=True)
    bars = demo_bars(1, 250)
    day = str(bars["date"][180])
    cache = user / "strategy_history_cache.json"
    cache.write_text(json.dumps({"days": {day: {"my_signal": [{"symbol": "600101.SH", "name": "测试"}]}}}))
    marker = source / ".matrix_generation_stock.json"
    marker.write_text(json.dumps({"state": "ready", "generation": "new"}))
    index = user / "cache" / "enriched_symbol_index"
    index.mkdir(parents=True)
    (index / "old.json").write_text(json.dumps({"generation": "old"}))
    bars.with_columns(pl.lit(999.).alias("close")).write_parquet(index / "old.parquet")
    real = source / "kline_daily_enriched" / "date=2019-01-02"
    real.mkdir(parents=True)
    bars.write_parquet(real / "part.parquet")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    result = import_tsp(source, target, ["my_signal"], 100)
    assert result["rows"] == 250
    repo = LocalRepository(target)
    assert repo.get_daily("600101.SH", bars["date"].min(), bars["date"].max())["close"].to_list() == bars["close"].to_list()
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_tsp_import_rejects_active_publication(tmp_path):
    (tmp_path / "user_data").mkdir()
    (tmp_path / "user_data" / "strategy_history_cache.json").write_text('{"days":{}}')
    (tmp_path / ".matrix_generation_stock.json").write_text('{"state":"publishing","generation":"old"}')
    with pytest.raises(ValueError, match="正在发布"):
        import_tsp(tmp_path, tmp_path / "target", ["my_signal"], 100)
