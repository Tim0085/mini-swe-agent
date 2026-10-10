import json
from uuid import uuid4

import pytest

from local_setup.webui.bench import prediction, valid_instance
from local_setup.webui.benchmarks import PRO_ID, case_key, snapshot
from local_setup.webui.common import STATE, read_json, save_json
from local_setup.webui.patches import inspect_patch
from local_setup.webui.queues import BatchCommit
from local_setup.webui.results import Results
from local_setup.webui.server import manager, queues
from test_library_queues import queue_client, wait_queue
from test_pro_cache_identity import cached_case
from test_pro_data import INSTANCE
from test_results import run, evaluation

PATCH = 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n'


def test_pro_registry_input_and_frozen_queue_recovery(queue_client):
    client = queue_client
    cached_case()
    registry = client.get('/api/benchmarks').json()['items']
    assert [row['id'] for row in registry][:2] == ['verified', 'lite']
    spec = next(row for row in registry if row['id'] == PRO_ID)
    assert spec['expected_count'] == 642 and spec['agent_network'] == 'none'
    assert valid_instance(INSTANCE, PRO_ID) == INSTANCE
    with pytest.raises(ValueError):
        valid_instance(INSTANCE, 'verified')
    for invalid in ('instance_../private', 'instance_bad..path', INSTANCE + '/extra'):
        with pytest.raises(ValueError):
            valid_instance(invalid, PRO_ID)
    options = client.get('/api/instance-options', params={'dataset': PRO_ID}).json()
    assert options['items'][0]['instance_id'] == INSTANCE
    assert client.get('/api/library', params={'dataset': PRO_ID}).json()['items'][0]['data_cached']
    plan = client.post('/api/queues/preview', json={
        'dataset': PRO_ID, 'instance_ids': [INSTANCE], 'operation': 'solve',
        'settings_version': client.get('/api/config').json()['version'],
        'diagnostics': {INSTANCE: 'success'}, 'auto_evaluate': True, 'auto_approve': True,
        'allow_data_download': False, 'allow_image_download': False, 'total_budget': 1}).json()
    assert plan['ready'], plan['issues']
    body = {'token': plan['token'], 'request_id': uuid4().hex}
    with manager.lock:
        started = queues.commit(BatchCommit(**body))
        queues.recover()
        queues.tick()
    assert queues.view(queues.get(started['id']))['status'] == 'paused'
    assert not queues.get(started['id'])['current_run']
    assert client.post('/api/queues', json=body).json()['id'] == started['id']
    assert client.post('/api/queues/' + started['id'] + '/control', json={'action': 'resume'}).status_code == 200
    done = wait_queue(client, started['id'], lambda q: q['status'] == 'completed')
    attempts = done['items'][0]['attempts']
    assert [attempt['stage'] for attempt in attempts] == ['download', 'solve', 'evaluate']
    assert done['benchmark_snapshot'] == snapshot(PRO_ID)
    assert done['benchmark_snapshot']['execution_config']['agent_network'] == 'none'
    for attempt in attempts:
        request = read_json(STATE / 'runs' / attempt['run_id'] / 'request.json')
        assert request['benchmark_snapshot'] == snapshot(PRO_ID)
        assert request['case_key'] == case_key(PRO_ID, INSTANCE)
        assert not request['allow_data_download'] and not request['allow_image_download']
    solve, evaluated = (manager.get(attempt['run_id']) for attempt in attempts[1:])
    assert evaluated['solve_run_id'] == solve['id']
    assert read_json(STATE / 'runs' / solve['id'] / 'predictions.jsonl') == read_json(STATE / 'runs' / evaluated['id'] / 'input.predictions.jsonl')
    assert len(client.post('/api/queues', json=body).json()['items'][0]['attempts']) == 3
    assert solve['id'] not in client.get('/api/results/export').text


@pytest.mark.parametrize(('reason',), [('LimitsExceeded',), ('TimeExceeded',), ('ConnectionError',)])
def test_partial_patch_preserves_exit_and_association_without_mixing_benchmarks(tmp_path, reason):
    capture = {'source': 'container_git_diff', 'partial': True, 'captured': True, 'reason': reason}
    identity = {'dataset': PRO_ID, 'benchmark_snapshot': snapshot(PRO_ID), 'case_key': case_key(PRO_ID, INSTANCE)}
    path = tmp_path / 'partial.traj.json'
    save_json(path, identity | {'instance_id': INSTANCE, 'info': {'exit_status': reason, 'submission': PATCH,
        'patch_capture': capture, 'config': {'model': {'model_name': 'openai/offline-pro'}}}})
    pred = prediction(path, INSTANCE)
    assert pred['model_patch'] == PATCH and pred['benchmark_snapshot'] == snapshot(PRO_ID)
    assert inspect_patch(path, INSTANCE, dataset=PRO_ID)['usable']
    assert not inspect_patch(path, INSTANCE, dataset='verified')['usable']
    assert read_json(path)['info']['exit_status'] == reason
    solve = run(21, **identity, instance_id=INSTANCE, prediction=str(path), result={'exit_status': reason, 'patch_capture': capture})
    pending = Results([solve], run_root=tmp_path).for_run(solve['id'])
    assert pending['patch_capture']['reason'] == reason and pending['verdict'] == '部分补丁，待评测'
    first = evaluation(22, solve, False, **identity)
    second = evaluation(23, solve, True, **identity)
    index = Results([solve, first, second, evaluation(24, run(20))], run_root=tmp_path)
    result = index.for_run(solve['id'])
    assert result['status'] == 'passed' and result['patch_capture']['partial']
    assert result['evaluation_run_ids'] == [first['id'], second['id']]
    assert index.listing()['summary']['resolution_rate'] is None and index.compare()['requires_dataset']
    assert index.listing(dataset=PRO_ID)['summary']['denominator'] == 1
    bad = solve | {'benchmark_snapshot': snapshot(PRO_ID) | {'evaluator_revision': 'wrong'}}
    assert Results([bad], run_root=tmp_path).for_run(solve['id'])['status'] == 'unknown'
    unsafe = read_json(path)
    unsafe['info']['patch_capture']['source'] = 'model_text'
    save_json(path, unsafe)
    with pytest.raises(ValueError, match='没有成功提交'):
        prediction(path, INSTANCE)
