# ruff: noqa: RUF001 -- Assert the exact Chinese UI outcome.
from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.training import router
from app.services.training import TrainingError, TrainingService, TrainingStore


class Market:
    def __init__(self):
        self.dates = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(220)]
        self.calls = []
        self.changed = False
        self.rows = []
        for i, d in enumerate(self.dates):
            price = 10 + i * 0.01
            self.rows.append(dict(date=d, open=price, high=price + .1, low=price - .1,
                                  close=price, raw_close=price, raw_high=price + .1,
                                  raw_low=price - .1, volume=10000, ma5=price - .02,
                                  macd_dif=.02, kdj_k=60, boll_upper=price + .2))

    def strategies(self):
        return [dict(id='test', name='测试量价', available=True, reason='')]

    def freeze(self, ids):
        if ids != ['test']:
            raise TrainingError('策略不可用')
        return [dict(id='test', name='测试量价', fingerprint='f', params={}, overrides={})]

    def calendar(self):
        return self.dates

    def generation(self):
        return 'changed' if self.changed else 'g'

    def scan(self, strategies, as_of):
        self.calls.append(as_of)
        return [('000001.SZ', '秘密名称')]

    def snapshot(self, symbol, start, end):
        return copy.deepcopy([r for r in self.rows if start <= r['date'] <= end])

    def history_before(self, symbol, before, count):
        return copy.deepcopy([r for r in self.rows if r['date'] < before][-count:])


@pytest.fixture
def setup(tmp_path):
    market = Market()
    service = TrainingService(TrainingStore(tmp_path), market)
    return service, market


def create(service, mode='close_t'):
    return service.create(dict(strategy_ids=['test'], mode=mode, horizon=20,
                               candidate_count=3, auto_scan=False, request_id='create-' + mode))


def test_configurable_pool_count_single_and_resume(setup, monkeypatch):
    service, market = setup
    monkeypatch.setattr(market, 'scan', lambda *a: [(f'600{i:03d}.SH', '样本') for i in range(30)])
    v = service.create(dict(strategy_ids=['test'], request_id='twenty', candidate_count=20, auto_scan=False))
    assert len(v['candidates']) == 20 and v['scan_info']['eligible_size'] == 30
    assert service.view(v['id']) == v
    one = service.create(dict(strategy_ids=['test'], request_id='single', candidate_count=20,
                              practice_kind='single', auto_scan=True))
    assert len(one['candidates']) == 1 and one['candidate_count'] == 1 and not one['auto_scan']
    with pytest.raises(TrainingError, match='同一题'):
        act(service, one, 'scan')
    with pytest.raises(TrainingError, match='先结束'):
        service.create(dict(strategy_ids=['test'], request_id='unfinished-next', previous_session_id=one['id']))
    one = act(service, one, 'finish')
    two = service.create(dict(strategy_ids=['test'], request_id='next', previous_session_id=one['id'], practice_kind='single'))
    assert two['question_number'] == 2
    assert '600' not in json.dumps(two['selection'])


def test_random_topic_without_strategy_and_date_filters(setup, monkeypatch):
    service, market = setup
    seen = []
    def select(selection, as_of, calendar):
        seen.append((selection, as_of))
        return [('600001.SH', '正常'), ('300001.SZ', 'ST样本')]
    monkeypatch.setattr(market, 'select_topic', select, raising=False)
    day = market.dates[130]
    v = service.create(dict(request_id='topic', selection_type='random', strategy_ids=[],
                            practice_kind='screening', candidate_count=10, horizon=20,
                            boards=['sh_main'], exclude_st=True, history_start=day, history_end=day,
                            scan_interval_min=1, scan_interval_max=1))
    assert len(v['candidates']) == 1 and v['strategies'] == []
    assert v['selection']['type'] == 'random' and seen[0][1] == day
    assert service.store.load(v['id'])['next_scan'] == 1
    advanced = act(service, v, 'advance')
    assert len(seen) == 2 and advanced['scan_info']['day'] == 1
    encoded = json.dumps(advanced, ensure_ascii=False)
    assert '600001.SH' not in encoded and '正常' not in encoded
    with pytest.raises(TrainingError, match='区间不足'):
        service.create(dict(request_id='too-early', selection_type='random', history_end=market.dates[100]))


def test_old_session_without_new_fields_remains_usable(setup):
    service, _ = setup
    v = create(service)
    s = service.store.load(v['id'])
    for key in ('selection', 'practice_kind', 'scan_interval_min', 'scan_interval_max', 'question_number', 'selection_version'):
        s.pop(key, None)
    service.store.replace_for_test(s)
    restored = service.view(v['id'])
    assert restored['practice_kind'] == 'screening' and restored['selection']['type'] == 'strategy'
    assert len(act(service, restored, 'scan')['candidates']) == 1


def test_summary_keeps_training_topics_separate(setup, monkeypatch):
    service, market = setup
    monkeypatch.setattr(market, 'select_topic', lambda *a: [('000001.SZ', '样本')], raising=False)
    a = create(service)
    act(service, a, 'finish')
    b = service.create(dict(request_id='random-summary', selection_type='random', practice_kind='single'))
    act(service, b, 'finish')
    groups = service.summary()['groups']
    assert len(groups) == 2 and {g['selection_type'] for g in groups} == {'strategy', 'random'}
    assert {g['practice_kind'] for g in groups} == {'single', 'screening'}


def test_cached_pool_avoids_engine_and_freezes_days(setup, monkeypatch):
    service, market = setup
    day = market.dates[130]
    cache = dict(days={day: {'test': [('000001.SZ', '缓存样本')]}}, version=1,
                 updated_at=1, fingerprint='cache-hash', parameter_version='unrecorded')
    monkeypatch.setattr(market, 'cached_index', lambda *a: cache, raising=False)
    monkeypatch.setattr(market, 'scan', lambda *a: pytest.fail('缓存模式不应调用历史引擎'))
    phases = []
    v = service.create(dict(strategy_ids=['test'], strategy_source='cached', horizon=20,
                            history_start=day, history_end=day, request_id='cache'),
                       progress=lambda *args: phases.append(args[0]))
    assert len(v['candidates']) == 1 and v['strategy_source'] == 'cached'
    assert '缓存' in ' '.join(phases)
    cache['days'].clear()
    assert act(service, v, 'scan')['scan_info']['pool_size'] == 1
    ended = act(service, service.view(v['id']), 'finish')
    exported = service.export(ended['id'])
    assert exported['cache_provenance']['parameter_version'] == 'unrecorded'
    assert exported['strategy_versions_role'] == 'reference_only'
    assert '缓存样本' not in json.dumps(v, ensure_ascii=False)


def test_missing_cache_and_cached_empty_results_do_not_fallback(setup, monkeypatch):
    service, market = setup
    day = market.dates[130]
    cache = dict(days={}, version=1, parameter_version='unrecorded')
    monkeypatch.setattr(market, 'cached_index', lambda *a: cache, raising=False)
    monkeypatch.setattr(market, 'scan', lambda *a: pytest.fail('不能回退到全量历史计算'))
    with pytest.raises(TrainingError, match='没有可用缓存起点'):
        service.create(dict(strategy_ids=['test'], strategy_source='cached', request_id='no-cache'))
    cache['days'][day] = {'test': []}
    with pytest.raises(TrainingError, match='没有可用缓存起点'):
        service.create(dict(strategy_ids=['test'], strategy_source='cached', horizon=20,
                            history_start=day, history_end=day, request_id='empty-cache'))


