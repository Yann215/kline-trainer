import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_training import Market, act

from app.custom.training import router
from app.services.training import TrainingError, TrainingService, TrainingStore


@pytest.fixture
def replay_setup(tmp_path):
    market = Market()
    service = TrainingService(TrainingStore(tmp_path), market)
    view = service.create(dict(strategy_ids=['test'], strategy_source='live', request_id='original',
        practice_kind='single', horizon=20, auto_scan=False,
        history_start=market.dates[150], history_end=market.dates[150]))
    return service, market, view


def test_future_outcomes_only_after_finish_use_t0_close_and_planned_end(replay_setup):
    service, market, view = replay_setup
    source = service.store.load(view['id'])
    for row in source['assets']['A']['rows']:
        row.update(close=10., high=11., low=9., raw_close=100., raw_high=110., raw_low=90.)
        if row['date'] <= source['dates'][0]:
            row.update(high=999., low=.01)
    next(r for r in source['assets']['A']['rows'] if r['date'] == source['dates'][3])['high'] = 20.
    next(r for r in source['assets']['A']['rows'] if r['date'] == source['dates'][7])['low'] = 5.
    next(r for r in source['assets']['A']['rows'] if r['date'] == source['dates'][-1])['close'] = 12.
    source['assets']['A']['rows'].append({**source['assets']['A']['rows'][-1],
        'date': market.dates[171], 'high': 9999., 'low': .001})
    service.store.replace_for_test(source)
    active = service.view(view['id'])
    assert 'review' not in active and 'future_outcomes' not in json.dumps(active)
    before = service.store.load(view['id'])
    view = act(service, active, 'advance')
    view = act(service, view, 'finish')
    result = view['review']['future_outcomes'][0]
    assert result['alias'] == 'A' and result['base_close'] == 10.
    assert result['max_rise'] == 1. and result['max_fall'] == -.5
    assert result['max_rise_day'] == 3 and result['max_fall_day'] == 7
    assert result['final_return'] == pytest.approx(.2) and result['final_close'] == 12.
    assert result['final_day'] == 20 and result['last_available_day'] == 20
    assert result['final_date'] == source['dates'][-1] and result['data_status'] == 'complete'
    assert view['cursor'] == 1 and view['review']['end'] == source['dates'][1]
    assert view['equity'] == view['cash'] == 100000.
    after = service.store.load(view['id'])
    assert after['assets'] == before['assets'] and after['events'] == before['events']
    assert service.export(view['id'])['future_outcomes'] == view['review']['future_outcomes']


@pytest.mark.parametrize('direction', [1, -1])
def test_extrema_include_zero_baseline_and_short_missing_history(replay_setup, direction):
    service, _, view = replay_setup
    source = service.store.load(view['id'])
    for row in source['assets']['A']['rows']:
        row.update(close=10., high=10., low=10.)
        if row['date'] > source['dates'][0]:
            row.update(close=10. + direction, high=10. + direction, low=10. + direction)
    source['assets']['A']['rows'] = [r for r in source['assets']['A']['rows'] if r['date'] <= source['dates'][5]]
    service.store.replace_for_test(source)
    ended = act(service, view, 'finish')
    result = ended['review']['future_outcomes'][0]
    assert result['max_rise'] == pytest.approx(.1 if direction == 1 else 0)
    assert result['max_fall'] == pytest.approx(0 if direction == 1 else -.1)
    assert result['data_status'] == 'last_available' and result['last_available_day'] == 5
    assert result['final_day'] == 20
    source['assets']['A']['rows'] = [r for r in source['assets']['A']['rows'] if r['date'] != source['dates'][0]]
    source['finished'] = True
    service.store.replace_for_test(source)
    missing = service.view(view['id'])['review']['future_outcomes'][0]
    assert missing['data_status'] == 'missing_start' and missing['max_rise'] is None
    assert missing['max_fall'] is None and missing['final_return'] is None


