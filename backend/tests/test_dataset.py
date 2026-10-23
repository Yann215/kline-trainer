import copy
import json
import uuid
from datetime import date

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.dataset import LocalRepository, LocalStrategies, built_in_signals, publish, validate_bars, validate_signals
from app.demo import demo_bars
from app.indicators.pipeline import compute_indicators
from app.main import create_app
from app.services.training import TrainingMarket, TrainingService, TrainingStore


@pytest.fixture
def dataset(tmp_path):
    publish(tmp_path, demo_bars(3, 700), price_mode="raw", label="合成测试行情")
    return tmp_path


def test_demo_reproducible_and_not_a_download():
    assert demo_bars(2, 200).equals(demo_bars(2, 200))
    assert all("合成" in name for name in demo_bars(2, 200)["name"].unique())


@pytest.mark.parametrize("error", ["duplicate", "negative_volume", "nan", "bad_ohlc", "bad_symbol", "future", "missing_raw"])
def test_import_rejects_invalid_input_without_replacing_manifest(tmp_path, error):
    frame = demo_bars(1, 180)
    publish(tmp_path, frame, price_mode="raw")
    manifest = (tmp_path / "manifest.json").read_bytes()
    if error == "duplicate":
        frame = pl.concat([frame, frame.head(1)])
    elif error == "negative_volume":
        frame = frame.with_columns(pl.lit(-1.).alias("volume"))
    elif error == "nan":
        frame = frame.with_columns(pl.lit(float("nan")).alias("close"))
    elif error == "bad_ohlc":
        frame = frame.with_columns((pl.col("low") - 1).alias("high"))
    elif error == "bad_symbol":
        frame = frame.with_columns(pl.lit("../../private").alias("symbol"))
    elif error == "future":
        frame = frame.with_columns(pl.lit(date(2099, 1, 1)).alias("date"))
    else:
        frame = frame.drop("raw_close")
    with pytest.raises(ValueError):
        publish(tmp_path, frame)
    assert (tmp_path / "manifest.json").read_bytes() == manifest


def test_import_signals_fail_closed_and_raw_mode_is_explicit():
    bars = demo_bars(1, 180)
    raw_missing = bars.drop("raw_close", "raw_high", "raw_low")
    with pytest.raises(ValueError, match="缺少字段"):
        validate_bars(raw_missing)
    assert validate_bars(raw_missing, "raw")["raw_close"].equals(bars["close"])
    signals = pl.DataFrame({"date": ["2019-01-02"], "symbol": ["000999.SZ"], "strategy_id": ["local"]})
    with pytest.raises(ValueError, match="无对应行情"):
        validate_signals(signals, bars)


def test_builtin_signals_are_prefix_invariant_and_no_next_day_data():
    bars = demo_bars(2, 280)
    cutoff = sorted(bars["date"].unique())[180]
    all_signals = built_in_signals(compute_indicators(bars)).filter(pl.col("date") <= cutoff)
    prefix = built_in_signals(compute_indicators(bars.filter(pl.col("date") <= cutoff)))
    assert all_signals.sort(["symbol", "date", "strategy_id"]).equals(prefix.sort(["symbol", "date", "strategy_id"]))


def test_local_strategy_cutoff_and_generation_fingerprint(dataset):
    repo = LocalRepository(dataset)
    engine = LocalStrategies(repo)
    sid = engine.ids[0]
    day = str(engine.signals.filter(pl.col("strategy_id") == sid)["date"].min())
    expected = set(engine.signals.filter((pl.col("date") == date.fromisoformat(day)) & (pl.col("strategy_id") == sid))["symbol"])
    assert {s for s, _ in engine.scan([sid], day)} == expected
    frozen = engine.freeze_one(sid)
    assert engine.cached_index([sid])["days"][day][sid]
    publish(dataset, demo_bars(3, 700), price_mode="raw")
    other = LocalStrategies(LocalRepository(dataset))
    assert frozen["fingerprint"] != other.freeze_one(sid)["fingerprint"]
    with pytest.raises(ValueError):
        engine.freeze_one("not_an_imported_strategy")