def test_candidate_batch_is_loaded_once(setup, monkeypatch):
    service, market = setup
    monkeypatch.setattr(market, 'scan', lambda *a: [(f'600{i:03d}.SH', '样本') for i in range(30)])
    calls = []
    def snapshot_many(symbols, start, end):
        calls.append(symbols)
        return {s: market.snapshot(s, start, end) for s in symbols}
    monkeypatch.setattr(market, 'snapshot_many', snapshot_many, raising=False)
    v = service.create(dict(strategy_ids=['test'], candidate_count=20, horizon=20, request_id='batch'))
    assert len(v['candidates']) == 20 and len(calls) == 1 and len(calls[0]) == 20


def test_real_batch_uses_repository_once_and_shared_indicators(tmp_path):
    from types import SimpleNamespace

    import polars as pl

    from app.indicators.pipeline import compute_indicators
    from app.services.training import TrainingMarket

    market = Market()
    rows = [{**r, 'symbol': symbol, 'date': date.fromisoformat(r['date'])}
            for symbol in ('000001.SZ', '600001.SH') for r in market.rows]
    frame = pl.DataFrame(rows).drop('ma5', 'macd_dif', 'kdj_k', 'boll_upper')
    calls = []
    def batch(symbols, start, end, columns):
        calls.append((symbols, start, end))
        return frame
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_enriched_range=lambda *a, **k: None,
                           get_daily_batch=batch)
    real = TrainingMarket(repo, None)
    result = real.snapshot_many(['000001.SZ', '600001.SH'], market.dates[80], market.dates[180])
    assert len(calls) == 1 and len(result['000001.SZ']) == 101
    expected = real._snapshot_rows(compute_indicators(frame.filter(pl.col('symbol') == '000001.SZ')).filter(
        pl.col('date').is_between(date.fromisoformat(market.dates[80]), date.fromisoformat(market.dates[180]))))
    assert result['000001.SZ'] == expected
    assert result['000001.SZ'][-1]['date'] == market.dates[180]


def act(service, view, action, **kwargs):
    return service.command(view['id'], dict(action=action, revision=view['revision'],
                                            request_id=f"{action}-{view['revision']}", **kwargs))


def test_blind_prefix_restore_and_finish(setup):
    service, market = setup
    v = create(service)
    encoded = json.dumps(v, ensure_ascii=False)
    assert '000001.SZ' not in encoded and '秘密名称' not in encoded and '2024-' not in encoded
    assert len(v['candidates'][0]['bars']) == 120
    assert v['candidates'][0]['bars'][-1]['date'] == 'T+0'
    assert market.calls == [service.store.load(v['id'])['dates'][0]]
    assert service.view(v['id']) == v
    future = service.store.load(v['id'])
    next_date = future['dates'][1]
    for row in future['assets']['A']['rows']:
        if row['date'] == next_date:
            row['close'] = 1e8
    service.store.replace_for_test(future)
    assert service.view(v['id']) == v
    v = act(service, v, 'finish')
    assert v['review']['identities'][0]['symbol'] == '000001.SZ'
    assert v['review']['stats']['trades'] == 0


def test_expand_history_freezes_all_prior_bars_without_revealing_future(setup):
    service, market = setup
    v = create(service)
    original = copy.deepcopy(v['candidates'][0]['bars'])
    v = act(service, v, 'buy', alias='A', reason='原始观察')
    stored = service.store.load(v['id'])
    original_event = copy.deepcopy(stored['events'][0])
    original_rows = copy.deepcopy(stored['assets']['A']['rows'])
    first = date.fromisoformat(market.dates[0])
    earlier = []
    for i in range(300):
        row = copy.deepcopy(market.rows[0])
        row['date'] = (first - timedelta(days=300 - i)).isoformat()
        earlier.append(row)
    market.rows = earlier + market.rows
    # A wider cache must not leak future or replace frozen prices.
    monkey_rows = copy.deepcopy(market.rows)
    monkey_rows[-1]['close'] = 1e9
    market.snapshot = lambda *args: monkey_rows
    payload = dict(alias='A', revision=v['revision'], request_id='all-history')
    expanded = service.expand_history(v['id'], payload)
    bars = expanded['candidates'][0]['bars']
    assert expanded['candidates'][0]['history_loaded'] is True
    assert len(bars) >= 420 and bars[-1]['date'] == 'T+0'
    assert bars[-120:] == original
    assert service.expand_history(v['id'], payload) == expanded
    assert service.view(v['id']) == expanded
    stored = service.store.load(v['id'])
    assert stored['events'][0] == original_event
    assert [r for r in stored['assets']['A']['rows'] if r['date'] >= market.dates[0]] == original_rows
    encoded = json.dumps(expanded, ensure_ascii=False)
    assert '000001.SZ' not in encoded and '秘密名称' not in encoded and '2024-' not in encoded
    expanded = act(service, expanded, 'hold', alias='A', reason='完整前史后的判断')
    assert service.store.load(v['id'])['events'][-1]['snapshot']['bars'] == bars
    advanced = act(service, expanded, 'advance')
    assert len(advanced['candidates'][0]['bars']) == len(bars) + 1
    assert advanced['candidates'][0]['bars'][-1]['date'] == 'T+1'


def test_history_generation_revision_and_alias_fail_closed(setup):
    service, market = setup
    v = create(service)
    for payload, message in [
        (dict(alias='Z', revision=v['revision']), '候选'),
        (dict(alias='A', revision=v['revision'] + 1), '版本'),
    ]:
        with pytest.raises(TrainingError, match=message):
            service.expand_history(v['id'], {**payload, 'request_id': 'bad'})
    market.changed = True
    with pytest.raises(TrainingError, match='数据版本'):
        service.expand_history(v['id'], dict(alias='A', revision=v['revision'], request_id='changed'))
    assert service.view(v['id']) == v


def test_history_load_does_not_lock_or_overwrite_concurrent_progress(setup):
    service, market = setup
    v = create(service)
    snapshot = market.snapshot
    concurrent = []
    def advance_during_read(*args):
        concurrent.append(act(service, v, 'advance'))
        return snapshot(*args)
    market.snapshot = advance_during_read
    with pytest.raises(TrainingError, match='版本'):
        service.expand_history(v['id'], dict(alias='A', revision=0, request_id='race'))
    assert service.view(v['id']) == concurrent[0]
    assert not service.view(v['id'])['candidates'][0]['history_loaded']


def test_history_api_contract_and_sanitized_failure(setup):
    service, market = setup
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    v = create(service)
    endpoint = f"/api/custom/training/sessions/{v['id']}/history"
    assert client.post(endpoint, json=dict(alias='A', revision=0)).status_code == 422
    response = client.post(endpoint, json=dict(alias='A', revision=0, request_id='history-api'))
    assert response.status_code == 200 and response.json()['candidates'][0]['history_loaded']
    other = service.create(dict(strategy_ids=['test'], request_id='other', horizon=20, auto_scan=False))
    def fail(*args):
        raise RuntimeError('000001.SZ 秘密名称 2024-01-01')
    market.snapshot = fail
    response = client.post(f"/api/custom/training/sessions/{other['id']}/history",
                           json=dict(alias='A', revision=0, request_id='fail-api'))
    assert response.status_code == 503 and '000001' not in response.text


