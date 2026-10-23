import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_training import Market, act

from app.custom.training import router
from app.services.training import TrainingError, TrainingService, TrainingStore


@pytest.fixture
def account_setup(tmp_path):
    market = Market()
    service = TrainingService(TrainingStore(tmp_path), market)
    return service, market


def config(market, key, **extra):
    return dict(strategy_ids=['test'], strategy_source='live', request_id=key,
                practice_kind='single', horizon=20, auto_scan=False,
                history_start=market.dates[150], history_end=market.dates[150],
                continuous_account=True, **extra)


def test_one_account_rolls_marked_equity_to_next_question_and_restores(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'first'))
    v = act(service, v, 'buy', alias='A', reason='平台突破', pattern='短平台', size=.5)
    v = act(service, v, 'advance')
    frozen = copy.deepcopy(service.store.load(v['id'])['events'])
    v = act(service, v, 'finish')
    ending = v['equity']
    assert v['account_settlement']['kind'] == 'mark_to_market'
    assert v['review']['stats']['trades'] == 0
    assert v['review']['stats']['open_positions'] == 1
    assert service.store.load(v['id'])['events'] == frozen
    nxt = service.create(config(market, 'second', previous_session_id=v['id']))
    assert nxt['cash'] == pytest.approx(ending)
    assert nxt['position'] is None
    assert nxt['account']['id'] == v['account']['id']
    assert nxt['account']['initial'] == 100000
    restored = TrainingService(TrainingStore(service.store.path.parents[2]), market)
    data = restored.accounts.view()
    assert data['account']['status'] == 'active'
    assert data['account']['current_session_id'] == nxt['id']
    assert data['points'][0]['value'] == 100000
    assert data['points'][-1]['value'] == pytest.approx(ending)
    assert all('date' not in point for point in data['points'])


def test_manual_stop_cancels_pending_and_is_idempotent(account_setup):
    service, market = account_setup
    cfg = config(market, 'pending')
    cfg['mode'] = 'open_t+1'
    v = service.create(cfg)
    v = act(service, v, 'buy', alias='A', reason='等待确认', pattern='修复')
    account = v['account']
    command = dict(revision=account['revision'], request_id='stop')
    ended = service.accounts.stop(account['id'], command)
    assert ended['account']['status'] == 'closed'
    current = service.view(v['id'])
    assert current['finished'] and current['pending'] is None
    assert '未成交' in current['events'][0]['outcome']
    assert service.accounts.stop(account['id'], command) == ended
    with pytest.raises(TrainingError, match='结束'):
        act(service, current, 'advance')


def test_stop_with_position_uses_mark_value_not_a_fake_sell(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'hold'))
    v = act(service, v, 'buy', alias='A', reason='观察', size=1)
    expected = v['equity']
    ended = service.accounts.stop(v['account']['id'], dict(revision=v['account']['revision'], request_id='end'))
    assert ended['account']['equity'] == pytest.approx(expected)
    assert not service.view(v['id'])['trades']
    assert service.accounts.records(v['account']['id'])['stats']['trades'] == 0


def test_active_account_prevents_parallel_questions_and_stale_stop(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'current'))
    with pytest.raises(TrainingError, match='当前题'):
        service.create(config(market, 'fork'))
    assert len(service.accounts.view()['accounts']) == 1
    stale = v['account']['revision']
    v = act(service, v, 'advance')
    with pytest.raises(TrainingError, match='版本'):
        service.accounts.stop(v['account']['id'], dict(revision=stale, request_id='stale'))
    assert service.view(v['id'])['finished'] is False


def test_preload_binds_current_balance_only_when_claimed(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'current'))
    staged = service.create(config(market, 'hidden'), defer_save=True)
    assert len(service.accounts.view()['accounts']) == 1
    assert not staged.get('account_id')
    v = act(service, v, 'buy', alias='A', reason='试错', size=1)
    v = act(service, v, 'advance')
    v = act(service, v, 'finish')
    cfg = config(market, 'claim', previous_session_id=v['id'])
    # Allow a different blind sample for the existing same-day exclusion check.
    for asset in staged['assets'].values():
        asset['symbol'] = '600000.SH'
    nxt = service.activate_preloaded(staged, cfg)
    assert nxt['cash'] == pytest.approx(v['equity'])
    assert nxt['account']['id'] == v['account']['id']
    assert service.create(cfg)['id'] == nxt['id']


def test_mode_records_use_entry_mode_and_keep_reasons(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'mode'))
    v = act(service, v, 'buy', alias='A', reason='缩量后突破', pattern='短平台', size=.5)
    v = act(service, v, 'advance')
    v = act(service, v, 'sell', alias='A', reason='跌破观察线', pattern='退出')
    records = service.accounts.records(v['account']['id'])
    assert records['stats']['trades'] == 1
    assert records['groups'][0]['pattern'] == '短平台'
    assert records['events'][0]['reason'] == '缩量后突破'
    assert records['events'][0]['pattern'] == '短平台'
    assert '秘密名称' not in json.dumps(records, ensure_ascii=False)
    assert 'snapshot' not in records['events'][0]