def test_replay_is_new_blind_single_stock_record_and_uses_only_frozen_data(replay_setup, monkeypatch):
    service, market, view = replay_setup
    view = act(service, view, 'buy', alias='A', reason='原始理由不能复制到重训')
    with pytest.raises(TrainingError, match='结束'):
        service.replay(view['id'], dict(alias='A', revision=view['revision'], request_id='too-early'))
    view = act(service, view, 'finish')
    original = service.store.load(view['id'])
    original_bytes = copy.deepcopy(original)
    def forbidden(*args, **kwargs):
        raise AssertionError('replay must not scan strategies or read current market data')
    for name in ['generation', 'calendar', 'scan', 'snapshot', 'history_before']:
        monkeypatch.setattr(market, name, forbidden)
    command = dict(alias='A', revision=view['revision'], request_id='replay-1')
    replay = service.replay(view['id'], command)
    assert replay['id'] != view['id'] and replay['replay_of'] == view['id'] and replay['replay_number'] == 1
    assert replay['cursor'] == 0 and not replay['finished'] and 'review' not in replay
    assert replay['cash'] == replay['equity'] == original['initial']
    assert not replay['position'] and not replay['pending'] and not replay['events'] and not replay['trades']
    assert '原始理由不能复制到重训' not in json.dumps(replay, ensure_ascii=False)
    assert '000001.SZ' not in json.dumps(replay) and original['dates'][-1] not in json.dumps(replay)
    assert len(replay['candidates']) == 1 and not replay['auto_scan']
    stored = service.store.load(replay['id'])
    assert stored['assets']['A'] == original['assets']['A'] and stored['dates'] == original['dates']
    assert stored['costs'] == original['costs'] and stored['strategies'] == original['strategies']
    assert stored['fills'] == stored['analyses'] == []
    assert service.store.load(view['id']) == original_bytes
    assert service.replay(view['id'], command)['id'] == replay['id'] and len(service.store.list()) == 2
    replay = act(service, replay, 'buy', alias='A', reason='重训自己的新判断')
    replay = act(service, replay, 'advance')
    replay = act(service, replay, 'sell', alias='A', reason='重训自主卖出')
    assert len(replay['trades']) == 1
    replay = act(service, replay, 'finish')
    assert service.export(replay['id'])['replay_of'] == view['id']
    assert service.analysis_evidence(replay['id'])['is_replay']
    groups = service.summary()['groups']
    assert len(groups) == 2 and {g['is_replay'] for g in groups} == {True, False}
    assert sum(g['sessions'] for g in groups if not g['is_replay']) == 1
    assert service.view(view['id'])['record_group'] == replay['record_group']
    next_replay = service.replay(view['id'], {**command, 'request_id': 'replay-2'})
    assert next_replay['replay_number'] == 2


def test_replay_api_validation_verification_and_original_revision(replay_setup):
    service, _, view = replay_setup
    source = service.store.load(view['id'])
    source['verification'] = True
    service.store.replace_for_test(source)
    view = act(service, view, 'finish')
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    client = TestClient(app)
    url = f"/api/custom/training/sessions/{view['id']}/replay"
    payload = dict(alias='A', revision=view['revision'], request_id='api-replay')
    assert client.post(url, json={**payload, 'alias': 'missing'}).status_code == 400
    assert client.post(url, json={**payload, 'revision': 0}).status_code == 409
    assert client.post(url, json={**payload, 'alias': ''}).status_code == 422
    assert client.post('/api/custom/training/sessions/missing/replay', json=payload).status_code == 404
    response = client.post(url, json=payload)
    assert response.status_code == 200 and response.json()['verification']
    result = response.json()
    assert client.post(url, json=payload).json()['id'] == result['id']
    listed = next(s for s in service.list() if s['id'] == result['id'])
    assert listed['replay_number'] == 1 and listed['replay_of'] == view['id']
    result = act(service, result, 'finish')
    assert service.summary()['groups'] == []


def test_missing_intraday_extreme_is_not_reported_as_a_true_maximum(replay_setup):
    service, _, view = replay_setup
    source = service.store.load(view['id'])
    next(r for r in source['assets']['A']['rows'] if r['date'] == source['dates'][3])['high'] = None
    service.store.replace_for_test(source)
    ended = act(service, view, 'finish')
    result = ended['review']['future_outcomes'][0]
    assert result['data_status'] == 'partial_prices' and result['max_rise'] is None
    assert result['max_rise_day'] is None and result['max_rise_price'] is None
    assert result['max_fall'] is not None and result['final_return'] is not None