def test_selected_history_count_does_not_load_all_and_preserves_frozen_bars(setup):
    service, market = setup
    v = service.create(dict(strategy_ids=['test'], request_id='bounded', horizon=20, auto_scan=False,
                            history_start=market.dates[190], history_end=market.dates[190]))
    v = act(service, v, 'buy', alias='A', reason='原窗口判断')
    original = service.store.load(v['id'])
    earlier = []
    for i in range(800):
        earlier.append({**market.rows[0], 'date': (date(2024, 1, 1) - timedelta(days=800-i)).isoformat()})
    market.rows = earlier + market.rows
    reads = []
    read = market.history_before
    def bounded_read(symbol, before, count):
        reads.append(count)
        return read(symbol, before, count)
    market.history_before = bounded_read
    def expand(view, count):
        return service.expand_history(view['id'], dict(alias='A', revision=view['revision'],
                                      request_id=f"window-{view['revision']}", bars=count))
    v = expand(v, 250)
    assert reads == [59]
    assert len(v['candidates'][0]['bars']) == 250
    assert v['candidates'][0]['history_window'] == 250 and not v['candidates'][0]['history_loaded']
    frozen = service.store.load(v['id'])
    assert len(frozen['assets']['A']['rows']) == 270
    assert frozen['events'] == original['events']
    assert frozen['assets']['A']['rows'][-len(original['assets']['A']['rows']):] == original['assets']['A']['rows']
    v = expand(v, 500)
    assert reads == [59, 250] and len(v['candidates'][0]['bars']) == 500
    v = expand(v, 120)
    assert reads == [59, 250] and len(v['candidates'][0]['bars']) == 120
    v = expand(v, 500)
    assert reads == [59, 250] and len(v['candidates'][0]['bars']) == 500
    v = act(service, v, 'advance')
    assert len(v['candidates'][0]['bars']) == 500 and v['candidates'][0]['bars'][-1]['date'] == 'T+1'
    v = expand(v, None)
    assert v['candidates'][0]['history_loaded'] and len(v['candidates'][0]['bars']) == 992
    assert service.view(v['id']) == v


def test_insufficient_history_count_reports_actual_available_and_api_bounds(setup):
    service, market = setup
    v = service.create(dict(strategy_ids=['test'], request_id='short-window', horizon=20,
                            auto_scan=False, history_start=market.dates[190], history_end=market.dates[190]))
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    endpoint = f"/api/custom/training/sessions/{v['id']}/history"
    payload = dict(alias='A', revision=0, request_id='short')
    assert client.post(endpoint, json={**payload, 'bars': 100}).status_code == 422
    response = client.post(endpoint, json={**payload, 'bars': 500})
    assert response.status_code == 200
    candidate = response.json()['candidates'][0]
    assert len(candidate['bars']) == 191 and candidate['history_window'] == 500 and candidate['history_loaded']
    with pytest.raises(TrainingError, match='根数'):
        service.expand_history(v['id'], {**payload, 'revision': 1, 'request_id': 'bad', 'bars': 100})


