import json
from uuid import uuid4

import pytest

from local_setup.webui.bench import dataset_file, image_name, load_instance
from local_setup.webui.benchmarks import REBENCH_ID, REGISTRY, case_key, snapshot
from local_setup.webui.common import CONFIG_ROOT, STATE, read_json, save_json
from local_setup.webui.library import public_row
from local_setup.webui.queues import BatchCommit
from local_setup.webui.server import manager, queues
from test_library_queues import IDS, queue_client, wait_queue

INSTANCE = IDS[27]
INTERFACE_INSTANCE = 'audit__fixture-1031_interface'
PATCH = 'diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new\n'


@pytest.fixture
def rebench_client(queue_client):
    rows = []
    for index, instance in enumerate((INSTANCE, INTERFACE_INSTANCE)):
        raw = {'instance_id': instance, 'repo': 'audit/fixture', 'base_commit': 'a' * 40, 'version': '1',
               'problem_statement': 'Repair the offline benchmark fixture ' + str(index),
               'patch': PATCH, 'test_patch': 'REFERENCE_TEST_PATCH_SENTINEL',
               'FAIL_TO_PASS': ['test_repair'], 'PASS_TO_PASS': ['test_regression'],
               'docker_image': 'swerebench/sweb.eval.x86_64.audit_1776_fixture-1031:latest',
               'install_config': {'log_parser': 'parse_log_pytest', 'test_cmd': 'pytest -rA', 'python': '3.12',
                                  'packages': '', 'install': 'python -m pip install -e .', 'docker_specs': {}},
               'dataset': REBENCH_ID, 'benchmark_snapshot': snapshot(REBENCH_ID), 'case_key': case_key(REBENCH_ID, instance)}
        save_json(dataset_file(REBENCH_ID, instance), [raw])
        rows.append(public_row(raw, index, REBENCH_ID))
    save_json(STATE / 'catalog' / (REBENCH_ID + '.json'),
              {'dataset': REBENCH_ID, 'benchmark_snapshot': snapshot(REBENCH_ID), 'items': rows,
               'synced_at': '2026-10-08T00:00:00+00:00', 'note': 'Local protocol fixtures; no source download'})
    yield queue_client


def plan(client, **changes):
    response = client.post('/api/queues/preview', json={
        'dataset': REBENCH_ID, 'instance_ids': [INSTANCE], 'operation': 'solve',
        'settings_version': client.get('/api/config').json()['version'],
        'auto_evaluate': True, 'auto_approve': True, 'total_budget': 1,
        'allow_data_download': False, 'allow_image_download': False,
        'diagnostics': {INSTANCE: 'success'}, **changes})
    assert response.status_code == 200, response.text
    assert response.json()['ready'], response.json()['issues']
    return response.json()


def test_registered_fixed_snapshot_local_catalog_and_old_default_api(rebench_client):
    client = rebench_client
    choices = client.get('/api/benchmarks')
    assert choices.status_code == 200
    registered = {item['id']: item for item in choices.json()['items']}
    assert {'verified', 'lite', REBENCH_ID} <= set(registered)
    assert registered[REBENCH_ID]['source_revision'] == REGISTRY[REBENCH_ID]['source_revision']
    page = client.get('/api/library', params={'dataset': REBENCH_ID}).json()
    assert page['total'] == 2 and all(row['data_cached'] for row in page['items'])
    assert all(row['image'].startswith('swerebench/') for row in page['items'])
    assert all('patch' not in row and 'test_patch' not in row for row in page['items'])
    options = client.get('/api/instance-options', params={'dataset': REBENCH_ID, 'search': '_interface'}).json()
    assert options['total'] == 1 and options['items'][0]['instance_id'] == INTERFACE_INSTANCE
    detail = client.get('/api/library/' + REBENCH_ID + '/' + INTERFACE_INSTANCE)
    assert detail.status_code == 200 and detail.json()['data_cached']
    assert 'REFERENCE_TEST_PATCH_SENTINEL' not in detail.text
    assert client.get('/api/library').json()['total'] == len(IDS)
    assert client.get('/api/instance-options').json()['total'] == len(IDS)
    assert client.get('/api/library', params={'dataset': 'rebench-floating-latest'}).status_code == 422
    denied = client.post('/api/library/sync', json={'dataset': REBENCH_ID, 'allow_network': False, 'request_id': uuid4().hex})
    assert denied.status_code == 400 and not manager.active


def test_cached_case_identity_is_checked_without_overwriting_data(rebench_client):
    path = dataset_file(REBENCH_ID, INSTANCE)
    original = path.read_bytes()
    row = load_instance(REBENCH_ID, INSTANCE)
    assert row['case_key'] == case_key(REBENCH_ID, INSTANCE)
    assert row['benchmark_snapshot'] == snapshot(REBENCH_ID)
    assert image_name(row, REBENCH_ID).startswith('swerebench/')
    with pytest.raises(ValueError, match='尚未缓存'):
        load_instance(REBENCH_ID, 'audit__fixture-99999', download=False)
    try:
        for changed in ({'dataset': 'verified'}, {'case_key': 'verified:' + INSTANCE},
                        {'benchmark_snapshot': {**snapshot(REBENCH_ID), 'source_revision': 'f' * 40}}):
            save_json(path, [row | changed])
            before = path.read_bytes()
            with pytest.raises(ValueError, match='身份|版本'):
                load_instance(REBENCH_ID, INSTANCE)
            assert path.read_bytes() == before
    finally:
        path.write_bytes(original)
    assert load_instance('verified', INSTANCE)['instance_id'] == INSTANCE


