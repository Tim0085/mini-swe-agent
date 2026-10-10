import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from local_setup.webui.server import NONCE, app, manager
from local_setup.webui.tests.test_profiles import local_service


@pytest.fixture
def api_client():
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.post('/session', json={'nonce': NONCE}).status_code == 200
        yield client
        if manager.active:
            client.post('/api/runs/' + manager.active + '/cancel', json={})
        deadline = time.monotonic() + 20
        while manager.active and time.monotonic() < deadline:
            time.sleep(.1)
        assert manager.active is None and not getattr(manager, 'tool_testing', False)


def create(client, base, protocol='chat'):
    response = client.post('/api/connections', json={'name': 'API 本机测试连接', 'api_base': base})
    assert response.status_code == 200, response.text
    connection = response.json()
    response = client.put(f"/api/connections/{connection['id']}/key", json={'key': 'api-fixture-secret', 'version': connection['version']})
    assert response.status_code == 200 and 'api-fixture-secret' not in response.text
    connection = response.json()
    response = client.post('/api/profiles', json={'name': 'API 工具测试档案', 'connection_id': connection['id'], 'model_name': 'alias-for-any-model',
                                                'protocol': protocol, 'pricing': {'mode': 'custom', 'input_per_million': 2, 'output_per_million': 6}})
    assert response.status_code == 200, response.text
    return connection, response.json()


def preview(client, profile):
    response = client.post(f"/api/profiles/{profile['id']}/test/preview", json={})
    assert response.status_code == 200, response.text
    assert response.json()['steps'] == 2 and response.json()['max_output_tokens'] == 256
    return {'token': response.json()['token'], 'request_id': uuid4().hex}


@pytest.mark.parametrize(('protocol',), [('chat',), ('responses',)])
def test_probe_api_preview_commit_idempotency_and_auth(api_client, protocol):
    with local_service() as (base, requests):
        connection, profile = create(api_client, base, protocol)
        models = api_client.get(f"/api/connections/{connection['id']}/models")
        assert models.status_code == 200 and 'alias-for-any-model' in models.json()['models']
        body = preview(api_client, profile)
        endpoint = f"/api/profiles/{profile['id']}/test"
        first = api_client.post(endpoint, json=body)
        assert first.status_code == 200 and first.json()['status'] == 'completed', first.text
        assert first.json()['result']['status'] == 'passed' and first.json()['result']['calls'] == 2
        assert api_client.post(endpoint, json=body).json() == first.json() and len(requests) == 2
        assert 'api-fixture-secret' not in json.dumps(api_client.get('/api/profiles').json())
        listed = api_client.get('/api/profiles').json()['profiles']
        assert next(p for p in listed if p['id'] == profile['id'])['test_current']
        api_client.cookies.clear()
        assert api_client.get('/api/profiles').status_code == 401
        assert api_client.put(f"/api/connections/{connection['id']}/key", json={'key': 'never-save', 'version': connection['version']}).status_code == 401
        assert api_client.post(endpoint, json=body).status_code == 401 and len(requests) == 2


def test_probe_api_snapshot_changes_block_requests(api_client):
    with local_service() as (base, requests):
        connection, profile = create(api_client, base)
        body = preview(api_client, profile)
        endpoint = f"/api/profiles/{profile['id']}/test"
        updated = api_client.put('/api/profiles/' + profile['id'], json=profile | {'parameters': {'temperature': .2}})
        assert updated.status_code == 200
        assert api_client.post(endpoint, json=body).status_code == 400 and not requests
        body = preview(api_client, updated.json())
        rotated = api_client.put(f"/api/connections/{connection['id']}/key", json={'key': 'api-fixture-secret-rotated', 'version': connection['version']})
        assert rotated.status_code == 200
        assert api_client.post(endpoint, json=body).status_code == 400 and not requests


def test_probe_api_holds_execution_slot_and_rejects_lifecycle(api_client, tmp_path):
    entered, release = threading.Event(), threading.Event()
    with local_service(entered, release) as (base, requests):
        _, profile = create(api_client, base)
        endpoint = f"/api/profiles/{profile['id']}/test"
        body = preview(api_client, profile)
        other = preview(api_client, profile)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api_client.post, endpoint, json=body)
            try:
                assert entered.wait(15)
                assert api_client.get('/api/environment').json()['tool_testing']
                repeated = api_client.post(endpoint, json=body)
                assert repeated.status_code == 200 and repeated.json()['status'] == 'running'
                assert api_client.post(endpoint, json=other).status_code == 400
                version = api_client.get('/api/config').json()['version']
                assert api_client.post('/api/runs', json={'kind': 'offline', 'cwd': str(tmp_path), 'task': 'offline', 'settings_version': version, 'request_id': uuid4().hex}).status_code == 409
                assert api_client.post('/api/lifecycle', json={'action': 'restart', 'mode': 'when_idle'}).status_code == 409
                assert len(requests) == 1
            finally:
                release.set()
            result = future.result(timeout=20)
            assert result.status_code == 200 and result.json()['result']['status'] == 'passed'
        assert len(requests) == 2 and not manager.tool_testing


def test_probe_api_waits_for_existing_offline_task(api_client, tmp_path):
    with local_service() as (base, requests):
        _, profile = create(api_client, base)
        body = preview(api_client, profile)
        version = api_client.get('/api/config').json()['version']
        run = api_client.post('/api/runs', json={'kind': 'offline', 'cwd': str(tmp_path), 'task': 'offline', 'settings_version': version, 'request_id': uuid4().hex})
        assert run.status_code == 200, run.text
        assert api_client.post(f"/api/profiles/{profile['id']}/test", json=body).status_code == 400
        assert not requests
