import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import training as training_api
from app.services.training import TrainingError
from app.services.training_jobs import TrainingJobs


def wait_for(jobs, job_id, terminal=True):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        view = jobs.get(job_id)
        if (view['status'] in {'complete', 'failed', 'cancelled'}) == terminal:
            return view
        time.sleep(.01)
    pytest.fail('preparation did not reach expected state')


def test_progress_idempotency_cancellation_and_capacity():
    jobs = TrainingJobs()
    blocked, release = threading.Event(), threading.Event()
    calls = []
    def create(config, progress):
        calls.append(config['request_id'])
        progress('loading', 1, 20)
        blocked.set()
        assert release.wait(2)
        progress('saving', 20, 20)
        return {'id': 'anonymous'}
    service = SimpleNamespace(create=create)
    a = jobs.start(service, {'request_id': 'a'})
    assert blocked.wait(1)
    assert jobs.get(a['id'])['completed'] == 1
    assert jobs.start(service, {'request_id': 'a'})['id'] == a['id']
    with pytest.raises(TrainingError):
        jobs.start(service, {'request_id': 'a', 'different': True})
    b = jobs.start(service, {'request_id': 'b'})
    with pytest.raises(TrainingError, match='两个'):
        jobs.start(service, {'request_id': 'c'})
    assert jobs.cancel(a['id'])['status'] == 'cancelling'
    release.set()
    assert wait_for(jobs, a['id'])['status'] == 'cancelled'
    assert wait_for(jobs, b['id'])['result'] == {'id': 'anonymous'}
    assert calls.count('a') == 1
    assert jobs.cancel(b['id'])['status'] == 'complete'


def test_api_returns_job_before_slow_prepare_and_hides_errors(monkeypatch):
    jobs = TrainingJobs()
    monkeypatch.setattr(training_api, '_jobs', jobs)
    release = threading.Event()
    def create(config, progress):
        progress('loading')
        assert release.wait(2)
        raise RuntimeError('000001.SZ 2024-01-01 private native error')
    app = FastAPI()
    app.state.training_service = SimpleNamespace(create=create)
    app.include_router(training_api.router)
    client = TestClient(app)
    response = client.post('/api/custom/training/preparations', json={'request_id': 'slow', 'selection_type': 'random'})
    assert response.status_code == 200
    job = response.json()
    assert job['status'] in {'queued', 'running'}
    assert client.get('/api/custom/training/preparations/' + job['id']).status_code == 200
    release.set()
    failed = wait_for(jobs, job['id'])
    assert failed['status'] == 'failed' and '000001' not in failed['error'] and '2024' not in failed['error']
    assert client.post('/api/custom/training/preparations/missing/cancel').status_code == 404


def test_preload_never_returns_question_and_claim_waits_without_duplicate_work():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    calls = []

    def create(config, progress, defer_save=False):
        calls.append(("build", defer_save))
        progress("loading")
        started.set()
        assert release.wait(2)
        progress("ready")
        return {"id": "private-question", "symbol": "000001.SZ", "date": "2024-01-01"}

    def activate(question, config, progress):
        calls.append(("activate", config["request_id"]))
        return {"id": "claimed"}

    service = SimpleNamespace(create=create, activate_preloaded=activate)
    ready = jobs.start(service, {"request_id": "preload", "selection": "x"}, preload=True)
    assert started.wait(1)
    assert ready["result"] is None
    foreground = jobs.start(
        service, {"request_id": "claim", "selection": "x", "preload_id": ready["id"]}
    )
    assert jobs.get(ready["id"])["result"] is None
    release.set()
    done = wait_for(jobs, foreground["id"])
    assert done["result"] == {"id": "claimed"}
    assert calls == [("build", True), ("activate", "claim")]
    assert (
        jobs.start(service, {"request_id": "claim", "selection": "x", "preload_id": ready["id"]})[
            "id"
        ]
        == foreground["id"]
    )
    assert not jobs.get(ready["id"])["preload_ready"]


@pytest.mark.parametrize("reason", ["settings", "generation", "expired"])
def test_preload_miss_falls_back_safely(reason):
    from app.services.training_jobs import PRELOAD_TTL

    jobs = TrainingJobs()
    calls = []

    def create(config, progress, defer_save=False):
        calls.append(defer_save)
        return {"id": "staged" if defer_save else "fresh"}

    def activate(question, config, progress):
        return None if reason == "generation" else {"id": "claimed"}

    service = SimpleNamespace(create=create, activate_preloaded=activate)
    preload = jobs.start(service, {"request_id": "p", "selection": "x"}, preload=True)
    wait_for(jobs, preload["id"])
    if reason == "expired":
        with jobs.lock:
            jobs.jobs[preload["id"]]["finished"] -= PRELOAD_TTL + 1
        assert not jobs.get(preload["id"])["preload_ready"]
    job = jobs.start(
        service,
        {
            "request_id": "c",
            "selection": "y" if reason == "settings" else "x",
            "preload_id": preload["id"],
        },
    )
    assert wait_for(jobs, job["id"])["result"]["id"] == "fresh"
    assert calls == [True, False]