def test_patch_routes_and_library_badges_share_strict_benchmark_identity(rebench_client):
    client = rebench_client
    folder = CONFIG_ROOT / '.local'
    prefix = 'rebench-integration-' + uuid4().hex
    legacy = folder / (prefix + '-legacy.predictions.jsonl')
    current = folder / (prefix + '-current.predictions.jsonl')
    base = {'instance_id': INSTANCE, 'model_name_or_path': 'openai/offline-test', 'model_patch': PATCH}
    legacy.write_text(json.dumps(base) + '\n', encoding='utf-8')
    try:
        assert not client.get('/api/library', params={'dataset': REBENCH_ID, 'search': INSTANCE}).json()['items'][0]['has_patch']
        wrong = client.post('/api/patches/inspect', json={'path': str(legacy), 'instance_id': INSTANCE, 'dataset': REBENCH_ID})
        assert wrong.status_code == 200 and not wrong.json()['usable'] and '基准版本' in wrong.json()['reason']
        assert client.post('/api/patches/inspect', json={'path': str(legacy), 'instance_id': INSTANCE}).json()['usable']
        assert client.get('/api/patches', params={'instance_id': INSTANCE}).json()['items']
        identity = {'dataset': REBENCH_ID, 'benchmark_snapshot': snapshot(REBENCH_ID), 'case_key': case_key(REBENCH_ID, INSTANCE)}
        current.write_text(json.dumps(base | identity) + '\n', encoding='utf-8')
        choices = client.get('/api/patches', params={'dataset': REBENCH_ID, 'instance_id': INSTANCE}).json()
        assert len(choices['items']) == 1 and choices['recommended'] == str(current)
        assert client.get('/api/library', params={'dataset': REBENCH_ID, 'search': INSTANCE}).json()['items'][0]['has_patch']
        detail = client.get('/api/library/' + REBENCH_ID + '/' + INSTANCE).json()
        assert detail['has_patch'] and detail['patches']['recommended'] == str(current)
        assert client.post('/api/patches/inspect', json={'path': str(current), 'instance_id': INSTANCE, 'dataset': 'verified'}).json()['usable'] is False
    finally:
        legacy.unlink(missing_ok=True)
        current.unlink(missing_ok=True)


def test_frozen_queue_recovers_paused_then_runs_three_diagnostic_stages_once(rebench_client):
    client = rebench_client
    preview = plan(client)
    body = {'token': preview['token'], 'request_id': uuid4().hex}
    with manager.lock:
        created = queues.commit(BatchCommit(**body))
        queues.recover()
        queues.tick()
        recovered = queues.view(queues.get(created['id']))
    assert recovered['status'] == 'paused' and not recovered['current_run']
    assert recovered['benchmark_snapshot'] == snapshot(REBENCH_ID)
    assert not recovered['items'][0]['attempts']
    repeated = client.post('/api/queues', json=body)
    assert repeated.status_code == 200 and repeated.json()['id'] == created['id']
    assert client.post('/api/queues/' + created['id'] + '/control', json={'action': 'resume'}).status_code == 200
    done = wait_queue(client, created['id'], lambda row: row['status'] == 'completed')
    attempts = done['items'][0]['attempts']
    assert [attempt['stage'] for attempt in attempts] == ['download', 'solve', 'evaluate']
    assert len({attempt['run_id'] for attempt in attempts}) == 3
    assert done['items'][0]['evaluation']['category'] == 'passed'
    for attempt in attempts:
        meta = manager.get(attempt['run_id'])
        request = read_json(STATE / 'runs' / attempt['run_id'] / 'request.json')
        assert meta['diagnostic'] and not request['allow_data_download'] and not request['allow_image_download']
        for value in (meta, request):
            assert value['dataset'] == REBENCH_ID and value['case_key'] == case_key(REBENCH_ID, INSTANCE)
            assert value['benchmark_snapshot'] == snapshot(REBENCH_ID)
    solve_id, evaluation_id = attempts[1]['run_id'], attempts[2]['run_id']
    assert manager.get(evaluation_id)['solve_run_id'] == solve_id
    predictions = read_json(STATE / 'runs' / solve_id / 'predictions.jsonl')
    evaluated = read_json(STATE / 'runs' / evaluation_id / 'input.predictions.jsonl')
    assert predictions == evaluated and predictions['case_key'] == case_key(REBENCH_ID, INSTANCE)
    assert client.post('/api/queues', json=body).json()['id'] == created['id']
    assert len(client.get('/api/queues/' + created['id']).json()['items'][0]['attempts']) == 3
    assert all(attempt['run_id'] not in client.get('/api/results/export').text for attempt in attempts)


def test_changed_preview_snapshot_cannot_create_a_batch(rebench_client):
    preview = plan(rebench_client)
    with manager.lock:
        queues.previews[preview['token']]['benchmark_snapshot']['source_revision'] = 'f' * 40
    denied = rebench_client.post('/api/queues', json={'token': preview['token'], 'request_id': uuid4().hex})
    assert denied.status_code == 400 and '版本' in denied.text
    assert not manager.active and queues.unfinished() is None