def test_imported_signals_start_on_signal_day_and_do_not_claim_recorded_parameters(tmp_path):
    bars = demo_bars(1, 500)
    days = sorted(bars["date"].unique())
    signals = bars.filter(pl.col("date") == days[300]).select("date", "symbol", pl.lit("my_signal").alias("strategy_id"))
    publish(tmp_path, bars, signals)
    repo = LocalRepository(tmp_path)
    engine = LocalStrategies(repo)
    assert engine.cached_index(["my_signal"])["parameter_version"] == "unrecorded"
    service = TrainingService(TrainingStore(tmp_path), TrainingMarket(repo, engine))
    view = service.create(dict(request_id="signal", selection_type="strategy", strategy_ids=["my_signal"],
                               strategy_source="cached", practice_kind="single", horizon=20))
    assert service.store.load(view["id"])["dates"][0] == str(days[300])
    assert view["candidates"][0]["bars"][-1]["date"] == "T+0"
    assert str(days[300]) not in json.dumps(view)


def test_local_api_full_workflow_blind_history_order_account_reveal_and_replay(dataset):
    app = create_app(dataset, seed_demo=False)
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok" and health["symbols"] == 3
        types = client.get("/api/custom/training/available-selection-types").json()
        assert "limit_up" not in {t["id"] for t in types}
        config = dict(selection_type="random", practice_kind="screening", candidate_count=3,
                      auto_scan=False, history_bars=500, continuous_account=True, horizon=20, request_id="new")
        response = client.post("/api/custom/training/sessions", json=config)
        assert response.status_code == 200, response.text
        view = response.json()
        initial_id = view["id"]
        assert len(view["candidates"]) == 3
        encoded = json.dumps(view, ensure_ascii=False)
        assert "600101.SH" not in encoded and "2019-" not in encoded and "合成样本" not in encoded
        old = copy.deepcopy(view["candidates"][0]["bars"])

        def command(action, **kw):
            nonlocal view
            response = client.post(f"/api/custom/training/sessions/{view['id']}/commands", json=dict(
                action=action, alias="A", reason="量价判断", revision=view["revision"], request_id=uuid.uuid4().hex,
                continuous_account=True, **kw))
            assert response.status_code == 200, response.text
            view = response.json()
            return view

        command("buy")
        assert view["position"]["cost_price"] > view["position"]["entry_price"]
        command("advance")
        assert view["cursor"] == 1
        assert view["candidates"][0]["bars"][:-1][-min(len(old)-1, 100):] == old[-min(len(old)-1, 100):]
        command("sell")
        assert view["position"] is None and len(view["trades"]) == 1
        command("finish")
        assert view["review"]["future_outcomes"][0]["final_day"] == 20
        account_id = view["account"]["id"]
        equity = view["equity"]
        assert client.get(f"/api/custom/training/sessions/{initial_id}/export").status_code == 200
        next_response = client.post("/api/custom/training/sessions", json={**config, "request_id": "next",
            "previous_session_id": initial_id, "account_id": account_id})
        assert next_response.status_code == 200, next_response.text
        view = next_response.json()
        assert view["account"]["id"] == account_id and view["equity"] == pytest.approx(equity)
        command("finish")
        records = client.get(f"/api/custom/training/account/{account_id}/records").json()
        assert len(records["questions"]) == 2
        response = client.post(f"/api/custom/training/sessions/{initial_id}/replay", json={
            "alias": "A", "revision": client.get(f"/api/custom/training/sessions/{initial_id}").json()["revision"],
            "request_id": "replay"})
        assert response.status_code == 200, response.text
        assert response.json()["replay_of"] == initial_id


def test_empty_directory_creates_working_demo_and_spa_routes(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>Kline Trainer</h1>", encoding="utf-8")
    with TestClient(create_app(tmp_path / "private", frontend=static)) as client:
        assert "合成" in client.get("/api/health").json()["dataset"]
        assert client.get("/training/account").text == "<h1>Kline Trainer</h1>"
        assert client.get("/api/does-not-exist").status_code == 404
        assert client.get("/../../manifest.json").text == "<h1>Kline Trainer</h1>"