def test_preload_memory_has_one_ready_question_and_cancel_discards_it():
    jobs = TrainingJobs()
    service = SimpleNamespace(create=lambda config, progress, defer_save=False: {"id": "staged"})
    a = jobs.start(service, {"request_id": "a", "selection": "a"}, preload=True)
    wait_for(jobs, a["id"])
    b = jobs.start(service, {"request_id": "b", "selection": "b"}, preload=True)
    wait_for(jobs, b["id"])
    assert not jobs.get(a["id"])["preload_ready"] and jobs.get(b["id"])["preload_ready"]
    jobs.cancel(b["id"])
    assert not jobs.get(b["id"])["preload_ready"]


def test_preload_api_does_not_expose_private_future_or_stock(monkeypatch):
    import json

    jobs = TrainingJobs()
    monkeypatch.setattr(training_api, "_jobs", jobs)
    service = SimpleNamespace(
        create=lambda config, progress, defer_save=False: {
            "id": "hidden",
            "symbol": "000001.SZ",
            "future": "2026-12-12",
        }
    )
    app = FastAPI()
    app.state.training_service = service
    app.include_router(training_api.router)
    client = TestClient(app)
    response = client.post(
        "/api/custom/training/preloads", json={"request_id": "p", "selection_type": "random"}
    )
    assert response.status_code == 200
    j = wait_for(jobs, response.json()["id"])
    assert j["preload_ready"] and j["result"] is None
    exposed = client.get("/api/custom/training/preparations/" + j["id"]).json()
    assert "000001" not in json.dumps(exposed) and "2026-12-12" not in json.dumps(exposed)


def test_cancelled_claim_releases_preload_for_retry():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    calls = []
    def create(config, progress, defer_save=False):
        calls.append(defer_save)
        started.set()
        assert release.wait(2)
        progress('ready')
        return {'id': 'private'}
    service = SimpleNamespace(create=create, activate_preloaded=lambda question, config, progress: {'id':'claimed'})
    preload = jobs.start(service, {'request_id':'preload'}, preload=True)
    assert started.wait(1)
    first = jobs.start(service, {'request_id':'first', 'preload_id':preload['id']})
    jobs.cancel(first['id'])
    assert wait_for(jobs, first['id'])['status'] == 'cancelled'
    release.set()
    assert wait_for(jobs, preload['id'])['preload_ready']
    second = jobs.start(service, {'request_id':'second', 'preload_id':preload['id']})
    assert wait_for(jobs, second['id'])['result']['id'] == 'claimed'
    assert calls == [True]

def test_same_settings_preloads_reuse_running_ready_and_keep_request_identity():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    builds = []
    def create(config, progress, defer_save=False):
        builds.append(config['request_id'])
        started.set()
        assert release.wait(2)
        progress('ready')
        return {'id': 'private'}
    service = SimpleNamespace(create=create)
    first = jobs.start(service, {'request_id': 'p1', 'indicators': ['vol']}, preload=True)
    assert started.wait(1)
    second_config = {'request_id': 'p2', 'indicators': ['vol', 'macd']}
    second = jobs.start(service, second_config, preload=True)
    assert second['id'] == first['id']
    release.set()
    assert wait_for(jobs, first['id'])['preload_ready']
    third = jobs.start(service, {'request_id': 'p3', 'indicators': ['rsi']}, preload=True)
    assert third['id'] == first['id'] and third['preload_ready']
    assert builds == ['p1']
    with pytest.raises(TrainingError, match='相同请求'):
        jobs.start(service, {**second_config, 'horizon': 120}, preload=True)


