import json
from types import SimpleNamespace

import polars as pl
import pytest

from app.dataset import LocalRepository
from app.demo import demo_bars
from app.market_import import import_tsp_market


def test_market_import_publishes_full_bars_and_preserves_source(tmp_path, monkeypatch):
    monkeypatch.setattr("app.market_import.shutil.disk_usage", lambda _: SimpleNamespace(free=9 * 1024**3))
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / ".matrix_generation_stock.json").write_text('{"state":"ready","generation":"abc"}')
    partition = source / "kline_daily_enriched" / "date=2019-01-02"
    partition.mkdir(parents=True)
    bars = demo_bars(3, 250)
    bars.write_parquet(partition / "part.parquet")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    target.mkdir()
    (target / "manifest.json").write_text('{"old":true}')
    result = import_tsp_market(source, target)
    assert result["symbols"] == 3 and result["rows"] == 750
    assert result["source_generation"] == "abc"
    repo = LocalRepository(target)
    source_one = bars.filter(pl.col("symbol") == bars["symbol"][0]).sort("date")
    actual = repo.get_daily(source_one["symbol"][0], source_one["date"].min(), source_one["date"].max())
    assert actual["raw_close"].to_list() == source_one["raw_close"].to_list()
    assert actual["volume"].to_list() == source_one["volume"].to_list()
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_market_import_failure_preserves_manifest(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    marker = target / "manifest.json"
    marker.write_text(json.dumps({"old": True}))
    (source / ".matrix_generation_stock.json").write_text('{"state":"publishing","generation":"abc"}')
    with pytest.raises(ValueError, match="正在发布"):
        import_tsp_market(source, target)
    assert json.loads(marker.read_text()) == {"old": True}