def test_market_bounded_history_queries_dates_and_reads_only_needed_range(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services.training import TrainingMarket

    queries, snapshots = [], []
    def execute(sql, params):
        queries.append((sql, params))
        return [(date(2024, 1, 5),), (date(2024, 1, 4),)]
    real = TrainingMarket(SimpleNamespace(execute_all=execute), None)
    rows = [{'date': f'2024-01-0{i}', 'close': 10} for i in range(1, 8)]
    def snapshot(symbols, start, end):
        snapshots.append((symbols, start, end))
        return {'000001.SZ': rows}
    monkeypatch.setattr(real, 'snapshot_many', snapshot)
    result = real.history_before('000001.SZ', '2024-01-06', 2)
    assert queries[0][1] == ['000001.SZ', date(2024, 1, 6), 2]
    assert snapshots == [(['000001.SZ'], '2024-01-04', '2024-01-05')]
    assert [r['date'] for r in result] == ['2024-01-04', '2024-01-05']


def test_reason_tplus1_costs_and_censoring(setup):
    service, _ = setup
    v = create(service)
    with pytest.raises(TrainingError, match='理由'):
        act(service, v, 'buy', alias='A', reason='  ')
    v = act(service, v, 'buy', alias='A', reason='平台突破', tags=['平台'], size=.5)
    assert v['position']['alias'] == 'A' and v['cash'] < 100000
    with pytest.raises(TrainingError, match=r'T\+1'):
        act(service, v, 'sell', alias='A', reason='回落')
    v = act(service, v, 'advance')
    v = act(service, v, 'sell', alias='A', reason='突破失败')
    assert v['position'] is None
    assert len(v['trades']) == 1 and v['trades'][0]['fees'] > 0
    v = act(service, v, 'buy', alias='A', reason='再次突破')
    v = act(service, v, 'finish')
    assert v['review']['stats']['trades'] == 1
    assert v['review']['stats']['open_positions'] == 1
    assert v['review']['events'][0]['reason'] == '平台突破'


def test_next_open_revision_idempotency_and_snapshot(setup):
    service, _ = setup
    v = create(service, 'open_t+1')
    v = act(service, v, 'indicators', indicators=['vol', 'macd', 'kdj', 'boll'])
    v = act(service, v, 'buy', alias='A', reason='均线确认')
    assert v['pending']['action'] == 'buy' and v['position'] is None
    before = copy.deepcopy(v)
    command = dict(action='advance', revision=v['revision'], request_id='retry-safe')
    v = service.command(v['id'], command)
    assert v['position'] and v['cursor'] == 1 and v['pending'] is None
    assert service.command(v['id'], command) == v
    with pytest.raises(TrainingError, match='版本'):
        service.command(v['id'], dict(action='advance', revision=before['revision'], request_id='stale'))
    event = service.store.load(v['id'])['events'][0]
    assert event['snapshot']['indicators'] == ['vol', 'macd', 'kdj', 'boll']
    assert event['snapshot']['bars'][-1]['date'] == 'T+0'
    assert v['position']['entry_day'] == 1
    # Next-open sell on buy-day close is legal: actual fill is tomorrow.
    v = act(service, v, 'sell', alias='A', reason='确认失败')
    v = act(service, v, 'advance')
    assert v['trades'][0]['exit_day'] == 2


def test_limit_and_missing_next_bar_do_not_peek_or_fill(setup):
    service, _ = setup
    v = create(service, 'open_t+1')
    session = service.store.load(v['id'])
    row = next(r for r in session['assets']['A']['rows'] if r['date'] == session['dates'][1])
    prior = next(r for r in session['assets']['A']['rows'] if r['date'] == session['dates'][0])
    # Open at limit, even if later it unlocked: cannot assume a morning fill.
    row.update(open=round(prior['raw_close'] * 1.1, 2), close=prior['raw_close'],
               raw_close=prior['raw_close'], high=15, low=10)
    service.store.replace_for_test(session)
    v = act(service, v, 'buy', alias='A', reason='信号')
    v = act(service, v, 'advance')
    assert v['position'] is None and v['pending'] is None
    assert '涨跌停' in v['notices'][-1]
    v = act(service, v, 'buy', alias='A', reason='再试')
    session = service.store.load(v['id'])
    session['assets']['A']['rows'] = [r for r in session['assets']['A']['rows']
                                    if r['date'] != session['dates'][2]]
    service.store.replace_for_test(session)
    v = act(service, v, 'advance')
    assert v['position'] is None


def test_generation_guard_empty_scan_and_atomic_rollback(setup):
    service, market = setup
    v = create(service)
    market.changed = True
    with pytest.raises(TrainingError, match='数据版本'):
        act(service, v, 'scan')
    assert service.view(v['id']) == v
    market.changed = False
    market.scan = lambda *args: []
    v = act(service, v, 'scan')
    assert v['candidates'] == []
    assert act(service, v, 'advance')['cursor'] == 1


def test_api_validation_no_leak_and_finish_gate(setup):
    service, _ = setup
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    assert client.post('/api/custom/training/sessions', json={}).status_code == 422
    v = create(service)
    assert client.get(f"/api/custom/training/sessions/{v['id']}/export").status_code == 409
    assert client.get('/api/custom/training/sessions/not-a-session').status_code == 404
    bad = client.post(f"/api/custom/training/sessions/{v['id']}/commands",
                      json=dict(action='buy', alias='A', reason='', request_id='bad', revision=0))
    assert bad.status_code == 400
    assert '000001' not in bad.text
    v = act(service, v, 'finish')
    export = client.get(f"/api/custom/training/sessions/{v['id']}/export")
    assert export.status_code == 200 and '秘密名称' in export.text
    aggregate = client.get('/api/custom/training/summary').json()
    assert aggregate['groups'][0]['mode'] == 'close_t'


def test_corporate_action_keeps_holding_and_pending_finish_cancels(setup):
    service, _ = setup
    v = create(service)
    v = act(service, v, 'buy', alias='A', reason='确认突破')
    session = service.store.load(v['id'])
    row = next(r for r in session['assets']['A']['rows'] if r['date'] == session['dates'][1])
    row['raw_close'] /= 2
    service.store.replace_for_test(session)
    v = act(service, v, 'advance')
    assert v['cursor'] == 1 and v['position'] is not None
    assert not v['trades']
    v = act(service, v, 'finish')
    assert v['review']['stats']['open_positions'] == 1
    assert v['review']['stats']['trades'] == 0
    pending = create(service, 'open_t+1')
    pending = act(service, pending, 'buy', alias='A', reason='等待确认')
    pending = act(service, pending, 'finish')
    assert pending['pending'] is None
    assert pending['review']['events'][0]['outcome'] == '结束时取消，未成交'


def test_restore_original_snapshot_and_persist_analysis(setup):
    service, market = setup
    v = create(service)
    v = act(service, v, 'reject', alias='A', reason='原始理由', tags=['平台'])
    event = copy.deepcopy(service.store.load(v['id'])['events'][0])
    v = act(service, v, 'advance')
    v = act(service, v, 'indicators', indicators=['boll', 'kdj'])
    assert service.store.load(v['id'])['events'][0] == event
    v = act(service, v, 'finish')
    service.save_analysis(v['id'], 'E1：原始理由，待验证')
    restored = TrainingService(TrainingStore(service.store.path.parent.parent.parent), market)
    assert restored.view(v['id'])['review']['analyses'][0]['content'].startswith('E1')


def test_open_fill_unchanged_by_later_candle_range(setup):
    service, _ = setup
    v = create(service, 'open_t+1')
    v = act(service, v, 'buy', alias='A', reason='开盘确认')
    session = service.store.load(v['id'])
    filled = act(service, v, 'advance')
    expected = service.store.load(v['id'])['fills']
    row = next(r for r in session['assets']['A']['rows'] if r['date'] == session['dates'][1])
    row['close'] *= 1.03
    row['raw_close'] *= 1.03
    row['high'] *= 1.08
    row['low'] *= .92
    row['volume'] *= 100
    service.store.replace_for_test(session)
    # Roll back the test-only idempotency marker along with the test fixture.
    with service.store.connect() as db:
        db.execute('DELETE FROM commands WHERE session_id=? AND request_id=?', (v['id'], f"advance-{v['revision']}"))
    changed = act(service, v, 'advance')
    assert service.store.load(v['id'])['fills'] == expected
    assert changed['position']['entry_price'] == filled['position']['entry_price']




def test_verification_sessions_do_not_affect_user_stats(setup):
    service, _ = setup
    v = service.create(dict(strategy_ids=['test'], horizon=20, request_id='qa', verification=True))
    v = act(service, v, 'buy', alias='A', reason='测试委托')
    v = act(service, v, 'advance')
    v = act(service, v, 'sell', alias='A', reason='验证清仓')
    act(service, v, 'finish')
    assert service.summary()['groups'] == []


def test_shared_indicator_prefix_has_no_future_effect():
    import polars as pl

    from app.indicators.pipeline import compute_indicators
    from app.services.training import DISPLAY_FIELDS

    rows = Market().rows
    df = pl.DataFrame([{**r, 'symbol': '000001.SZ', 'date': date.fromisoformat(r['date'])} for r in rows])
    full = compute_indicators(df)
    prefix = compute_indicators(df.head(180))
    cols = [c for c in DISPLAY_FIELDS if c in full.columns and c in prefix.columns]
    assert full.head(180).select(cols).equals(prefix.select(cols))


def test_ai_references_original_reasons_and_saves_report(setup, monkeypatch):
    from app.services import ai_provider

    service, _ = setup
    v = create(service)
    v = act(service, v, 'reject', alias='A', reason='原理由', tags=['平台'])
    act(service, v, 'finish')
    seen = []
    async def generate(messages, **kwargs):
        seen.append(messages)
        return 'E1: 用户原理由为原理由, 需要下一批验证。'
    monkeypatch.setattr(ai_provider, 'ai_configured', lambda: True)
    monkeypatch.setattr(ai_provider, 'generate_ai_text', generate)
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    response = TestClient(app).post(f"/api/custom/training/sessions/{v['id']}/analyze")
    assert response.status_code == 200
    payload = json.loads(seen[0][1]['content'])
    assert payload['events'][0]['reason'] == '原理由' and payload['events'][0]['id'] == 'E1'
    assert payload['events'][0]['size'] == .5
    assert payload['events'][0]['known_chart']['known_day'] == 0
    assert payload['events'][0]['known_chart']['recent_five'][-1]['date'] == 'T+0'
    assert '000001.SZ' not in seen[0][1]['content'] and '秘密名称' not in seen[0][1]['content']
    assert service.export(v['id'])['analyses'][0]['content'].startswith('E1')


def test_failed_auto_scan_retains_committed_step(setup):
    service, market = setup
    v = create(service)
    session = service.store.load(v['id'])
    session['auto_scan'], session['next_scan'] = True, 1
    service.store.replace_for_test(session)
    market.changed = True
    v = act(service, v, 'advance')
    assert v['cursor'] == 1 and not v['auto_scan']
    assert v['candidates'][0]['alias'] == 'A'
    assert v['notices'][-1].startswith('自动选股已暂停')




def test_observations_are_per_stock_without_trade_or_candidate_changes(setup, monkeypatch):
    service, market = setup
    monkeypatch.setattr(market, 'scan', lambda *a: [('000001.SZ', '样本一'), ('000002.SZ', '样本二')])
    v = create(service)
    app = FastAPI()
    app.include_router(router)
    app.state.training_service = service
    client = TestClient(app)
    initial = service.store.load(v['id'])
    for alias, reason, confidence, size in [('A', '放量后缩量，观察承接', 2, .25), ('B', '超跌后均线修复', 4, .5)]:
        request = dict(action='note', alias=alias, reason=reason, tags=[alias], invalidation='跌破观察低点' + alias,
                       confidence=confidence, size=size, request_id='note-' + alias, revision=v['revision'])
        response = client.post(f"/api/custom/training/sessions/{v['id']}/commands", json=request)
        assert response.status_code == 200
        v = response.json()
        assert client.post(f"/api/custom/training/sessions/{v['id']}/commands", json=request).json() == v
        e = v['events'][-1]
        assert (e['alias'], e['reason'], e['confidence'], e['size']) == (alias, reason, confidence, size)
        assert e['tags'] == [alias] and e['invalidation'] == '跌破观察低点' + alias
        assert v['position'] is None and v['pending'] is None and v['cash'] == initial['cash']
        assert not v['trades'] and not any(c['rejected'] for c in v['candidates'])
    assert len(v['events']) == 2 and service.view(v['id']) == v
    stored = service.store.load(v['id'])
    for key in ('active', 'rejected', 'fills', 'trades', 'cursor', 'next_scan'):
        assert stored[key] == initial[key]
    for e in stored['events']:
        assert e['snapshot']['bars'][-1]['date'] == 'T+0'
    for invalid in [dict(alias='Z', reason='其他票'), dict(alias='A', reason=' ')]:
        with pytest.raises(TrainingError):
            act(service, v, 'note', **invalid)
    assert service.view(v['id']) == v
    v = act(service, v, 'buy', alias='A', reason='买入理由')
    position = copy.deepcopy(v['position'])
    v = act(service, v, 'note', alias='B', reason='持仓时也可观察其他票')
    assert v['position'] == position and not v['pending'] and not v['trades']
    ended = act(service, v, 'finish')
    assert ended['review']['events'][0]['alias'] == 'A'
    assert ended['review']['events'][1]['alias'] == 'B'
    evidence = service.analysis_evidence(v['id'])
    assert [(e['alias'], e['action']) for e in evidence['events']] == [('A', 'note'), ('B', 'note'), ('A', 'buy'), ('B', 'note')]



def test_plain_advance_preserves_large_snapshots_and_idempotence(setup, monkeypatch):
    service, _ = setup
    v = create(service)
    v = act(service, v, 'note', alias='A', reason='原始图表判断')
    before = service.store.load(v['id'])
    # Large decision evidence must remain frozen, including legacy/additive keys.
    before['events'][0]['snapshot']['extra_evidence'] = 'x' * 200_000
    before['retained_extension'] = {'value': 'unchanged'}
    service.store.replace_for_test(before)
    expected = copy.deepcopy(before)
    expected['cursor'] += 1
    expected['revision'] += 1
    expected['equity'].append({'day': 1, 'value': service._equity(expected)})
    command = dict(action='advance', revision=v['revision'], request_id='fast-step')
    with monkeypatch.context() as patch:
        patch.setattr(service.store, 'read', lambda *a: pytest.fail('普通推进不应解码完整判断快照'))
        next_view = service.command(v['id'], command)
        assert service.command(v['id'], command) == next_view
        with pytest.raises(TrainingError, match='版本已变化'):
            service.command(v['id'], {**command, 'request_id': 'stale-step'})
    assert service.store.load(v['id']) == expected
    assert next_view == service._view(expected)
    assert next_view['candidates'][0]['bars'][-1]['date'] == 'T+1'
    assert 'extra_evidence' not in json.dumps(next_view)


def test_fast_step_does_not_skip_pending_fill_or_due_selection(setup):
    service, _ = setup
    v = create(service, 'open_t+1')
    v = act(service, v, 'buy', alias='A', reason='次日开盘买入')
    with service.store.connect(write=False) as db:
        assert service.store.read_step(db, v['id']) is None
    v = act(service, v, 'advance')
    assert v['position'] and v['pending'] is None
    assert v['events'][0]['outcome'] == 'T+1 已成交'
    session = service.store.load(v['id'])
    session['auto_scan'], session['next_scan'] = True, v['cursor'] + 1
    service.store.replace_for_test(session)
    with service.store.connect(write=False) as db:
        assert service.store.read_step(db, v['id']) is None
    v = act(service, v, 'advance')
    assert v['scan_info']['day'] == 2
    act(service, v, 'finish')
    with service.store.connect(write=False) as db:
        assert service.store.read_step(db, v['id']) is None


@pytest.mark.parametrize('window', [120, 250, 'all'])
def test_step_delta_equals_complete_revealed_chart(setup, window):
    service, _ = setup
    v = create(service)
    v = act(service, v, 'note', alias='A', reason='保留当时图表')
    request = dict(alias='A', bars=None if window == 'all' else window,
                   revision=v['revision'], request_id='history-delta')
    v = service.expand_history(v['id'], request)
    before = service.store.load(v['id'])
    request = dict(action='advance', revision=v['revision'], request_id='delta', step_delta=True, price_unit='cny_qfq')
    delta = service.command(v['id'], request)
    full = service.view(v['id'])
    assert delta['step_delta'] and delta['base_revision'] == v['revision']
    assert delta['candidates'][0]['bars'] == full['candidates'][0]['bars'][-1:]
    assert service.command(v['id'], request) == delta
    assert '000001.SZ' not in json.dumps(delta) and 'snapshot' not in json.dumps(delta)
    assert service.store.load(v['id'])['events'] == before['events']
    # Append/replace the single revealed row and trim only bounded windows.
    def merge(prior, update):
        rows = prior.copy()
        for row in update:
            if rows and rows[-1]['date'] == row['date']:
                rows[-1] = row
            else:
                rows.append(row)
        return rows if window == 'all' else rows[-window:]
    assert merge(v['candidates'][0]['bars'], delta['candidates'][0]['bars']) == full['candidates'][0]['bars']
    session = service.store.load(v['id'])
    day = session['dates'][session['cursor'] + 1]
    session['assets']['A']['rows'] = [r for r in session['assets']['A']['rows'] if r['date'] != day]
    service.store.replace_for_test(session)
    suspended = service.command(v['id'], {**request, 'revision':delta['revision'], 'request_id':'suspended'})
    assert not suspended['candidates'][0]['tradable']
    assert merge(full['candidates'][0]['bars'], suspended['candidates'][0]['bars']) == service.view(v['id'])['candidates'][0]['bars']
    # A much older repeated request must return a complete current view.
    old_retry = service.command(v['id'], request)
    assert not old_retry.get('step_delta') and old_retry == service.view(v['id'])


def test_delta_protocol_is_optional_and_pending_actions_return_full_view(setup):
    service, _ = setup
    v = create(service, 'open_t+1')
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    v = act(service, v, 'buy', alias='A', reason='次日成交完整返回')
    reply = client.post(f"/api/custom/training/sessions/{v['id']}/commands",
                        json=dict(action='advance',revision=v['revision'],request_id='pending-delta',step_delta=True))
    assert reply.status_code == 200
    v = reply.json()
    assert not v.get('step_delta') and len(v['candidates'][0]['bars']) == 120
    assert v['position'] and v['pending'] is None
    delta = client.post(f"/api/custom/training/sessions/{v['id']}/commands",
                        json=dict(action='advance',revision=v['revision'],request_id='api-delta',step_delta=True,price_unit='cny_qfq')).json()
    assert delta['step_delta'] and len(delta['candidates'][0]['bars']) == 1
    assert client.get(f"/api/custom/training/sessions/{v['id']}").json()['candidates'][0]['bars'][-1]['date'] == 'T+2'


@pytest.mark.parametrize('mode', ['close_t', 'open_t+1'])
def test_niu_starts_on_matching_signal_day_including_complete_signal_bar(setup, monkeypatch, mode):
    import random
    service, market = setup
    sid = 'custom_jg_niu_niu_select'
    signal = market.dates[130]
    days = {market.dates[129]: {sid: []}, signal: {sid: [('600001.SH', '牛牛信号')]},
            market.dates[131]: {sid: [('000002.SZ', '其他板块')]},
            market.dates[132]: {sid: [('600002.SH', 'ST信号')]}}
    monkeypatch.setattr(market, 'freeze', lambda ids: [dict(id=sid, name='牛牛', fingerprint='f')])
    monkeypatch.setattr(market, 'cached_index', lambda *a: dict(days=days), raising=False)
    monkeypatch.setattr(market, 'scan', lambda *a: pytest.fail('缓存抽题不能调用引擎'))
    monkeypatch.setattr(random.SystemRandom, 'choice', lambda self, values: values[0])
    config = dict(strategy_ids=[sid], strategy_source='cached', mode=mode, practice_kind='single',
                  history_start=market.dates[129], history_end=market.dates[132], horizon=20,
                  boards=['sh_main'], exclude_st=True, request_id='niu-signal')
    v = service.create(config)
    frozen = service.store.load(v['id'])
    assert frozen['dates'][0] == signal and v['cursor'] == 0
    assert frozen['scans'][0]['day'] == 0 and len(v['candidates']) == 1
    bars = v['candidates'][0]['bars']
    assert len(bars) == 120 and bars[-1]['date'] == 'T+0'
    assert all(int(b['date'][1:]) <= 0 for b in bars)
    assert bars[-1]['close'] == pytest.approx(market.rows[130]['close'])
    assert bars[-1]['volume'] == market.rows[130]['volume']
    assert any('信号当天' in n for n in v['notices'])
    v = act(service, v, 'buy', alias='A', reason='信号日判断')
    if mode == 'close_t':
        assert v['position']['entry_day'] == 0
    else:
        assert v['pending']['day'] == 0 and v['position'] is None
    v = act(service, v, 'advance')
    assert v['cursor'] == 1 and v['candidates'][0]['bars'][-1]['date'] == 'T+1'
    assert v['position']['entry_day'] == (0 if mode == 'close_t' else 1)
    # Bounds covering only a no-signal, excluded board or ST day cannot create a topic.
    for i in [129, 131, 132]:
        with pytest.raises(TrainingError, match='没有可用缓存起点'):
            service.create({**config, 'history_start': market.dates[i], 'history_end': market.dates[i],
                            'request_id': 'niu-no-signal-' + str(i)})


def test_niu_does_not_save_empty_question_when_signal_has_insufficient_local_history(setup, monkeypatch):
    service, market = setup
    sid = 'custom_jg_niu_niu_select'
    signal = market.dates[130]
    monkeypatch.setattr(market, 'freeze', lambda ids: [dict(id=sid, name='牛牛', fingerprint='f')])
    monkeypatch.setattr(market, 'cached_index', lambda *a: dict(days={signal: {sid: [('600001.SH', '信号')]}}), raising=False)
    monkeypatch.setattr(market, 'snapshot', lambda *a: [])
    with pytest.raises(TrainingError, match='没有可训练的策略信号股票'):
        service.create(dict(strategy_ids=[sid], strategy_source='cached', history_start=signal,
                            history_end=signal, horizon=20, request_id='niu-missing-history'))
    assert service.store.list() == []


def test_staged_question_is_hidden_until_claim_and_previous_must_finish(setup, monkeypatch):
    import random

    service, market = setup
    choices = iter([130, 140])
    monkeypatch.setattr(random.SystemRandom, "choice", lambda self, indices: next(choices))
    parent = create(service)
    config = dict(
        strategy_ids=["test"],
        horizon=20,
        auto_scan=False,
        request_id="staged",
        previous_session_id=parent["id"],
    )
    question = service.create(config, defer_save=True)
    assert len(service.store.list()) == 1
    assert question["cursor"] == 0 and question["dates"][0] == market.dates[140]
    with pytest.raises(TrainingError, match="先结束"):
        service.activate_preloaded(question, config)
    assert len(service.store.list()) == 1
    act(service, parent, "finish")
    view = service.activate_preloaded(question, config)
    assert view["question_number"] == 2 and len(service.store.list()) == 2
    assert view["candidates"][0]["bars"][-1]["date"] == "T+0"
    assert "000001.SZ" not in json.dumps(view)
    assert service.activate_preloaded(question, config)["id"] == view["id"]


def test_staged_question_rejects_changed_data_or_rules_and_same_day_repeat(setup, monkeypatch):
    service, market = setup
    config = dict(strategy_ids=["test"], horizon=20, auto_scan=False, request_id="staged-invalid")
    question = service.create(config, defer_save=True)
    market.changed = True
    assert service.activate_preloaded(question, config) is None
    market.changed = False
    with monkeypatch.context() as patch:
        patch.setattr(market, "freeze", lambda ids: [dict(fingerprint="new-rules")])
        assert service.activate_preloaded(question, config) is None
    assert not service.store.list()
    parent = service.create(config)
    parent = act(service, parent, "finish")
    question["dates"] = service.store.load(parent["id"])["dates"]
    assert (
        service.activate_preloaded(
            question, {**config, "request_id": "same-day", "previous_session_id": parent["id"]}
        )
        is None
    )
    assert len(service.store.list()) == 1


def test_training_calendar_cache_invalidation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import app.services.training as module
    from app.services.training import TrainingMarket

    calls = []
    generation = ["a"]
    today = [date(2024, 1, 3)]
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_matrix_data_generation=lambda kind: generation[0],
        execute_all=lambda sql: calls.append(sql) or [(date(2024, 1, 1),), (date(2024, 1, 3),)],
    )
    market = TrainingMarket(repo, None)
    monkeypatch.setattr(module, "cn_today", lambda: today[0])
    assert market.calendar() == ["2024-01-01"]
    assert market.calendar() == ["2024-01-01"] and len(calls) == 1
    generation[0] = "b"
    market.calendar()
    assert len(calls) == 2
    today[0] = date(2024, 1, 4)
    assert market.calendar() == ["2024-01-01", "2024-01-03"] and len(calls) == 3