def test_busy_preload_waits_then_claims_without_deadlock_or_second_build():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    builds = []
    def create(config, progress, defer_save=False):
        builds.append((config['request_id'], defer_save))
        if not defer_save:
            started.set()
            assert release.wait(2)
        progress('ready')
        return {'id': 'private' if defer_save else 'existing'}
    service = SimpleNamespace(create=create, activate_preloaded=lambda *a, **k: {'id': 'claimed'})
    foreground = jobs.start(service, {'request_id': 'foreground'})
    assert started.wait(1)
    preload = jobs.start(service, {'request_id': 'preload'}, preload=True)
    assert preload['status'] == 'queued'
    claim = jobs.start(service, {'request_id': 'claim', 'preload_id': preload['id']})
    assert builds == [('foreground', False)]
    release.set()
    assert wait_for(jobs, foreground['id'])['status'] == 'complete'
    assert wait_for(jobs, claim['id'])['result'] == {'id': 'claimed'}
    assert builds == [('foreground', False), ('preload', True)]


def test_latest_queued_preload_replaces_obsolete_context_without_extra_io():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    builds = []
    def create(config, progress, defer_save=False):
        builds.append(config['request_id'])
        if config['request_id'] == 'first':
            started.set()
            assert release.wait(2)
        progress('ready')
        return {'id': 'private'}
    service = SimpleNamespace(create=create)
    first = jobs.start(service, {'request_id': 'first', 'selection': 'a'}, preload=True)
    assert started.wait(1)
    obsolete = jobs.start(service, {'request_id': 'obsolete', 'selection': 'b'}, preload=True)
    latest = jobs.start(service, {'request_id': 'latest', 'selection': 'c'}, preload=True)
    release.set()
    assert wait_for(jobs, first['id'])['status'] == 'cancelled'
    assert wait_for(jobs, obsolete['id'])['status'] == 'cancelled'
    assert wait_for(jobs, latest['id'])['preload_ready']
    assert builds == ['first', 'latest']


def test_preload_revision_change_rebuilds_instead_of_reusing_stale_ready_data():
    jobs = TrainingJobs()
    revision, builds = ['old'], []
    def create(config, progress, defer_save=False):
        builds.append(config['request_id'])
        return {'id': 'private', 'revision': revision[0]}
    service = SimpleNamespace(create=create,
        preload_revision=lambda config, session=None: session['revision'] if session else revision[0])
    first = jobs.start(service, {'request_id': 'first'}, preload=True)
    wait_for(jobs, first['id'])
    revision[0] = 'new'
    second = jobs.start(service, {'request_id': 'second'}, preload=True)
    assert wait_for(jobs, second['id'])['preload_ready']
    assert first['id'] != second['id'] and builds == ['first', 'second']


def test_new_background_context_does_not_cancel_a_claimed_preload():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    builds = []
    def create(config, progress, defer_save=False):
        builds.append(config['request_id'])
        if config['request_id'] == 'old':
            started.set()
            assert release.wait(2)
        progress('ready')
        return {'id': 'private'}
    service = SimpleNamespace(create=create, activate_preloaded=lambda *a, **k: {'id': 'claimed'})
    old = jobs.start(service, {'request_id': 'old', 'selection': 'old'}, preload=True)
    assert started.wait(1)
    claim = jobs.start(service, {'request_id': 'claim', 'selection': 'old', 'preload_id': old['id']})
    new = jobs.start(service, {'request_id': 'new', 'selection': 'new'}, preload=True)
    assert new['status'] == 'queued'
    release.set()
    assert wait_for(jobs, claim['id'])['result'] == {'id': 'claimed'}
    assert wait_for(jobs, new['id'])['preload_ready']
    assert builds == ['old', 'new']


def test_reused_preload_request_aliases_are_bounded():
    from app.services.training_jobs import MAX_REQUEST_ALIASES
    jobs = TrainingJobs()
    service = SimpleNamespace(create=lambda *a, **k: {'id': 'private'})
    first = jobs.start(service, {'request_id': 'first'}, preload=True)
    wait_for(jobs, first['id'])
    for index in range(MAX_REQUEST_ALIASES + 2):
        assert jobs.start(service, {'request_id': f'alias-{index}'}, preload=True)['id'] == first['id']
    assert len(jobs.aliases) == MAX_REQUEST_ALIASES and len(jobs.jobs) == 1


def test_obsolete_queued_contexts_do_not_exceed_job_registry_limit():
    jobs = TrainingJobs()
    started, release = threading.Event(), threading.Event()
    def create(config, progress, defer_save=False):
        if not defer_save:
            started.set()
            assert release.wait(5)
        progress('ready')
        return {'id': 'private'}
    service = SimpleNamespace(create=create)
    jobs.start(service, {'request_id': 'foreground'})
    assert started.wait(1)
    try:
        for index in range(80):
            latest = jobs.start(service, {'request_id': f'queued-{index}', 'selection': index}, preload=True)
            assert len(jobs.jobs) <= 64
    finally:
        release.set()
    assert wait_for(jobs, latest['id'])['preload_ready']
