import json
import threading
import time

import pytest

from app import cli
from app.services.data_sync import DataSync
from app.main import create_app
from fastapi.testclient import TestClient


def test_sync_is_single_flight_and_preserves_user_data(tmp_path, monkeypatch):
    source, root = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    root.mkdir()
    account = root / 'account.db'
    account.write_bytes(b'unchanged')
    started, release = threading.Event(), threading.Event()

    def fake_import(path, target):
        assert path == source.resolve() and target == root.resolve()
        started.set()
        assert release.wait(5)
        return {'symbols': 20, 'start': '2020-01-01', 'end': '2025-01-01'}

    monkeypatch.setattr(cli, 'import_tsp_market', fake_import, raising=False)
    service = DataSync(root)
    assert service.start(str(source))['status'] == 'running'
    assert started.wait(5)
    with pytest.raises(RuntimeError, match='同步任务'):
        service.start(str(source))
    release.set()
    for _ in range(100):
        if service.view()['status'] == 'complete':
            break
        time.sleep(.01)
    assert service.view()['result']['symbols'] == 20
    assert account.read_bytes() == b'unchanged'
    assert json.loads((root / 'sync-settings.json').read_text(encoding='utf-8'))['source'] == str(source.resolve())


def test_sync_rejects_bad_or_overlapping_paths(tmp_path):
    root = tmp_path / 'target'
    root.mkdir()
    service = DataSync(root)
    for path in ('', 'relative/path', str(tmp_path), str(root), str(tmp_path / 'missing')):
        with pytest.raises(ValueError):
            service.start(path)


def test_sync_api_rejects_cross_origin_requests(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/data-sync').json()['status'] == 'idle'
        response = client.post('/api/data-sync', json={'source': str(tmp_path)}, headers={'origin': 'https://example.com'})
        assert response.status_code == 403