@pytest.mark.parametrize("factor", [2.0, 0.8, 1.05])
def test_exrights_hold_continues_and_sell_uses_same_equity_basis(setup, factor):
    service, _ = setup
    v = create(service)
    v = act(service, v, "buy", alias="A", reason="持股跨除权")
    original = service.store.load(v["id"])
    session = copy.deepcopy(original)
    pos = session["position"]
    entry = next(r for r in session["assets"]["A"]["rows"] if r["date"] == session["dates"][0])
    # Flat adjusted price and a raw ex-right gap; should not create profit/loss.
    for row in session["assets"]["A"]["rows"]:
        if row["date"] >= session["dates"][1]:
            for key in ("open", "high", "low", "close"):
                row[key] = entry[key]
            row["raw_close"] = row["close"] / factor
            row["raw_high"] = row["high"] / factor
            row["raw_low"] = row["low"] / factor
    # Simulate a legacy saved holding without a new valuation field.
    session["position"].pop("adjusted_units", None)
    service.store.replace_for_test(session)
    v = act(service, v, "advance", step_delta=True)
    assert v["cursor"] == 1 and v["position"] and not v["pending"] and not v["finished"]
    assert v["equity"] == pytest.approx(service._equity(original))
    assert service.store.load(v["id"])["events"][0] == original["events"][0]
    assert not v["trades"] and len(service.store.load(v["id"])["fills"]) == 1
    # Duplicate request remains idempotent across the factor change.
    assert (
        service.command(
            v["id"],
            dict(
                action="advance",
                revision=original["revision"],
                request_id="advance-" + str(original["revision"]),
                step_delta=True,
            ),
        )
        == v
    )
    v = act(service, v, "hold", alias="A", reason="仍然继续持有")
    v = act(service, v, "sell", alias="A", reason="自主卖出")
    assert v["position"] is None and len(v["trades"]) == 1
    gross = original["position"]["shares"] * entry["close"]
    cost = original["position"]["cost"]
    sell_cost = 0.0002 + 0.0005 + 5 / 10000
    assert v["trades"][0]["profit"] == pytest.approx(gross * (1 - sell_cost) - cost)
    assert v["cash"] == pytest.approx(original["cash"] + gross * (1 - sell_cost))
    assert v["trades"][0]["entry_day"] == pos["entry_day"]


