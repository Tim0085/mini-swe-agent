from uuid import uuid4

import pytest

from local_setup.webui.common import STATE
from local_setup.webui.profiles import freeze_profile
from local_setup.webui.queues import BatchCommit
from local_setup.webui.server import manager, queues
from test_library_queues import IDS, queue_client
from test_experiments import experiment, model_profiles


def test_synchronous_dispatch_failure_pauses_and_never_finishes(queue_client):
    _, profiles = model_profiles(queue_client, 1)
    plan = experiment(queue_client, profiles)
    assert plan['ready']
    with manager.lock:
        created = queues.commit(BatchCommit(token=plan['token'], request_id=uuid4().hex))
        queue = queues.records[created['id']]
        # Restore a stage boundary where preparation finished but its local cache was removed.
        item = queue['items'][0]
        item['stage_index'] = 1
        queues.store(queue)
        cache = STATE / 'datasets/verified' / (IDS[0] + '.json')
        assert cache.resolve().is_relative_to(STATE.resolve())
        cache.unlink()
        records_before = set(manager.records)
        queues.tick()
        assert queue['status'] == 'paused' and item['status'] == 'failed'
        assert '缓存' in queue['reason']
        assert not queue['current_run'] and item['attempts'][-1]['run_id'] is None
        queues.tick()
        assert queue['status'] == 'paused' and set(manager.records) == records_before
        assert manager.active is None


@pytest.mark.parametrize(('auto_evaluate',), [(False,), (True,)])
def test_legacy_stale_settings_rejected_before_work_is_created(queue_client, auto_evaluate):
    before_runs, before_queues = set(manager.records), set(queues.records)
    response = queue_client.post('/api/runs', json={'kind': 'swebench', 'dataset': 'verified',
        'instance_id': IDS[0], 'profile_id': 'legacy', 'settings_version': 'stale-view-version',
        'request_id': uuid4().hex, 'auto_evaluate': auto_evaluate})
    assert response.status_code == 409 and '原配置已变化' in response.text
    assert set(manager.records) == before_runs and set(queues.records) == before_queues
    assert manager.active is None


@pytest.mark.parametrize(('auto_evaluate',), [(False,), (True,)])
def test_profile_stale_fingerprint_rejected_before_secret_or_model_access(queue_client, auto_evaluate):
    _, profiles = model_profiles(queue_client, 1)
    profile = profiles[0]
    fingerprint = freeze_profile(profile['id'])['fingerprint']
    changed = queue_client.put('/api/profiles/' + profile['id'], json=profile | {'parameters': {'temperature': .4}})
    assert changed.status_code == 200
    before_runs, before_queues = set(manager.records), set(queues.records)
    response = queue_client.post('/api/runs', json={'kind': 'swebench', 'dataset': 'verified',
        'instance_id': IDS[0], 'profile_id': profile['id'], 'profile_fingerprint': fingerprint,
        'settings_version': queue_client.get('/api/config').json()['version'], 'request_id': uuid4().hex,
        'auto_evaluate': auto_evaluate})
    assert response.status_code == 409 and '模型参数或密钥已变化' in response.text
    assert set(manager.records) == before_runs and set(queues.records) == before_queues
    assert manager.active is None
