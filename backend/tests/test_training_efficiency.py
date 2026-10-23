import copy
import json

import pytest
from test_training import Market, act

from app.services.training import TrainingError, TrainingService, TrainingStore


@pytest.fixture
def trainer(tmp_path):
    market = Market()
    return TrainingService(TrainingStore(tmp_path), market), market


def create(service, market, key='one', **extra):
    return service.create(dict(strategy_ids=['test'], request_id=key, horizon=20,
                               auto_scan=False, candidate_count=1,
                               history_start=market.dates[150], history_end=market.dates[150], **extra))


def test_step_decodes_only_visible_assets_but_preserves_frozen_history(trainer):
    service, market = trainer
    view = create(service, market)
    view = act(service, view, 'buy', alias='A', reason='保留持仓', tags=['平台突破'])
    original = service.store.load(view['id'])
    original['assets']['B'] = copy.deepcopy(original['assets']['A'])
    original['assets']['B']['symbol'] = '600001.SH'
    original['assets']['inactive'] = copy.deepcopy(original['assets']['A'])
    original['active'] = ['B']  # Holding A remains visible even outside this scan.
    service.store.replace_for_test(original)
    with service.store.connect(write=False) as db:
        projected = service.store.read_step(db, view['id'])
    assert set(projected['assets']) == {'A', 'B'}
    assert 'snapshot' not in projected['events'][0]
    advanced = act(service, service.view(view['id']), 'advance', step_delta=True, price_unit='cny_qfq')
    assert advanced['cursor'] == 1 and {c['alias'] for c in advanced['candidates']} == {'A', 'B'}
    assert all(len(c['bars']) == 1 for c in advanced['candidates'])
    persisted = service.store.load(view['id'])
    assert persisted['assets'] == original['assets']
    assert persisted['events'] == original['events']
    assert persisted['cached_index'] == original['cached_index']
    assert '秘密名称' not in json.dumps(advanced, ensure_ascii=False)


def test_history_and_summary_do_not_decode_full_sessions(trainer, monkeypatch):
    service, market = trainer
    view = create(service, market)
    view = act(service, view, 'buy', alias='A', reason='原始理由', tags=['平台突破', '缩量回踩'])
    view = act(service, view, 'advance')
    view = act(service, view, 'finish')
    expected_list, expected_summary = service.list(), service.summary()
    monkeypatch.setattr(service.store, 'list', lambda: pytest.fail('Display endpoints must not decode full frozen sessions'))
    assert service.list() == expected_list
    assert service.summary() == expected_summary


def test_display_projection_remains_exact_for_legacy_holdings_and_verification(trainer):
    service, market = trainer
    view = create(service, market)
    view = act(service, view, 'buy', alias='A', reason='旧持仓', tags=['均线修复'])
    state = service.store.load(view['id'])
    state['position'].pop('adjusted_units', None)
    state['finished'] = True
    for key in ['selection', 'practice_kind', 'question_number', 'verification']:
        state.pop(key, None)
    # Deliberately stale saved curve: list returns the exact marked value.
    state['equity'][-1]['value'] = 123
    service.store.replace_for_test(state)
    expected_equity = service._equity(state)
    assert service.list()[0]['total_return'] == expected_equity / state['initial'] - 1
    assert service.summary()['groups'][0]['open_positions'] == 1
    state['verification'] = True
    service.store.replace_for_test(state)
    assert service.list()[0]['verification'] is True
    assert service.summary()['groups'] == []


def test_candidate_validation_does_not_build_an_extra_full_chart(trainer, monkeypatch):
    service, market = trainer
    view = create(service, market)
    calls = []
    original = service._view

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, '_view', counted)
    saved = act(service, view, 'note', alias='A', reason='记录后继续观察')
    assert saved['events'][-1]['reason'] == '记录后继续观察'
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(TrainingError, match='可见候选'):
        act(service, saved, 'note', alias='unknown', reason='不可见')
    assert not calls


def test_summary_projects_buy_tags_and_reads_fresh_records(trainer):
    service, market = trainer
    view = create(service, market)
    assert service.summary()['groups'] == []
    view = act(service, view, 'buy', alias='A', reason='证据', tags=['放量确认'])
    view = act(service, view, 'hold', alias='A', reason='持有', tags=['趋势延续'])
    view = act(service, view, 'finish')
    group = service.summary()['groups'][0]
    assert group['buy_tags'] == {'放量确认': 1}
    assert group['sessions'] == 1 and group['trades'] == 0 and group['open_positions'] == 1
    assert service.store.load(view['id'])['events'][1]['snapshot']['bars']