def test_legacy_session_can_join_on_first_command_without_rewriting_history(account_setup):
    service, market = account_setup
    cfg = config(market, 'legacy')
    cfg.pop('continuous_account')
    v = service.create(cfg)
    v = act(service, v, 'buy', alias='A', reason='原理由', size=.5)
    original = copy.deepcopy(service.store.load(v['id'])['events'])
    assert service.accounts.view()['account'] is None
    v = act(service, v, 'advance', continuous_account=True)
    assert v['account']['equity'] == pytest.approx(v['equity'])
    assert service.store.load(v['id'])['events'] == original


def test_closed_account_never_reopens_through_next_or_old_commands(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'old'))
    service.accounts.stop(v['account']['id'], dict(revision=v['account']['revision'], request_id='stop'))
    with pytest.raises(TrainingError, match='结束'):
        service.create(config(market, 'next', previous_session_id=v['id']))
    fresh = service.create(config(market, 'explicit-new'))
    assert fresh['account']['id'] != v['account']['id']
    assert fresh['cash'] == 100000
    assert len(service.accounts.view()['accounts']) == 2


def test_verification_and_replays_keep_separate_identity_and_capital(account_setup):
    service, market = account_setup
    user = service.create(config(market, 'user'))
    check = service.create(config(market, 'verification', verification=True))
    assert check['account']['id'] != user['account']['id']
    assert service.accounts.view(verification=True)['account']['id'] == check['account']['id']
    user = act(service, user, 'finish')
    replay = service.replay(user['id'], dict(alias='A', revision=user['revision'], request_id='replay', continuous_account=True))
    assert replay['account']['id'] == user['account']['id']
    assert replay['cash'] == user['equity']
    assert replay['replay_of'] == user['id']


def test_account_api_empty_errors_and_stop_contract(account_setup):
    service, market = account_setup
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/custom/training/account').json()['account'] is None
        assert client.get('/api/custom/training/account/unknown/records').status_code == 404
        v = service.create(config(market, 'api'))
        records = client.get(f"/api/custom/training/account/{v['account']['id']}/records").json()
        assert records['questions'][0]['id'] == v['id']
        assert records['questions'][0]['finished'] is False
        payload = dict(revision=v['account']['revision'], request_id='api-stop')
        response = client.post(f"/api/custom/training/account/{v['account']['id']}/stop", json=payload)
        assert response.status_code == 200
        assert response.json()['account']['status'] == 'closed'
        assert client.post(f"/api/custom/training/account/{v['account']['id']}/stop", json={**payload, 'revision': -1}).status_code == 422
        exported = client.get(f"/api/custom/training/account/{v['account']['id']}/export")
        assert exported.status_code == 200 and exported.json()['points']
        assert '秘密名称' not in exported.text


def test_two_concurrent_next_questions_cannot_fork_capital(account_setup):
    service, market = account_setup
    v = act(service, service.create(config(market, 'first')), 'finish')
    def create_next(key):
        try:
            return service.create(config(market, key, previous_session_id=v['id']))
        except TrainingError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_next, ['race-one', 'race-two']))
    assert sum(result is not None for result in results) == 1
    assert service.accounts.view()['account']['questions'] == 2


def test_failed_legacy_command_rolls_back_account_adoption(account_setup):
    service, market = account_setup
    cfg = config(market, 'legacy')
    cfg.pop('continuous_account')
    v = service.create(cfg)
    with pytest.raises(TrainingError, match='理由'):
        act(service, v, 'buy', alias='A', reason='', continuous_account=True)
    assert service.accounts.view()['account'] is None
    assert not service.store.load(v['id']).get('account_id')


def test_preparation_started_before_stop_cannot_restart_recording(account_setup):
    service, market = account_setup
    v = service.create(config(market, 'active'))
    late_config = config(market, 'late', account_id=v['account']['id'])
    staged = service.create(late_config, defer_save=True)
    service.accounts.stop(v['account']['id'], dict(revision=v['account']['revision'], request_id='stop'))
    with pytest.raises(TrainingError, match='结束'):
        service.activate_preloaded(staged, late_config)
    with pytest.raises(TrainingError, match='结束'):
        service.create(late_config)
    assert service.accounts.view()['account']['status'] == 'closed'
    assert len(service.accounts.view()['accounts']) == 1


def test_account_detail_lists_questions_even_without_judgments_and_preserves_capital(account_setup):
    service, market = account_setup
    first = service.create(config(market, 'detail-first'))
    first = act(service, first, 'buy', alias='A', reason='原始判断', pattern='平台', size=.5)
    first = act(service, first, 'advance')
    first = act(service, first, 'finish')
    second = service.create(config(market, 'detail-next', previous_session_id=first['id']))
    records = service.accounts.records(first['account']['id'])
    assert [q['id'] for q in records['questions']] == [first['id'], second['id']]
    assert [q['question'] for q in records['questions']] == [1, 2]
    assert records['questions'][0]['finished'] is True
    assert records['questions'][1]['finished'] is False
    assert records['questions'][0]['equity'] == pytest.approx(second['cash'])
    assert records['questions'][0]['total_return'] == pytest.approx(first['total_return'])
    assert records['questions'][0]['strategy'] == '测试量价'
    assert records['events'][0]['reason'] == '原始判断'
    assert '秘密名称' not in json.dumps(records, ensure_ascii=False)
    assert records['stats']['trades'] == 0 and records['stats']['settlements'] == 1
    assert service.view(second['id'])['finished'] is False
