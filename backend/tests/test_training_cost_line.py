import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_training import Market, act

from app.custom.training import router
from app.services.training import TrainingService, TrainingStore


@pytest.fixture
def trainer(tmp_path):
    return TrainingService(TrainingStore(tmp_path), Market())


@pytest.mark.parametrize('mode', ['close_t', 'open_t+1'])
def test_cost_price_matches_adjusted_equity_and_only_exists_after_fill(trainer, mode):
    service = trainer
    view = service.create(dict(strategy_ids=['test'], mode=mode, horizon=20,
                               candidate_count=1, auto_scan=False, request_id=mode))
    assert view['position'] is None
    view = act(service, view, 'buy', alias='A', reason='cost-line validation')
    if mode == 'open_t+1':
        assert view['position'] is None and view['pending']
        view = act(service, view, 'advance')
    state = service.store.load(view['id'])
    pos = state['position']
    expected = pos['cost']/pos['adjusted_units']
    assert view['position']['cost_price'] == pytest.approx(expected)
    assert expected > view['position']['entry_price']
    latest = view['candidates'][0]['bars'][-1]['close']
    assert latest/expected-1 == pytest.approx(view['position']['pnl'])
    view = act(service, view, 'advance', step_delta=True, price_unit='cny_qfq')
    assert view['position']['cost_price'] == pytest.approx(expected)
    view = act(service, view, 'sell', alias='A', reason='cost-line close')
    if mode == 'open_t+1':
        assert view['position']['cost_price'] == pytest.approx(expected)
        view = act(service, view, 'advance')
    assert view['position'] is None


def test_legacy_holding_cost_stays_on_adjusted_basis_across_exrights(trainer):
    service = trainer
    view = service.create(dict(strategy_ids=['test'], horizon=20, candidate_count=1,
                               auto_scan=False, request_id='legacy'))
    view = act(service, view, 'buy', alias='A', reason='legacy cost')
    state = copy.deepcopy(service.store.load(view['id']))
    expected = state['position']['cost']/state['position'].pop('adjusted_units')
    for row in state['assets']['A']['rows']:
        if row['date'] >= state['dates'][1]:
            row['raw_close'] = row['close']/2
            row['raw_high'] = row['high']/2
            row['raw_low'] = row['low']/2
    service.store.replace_for_test(state)
    advanced = act(service, view, 'advance', step_delta=True, price_unit='cny_qfq')
    assert advanced['position']['cost_price'] == pytest.approx(expected)
    assert service.view(view['id'])['position']['cost_price'] == pytest.approx(expected)


def test_cost_price_is_in_resume_api_without_revealing_identity(trainer):
    service = trainer
    view = service.create(dict(strategy_ids=['test'], horizon=20, candidate_count=1,
                               auto_scan=False, request_id='api'))
    view = act(service, view, 'buy', alias='A', reason='API cost')
    app = FastAPI()
    app.state.training_service = service
    app.include_router(router)
    response = TestClient(app).get(f"/api/custom/training/sessions/{view['id']}")
    assert response.status_code == 200
    assert response.json()['position']['cost_price'] == view['position']['cost_price']
    assert '000001.SZ' not in response.text