def test_pending_open_sell_across_exrights_uses_adjusted_reference(setup):
    service, _ = setup
    v = create(service, "open_t+1")
    v = act(service, v, "buy", alias="A", reason="次日开盘买入")
    v = act(service, v, "advance")
    v = act(service, v, "sell", alias="A", reason="次日自主卖出")
    s = service.store.load(v["id"])
    prior = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][1])
    following = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][2])
    for key in ("open", "high", "low", "close"):
        following[key] = prior[key]
    for key in ("raw_close", "raw_high", "raw_low"):
        following[key] = prior[key] / 2
    service.store.replace_for_test(s)
    v = act(service, v, "advance")
    assert v["cursor"] == 2 and v["position"] is None and v["pending"] is None
    assert v["events"][-1]["outcome"] == "T+2 已成交"
    assert len(v["trades"]) == 1


def test_exright_open_limit_bound_and_future_close_do_not_change_fill(setup):
    service, _ = setup
    v = create(service, "open_t+1")
    v = act(service, v, "buy", alias="A", reason="等待次日开盘")
    s = service.store.load(v["id"])
    prior = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][0])
    row = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][1])
    # 2-for-1 split: raw reference is half yesterday's price; raw open is limit up.
    prior["close"] = prior["raw_close"] = 10.0
    ref = 5.0
    row.update(close=10.0, raw_close=ref, open=11.0)
    service.store.replace_for_test(s)
    v = act(service, v, "advance")
    assert v["position"] is None and "涨跌停" in v["events"][0]["outcome"]


def test_price_factor_missing_is_reported_without_losing_holding(setup):
    service, _ = setup
    v = create(service)
    v = act(service, v, "buy", alias="A", reason="原持仓")
    s = service.store.load(v["id"])
    # Legacy units must derive from valid entry data, not guess at a factor.
    s["position"].pop("adjusted_units", None)
    row = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][0])
    row["raw_close"] = None
    service.store.replace_for_test(s)
    with pytest.raises(TrainingError, match="复权"):
        service.view(v["id"])
    assert service.store.load(v["id"])["position"] is not None


def test_exright_next_open_fill_is_independent_of_later_intraday_prices(setup):
    service, _ = setup
    filled = []
    for mode_id, later_close in enumerate([5.4, 6.1]):
        v = service.create(
            dict(
                strategy_ids=["test"],
                mode="open_t+1",
                horizon=20,
                auto_scan=False,
                request_id="future-candle-" + str(mode_id),
            )
        )
        s = service.store.load(v["id"])
        prior = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][0])
        prior["close"] = 5.0
        prior["raw_close"] = 10.0
        row = next(r for r in s["assets"]["A"]["rows"] if r["date"] == s["dates"][1])
        row.update(
            open=5.0,
            close=later_close,
            raw_close=later_close,
            high=8.0,
            low=4.0,
            raw_high=8.0,
            raw_low=4.0,
        )
        service.store.replace_for_test(s)
        v = act(service, v, "buy", alias="A", reason="只按次日开盘成交")
        v = act(service, v, "advance")
        pos = service.store.load(v["id"])["position"]
        assert pos is not None
        filled.append(
            {k: pos[k] for k in ["shares", "cost", "entry_day", "fees", "adjusted_units"]}
        )
    assert filled[0] == filled[1]


def test_history_list_decoding_does_not_block_writes(setup, monkeypatch):
    import threading

    import app.services.training as module
    service, _ = setup
    v = create(service)
    create(service, 'open_t+1')
    reading, release, committed = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original_loads = module.json.loads
    def slow_decode(*args, **kwargs):
        if threading.current_thread().name == 'slow-history-reader':
            reading.set()
            assert release.wait(3)
        return original_loads(*args, **kwargs)
    monkeypatch.setattr(module.json, 'loads', slow_decode)
    def reader():
        try:
            service.store.list()
        except Exception as exc:
            errors.append(exc)
    def writer():
        try:
            with service.store.connect() as db:
                db.execute('UPDATE sessions SET create_key=create_key WHERE id=?', (v['id'],))
            committed.set()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=reader, name='slow-history-reader')
    thread.start()
    assert reading.wait(1)
    mutation = threading.Thread(target=writer)
    mutation.start()
    try:
        assert committed.wait(.7), 'JSON解码期间不应占用SQLite读锁'
    finally:
        release.set()
        thread.join(2)
        mutation.join(2)
    assert not errors


def test_database_busy_is_specific_and_rolls_back_progress(setup, monkeypatch):
    import sqlite3

    import app.services.training as module
    service, _ = setup
    v = create(service)
    before = service.store.load(v['id'])
    original_connect = sqlite3.connect
    reader = original_connect(service.store.path)
    reader.execute('BEGIN')
    reader.execute('SELECT payload FROM sessions').fetchall()
    def short_timeout(*args, **kwargs):
        kwargs['timeout'] = .05
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(module.sqlite3, 'connect', short_timeout)
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    try:
        response = TestClient(app).post(f"/api/custom/training/sessions/{v['id']}/commands",
            json=dict(action='advance',revision=v['revision'],request_id='blocked-commit'))
        assert response.status_code == 409
        assert '训练记录正在' in response.text and '训练数据暂不可用' not in response.text
    finally:
        reader.rollback()
        reader.close()
    assert service.store.load(v['id']) == before


def test_strategy_record_groups_use_saved_ids_and_preserve_old_records(setup):
    service, _ = setup
    view = create(service)
    base = service.store.load(view['id'])
    with service.store.connect() as db:
        db.execute('DELETE FROM sessions WHERE id=?', (base['id'],))
        for index, (ids, names, profit) in enumerate([
            (['test'], ['旧名称'], .1),
            (['test'], ['新名称'], -.05),
            (['other'], ['新名称'], -.2),
            (['test', 'other'], ['新名称', '另一策略'], .3),
            (['other', 'test'], ['另一策略', '新名称'], -.1),
        ]):
            record = copy.deepcopy(base)
            record.update(id=f'group-{index}', create_key=f'group-{index}', finished=True)
            record['strategies'] = [dict(id=key, name=name, params={'changed': index}) for key, name in zip(ids, names, strict=True)]
            record['trades'] = [dict(pnl=profit)]
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (record['id'], record['create_key'], json.dumps(record)))
    before = service.store.list()
    records = {item['id']: item for item in service.list()}
    assert records['group-0']['record_group']['id'] == records['group-1']['record_group']['id']
    assert records['group-1']['record_group']['id'] != records['group-2']['record_group']['id']
    assert records['group-3']['record_group']['id'] == records['group-4']['record_group']['id']
    assert records['group-3']['record_group']['id'] != records['group-0']['record_group']['id']
    groups = {group['record_group']['id']: group for group in service.summary()['groups']}
    single = groups[records['group-0']['record_group']['id']]
    assert single['sessions'] == 2 and single['trades'] == 2 and single['win_rate'] == .5
    assert single['mean_trade_return'] == pytest.approx(.025)
    assert single['record_group']['name'] == '新名称'
    other = groups[records['group-2']['record_group']['id']]
    assert other['sessions'] == 1 and other['win_rate'] == 0
    assert service.store.list() == before
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    assert TestClient(app).get('/api/custom/training/sessions').json() == service.list()


def test_record_group_available_in_view_and_non_strategy_history(setup, monkeypatch):
    service, market = setup
    v = create(service)
    assert service.view(v['id'])['record_group'] == service.list()[0]['record_group']
    original = service.store.load(v['id'])
    monkeypatch.setattr(market, 'select_topic', lambda *a: [('000001.SZ', '样本')], raising=False)
    random_topic = service.create(dict(request_id='group-random', selection_type='random', practice_kind='single'))
    assert random_topic['record_group']['id'] != v['record_group']['id']
    assert random_topic['record_group']['name'] == random_topic['selection']['name']
    assert service.store.load(v['id']) == original


def test_chart_yuan_prices_raw_reference_and_original_records(setup):
    from app.services.training import PRICE_FIELDS

    service, market = setup
    for row in market.rows:
        for key in ('raw_close', 'raw_high', 'raw_low'):
            row[key] /= 2
    v = create(service)
    stored = service.store.load(v['id'])
    row = next(r for r in stored['assets']['A']['rows'] if r['date'] == stored['dates'][0])
    bar = v['candidates'][0]['bars'][-1]
    assert v['price_unit'] == 'cny_qfq'
    for key in PRICE_FIELDS:
        assert bar[key] == row.get(key)
    assert bar['raw_close'] == row['raw_close'] and bar['close'] != bar['raw_close']
    assert bar['kdj_k'] == row['kdj_k'] and bar['volume'] == row['volume']
    assert all(int(b['date'][1:]) <= 0 for b in v['candidates'][0]['bars'])
    bought = act(service, v, 'buy', alias='A', reason='实际股价判断')
    assert bought['position']['entry_price'] == row['close']
    original = service.store.load(v['id'])
    assert original['events'][0]['snapshot']['price_unit'] == 'cny_qfq'
    assert original['events'][0]['snapshot']['bars'] == v['candidates'][0]['bars']
    assert service.view(v['id']) == bought
    assert service.store.load(v['id']) == original
    delta = service.command(v['id'], dict(action='advance', revision=bought['revision'], request_id='yuan-step', step_delta=True, price_unit='cny_qfq'))
    assert delta['step_delta'] and delta['price_unit'] == 'cny_qfq'
    assert delta['candidates'][0]['bars'] == service.view(v['id'])['candidates'][0]['bars'][-1:]


def test_legacy_price_snapshots_keep_units_and_hashes_in_review(setup):
    from app.services.training import PRICE_FIELDS, _hash

    service, _ = setup
    v = act(service, create(service), 'buy', alias='A', reason='旧记录的理由')
    legacy = service.store.load(v['id'])
    scale = legacy['assets']['A']['scale']
    snapshot = legacy['events'][0]['snapshot']
    snapshot.pop('price_unit', None)
    for bar in snapshot['bars']:
        for key in PRICE_FIELDS:
            if bar.get(key) is not None:
                bar[key] *= scale
    legacy['events'][0]['snapshot_hash'] = _hash(snapshot)
    legacy['position']['display_price'] *= scale
    legacy['position'].pop('display_price_unit', None)
    for fill in legacy['fills']:
        fill['display_price'] *= scale
        fill.pop('display_price_unit', None)
    service.store.replace_for_test(legacy)
    before = service.store.load(v['id'])
    restored = service.view(v['id'])
    expected = next(row['close'] for row in before['assets']['A']['rows'] if row['date'] == before['dates'][before['cursor']])
    assert restored['candidates'][0]['bars'][-1]['close'] == expected
    assert restored['position']['entry_price'] == expected
    assert service.store.load(v['id']) == before
    current = act(service, restored, 'note', alias='A', reason='修复后的实际价格记录')
    act(service, current, 'finish')
    exported = service.export(v['id'])
    assert exported['events'][0] == before['events'][0]
    assert exported['price_units']['legacy_snapshot_default'] == 'normalized_100'
    evidence = service.analysis_evidence(v['id'])
    assert evidence['events'][0]['known_chart']['price_unit'] == 'normalized_100'
    assert evidence['events'][1]['known_chart']['price_unit'] == 'cny_qfq'
    assert evidence['events'][0]['known_chart']['last_bar']['close'] == snapshot['bars'][-1]['close']

def test_old_client_step_receives_complete_yuan_chart(setup):
    service, _ = setup
    v = create(service)
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    # A still-open older page can contain normalized bars. Never append one
    # yuan bar into that chart; require an explicit matching display unit.
    response = client.post(f"/api/custom/training/sessions/{v['id']}/commands",
        json=dict(action='advance', revision=v['revision'], request_id='old-price-client', step_delta=True))
    assert response.status_code == 200
    body = response.json()
    assert not body.get('step_delta') and body['price_unit'] == 'cny_qfq'
    assert len(body['candidates'][0]['bars']) == 120
    assert body == service.view(v['id'])

def test_preloaded_question_keeps_current_display_indicators_without_rebuilding(setup):
    service, _ = setup
    config = dict(strategy_ids=['test'], horizon=20, auto_scan=False, request_id='preload-indicators', indicators=['vol'])
    staged = service.create(config, defer_save=True)
    before = copy.deepcopy(staged['assets'])
    view = service.activate_preloaded(staged, {**config, 'request_id': 'claim-indicators', 'indicators': ['vol', 'macd', 'rsi']})
    assert view['indicators'] == ['vol', 'macd', 'rsi']
    assert service.store.load(view['id'])['assets'] == before



@pytest.mark.parametrize('count', [120, 250, 500])
def test_preload_freezes_selected_history_window_before_claim(setup, count):
    service, market = setup
    original_rows = copy.deepcopy(market.rows)
    market.rows = [{**market.rows[0], 'date': (date(2024, 1, 1) - timedelta(days=800-i)).isoformat()}
                   for i in range(800)] + market.rows
    cfg = dict(strategy_ids=['test'], request_id='window-preload', horizon=20, auto_scan=False,
        practice_kind='single', history_start=market.dates[190], history_end=market.dates[190], history_bars=count)
    question = service.create(cfg, defer_save=True)
    assert not service.store.list()
    assert question['assets']['A']['history_window'] == count
    before_claim = copy.deepcopy(question)
    reads = []
    def forbidden_read(*args):
        reads.append(args)
        raise AssertionError('ready claim or window switch must not read disk')
    market.history_before = forbidden_read
    view = service.activate_preloaded(question, {**cfg, 'request_id': 'claim-window'})
    assert len(view['candidates'][0]['bars']) == count
    assert view['cursor'] == 0 and all(int(b['date'][1:]) <= 0 for b in view['candidates'][0]['bars'])
    stored = service.store.load(view['id'])
    assert stored['assets'] == before_claim['assets'] and stored['events'] == []
    for row in stored['assets']['A']['rows']:
        if row['date'] >= '2024-01-01':
            assert row == next(r for r in original_rows if r['date'] == row['date'])
    view = service.expand_history(view['id'], dict(alias='A', revision=view['revision'], request_id='smaller-window', bars=120))
    view = service.expand_history(view['id'], dict(alias='A', revision=view['revision'], request_id='restore-window', bars=count))
    assert len(view['candidates'][0]['bars']) == count and not reads
    assert service.activate_preloaded(before_claim, {**cfg, 'request_id': 'mismatched-window',
        'history_bars': 250 if count != 250 else 500}) is None


def test_initial_history_window_short_data_validation_and_generation(setup):
    service, market = setup
    cfg = dict(strategy_ids=['test'], request_id='initial-window', horizon=20, auto_scan=False, strategy_source='live',
        history_start=market.dates[190], history_end=market.dates[190], history_bars=500)
    view = service.create(cfg)
    assert view['candidates'][0]['history_loaded'] and len(view['candidates'][0]['bars']) == 191
    with pytest.raises(TrainingError, match='根数'):
        service.create({**cfg, 'request_id': 'invalid-window', 'history_bars': 600})
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    assert client.post('/api/custom/training/sessions', json={**cfg, 'request_id': 'invalid-api-window', 'history_bars': 600}).status_code == 422
    response = client.post('/api/custom/training/sessions', json={**cfg, 'request_id': 'api-window'})
    assert response.status_code == 200 and response.json()['candidates'][0]['history_window'] == 500
    read = market.history_before
    def changed(*args):
        rows = read(*args)
        market.changed = True
        return rows
    market.history_before = changed
    with pytest.raises(TrainingError, match='更新'):
        service.create({**cfg, 'request_id': 'generation-changes'}, defer_save=True)
    assert len(service.store.list()) == 2
