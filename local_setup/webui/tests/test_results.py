import json
import csv
import io

import pytest

from local_setup.webui.results import Results


def run(number, kind='swebench', **fields):
    return {'id': f'20260920-120000-{number:08x}', 'kind': kind, 'status': 'completed', 'title': 'Result fixture',
            'dataset': 'verified', 'instance_id': 'astropy__astropy-13033', 'model': 'openai/test-model',
            'created': f'2026-09-20T12:{number:02d}:00+00:00', 'finished': f'2026-09-20T12:{number:02d}:10+00:00',
            'patch_hash': 'patch-a', 'auto_approve': False, 'profile_snapshot': {'profile_id': 'one', 'profile_name': 'Model A',
            'model_name': 'openai/test-model', 'api_base': 'https://private.example/v1',
            'limits': {'step_limit': 40, 'cost_limit': 3}}, 'stats': {'calls': 2, 'cost': .12},
            'result': {'exit_status': 'Submitted'}, **fields}


def evaluation(number, source, passed=True, **fields):
    return run(number, 'evaluate', solve_run_id=source['id'], model=source['model'], instance_id=source['instance_id'],
               patch_hash=source.get('patch_hash'),
               profile_snapshot=fields.pop('profile_snapshot', source.get('profile_snapshot', {})),
               evaluation={'category': 'passed' if passed else 'test_failed', 'resolved': int(passed), 'completed': 1,
                           'detail': {source['instance_id']: {'tests_status': {
                               'FAIL_TO_PASS': {'success': ['repair'] if passed else [], 'failure': [] if passed else ['repair']},
                                'PASS_TO_PASS': {'success': [str(i) for i in range(20)], 'failure': []}}}}}, **fields)


@pytest.mark.parametrize(('source_kind',), [('record',), ('request',), ('path',), ('attempt',)])
def test_missing_source_never_reassigns_evaluation_to_identical_patch(tmp_path, source_kind):
    removed, remaining = run(1, attempt_id='removed'), run(2, attempt_id='remaining')
    assessed = evaluation(3, removed)
    if source_kind != 'record':
        assessed.pop('solve_run_id')
    if source_kind == 'request':
        folder = tmp_path / assessed['id']
        folder.mkdir()
        (folder / 'request.json').write_text(json.dumps({'solve_run_id': removed['id']}), encoding='utf-8')
    if source_kind == 'path':
        assessed['patch_source'] = {'path': str(tmp_path / removed['id'] / 'predictions.jsonl')}
    if source_kind == 'attempt':
        assessed['attempt_id'] = removed['attempt_id']
    result = Results([remaining, assessed], run_root=tmp_path)
    assert result.for_run(remaining['id'])['status'] == 'submitted'
    assert result.for_run(assessed['id'])['association'] == 'evaluation_only'
    assert result.for_run(assessed['id'])['cost']['estimated'] is None
    assert result.for_run(assessed['id'])['evaluation_run_ids'] == [assessed['id']]


def test_orphans_preserve_service_attempt_and_unknown_origins(tmp_path):
    a, b = run(1), run(2)
    a['profile_snapshot']['connection_id'] = 'service-a'
    b['profile_snapshot']['connection_id'] = 'service-b'
    ea, eb, repeated = evaluation(3, a, False), evaluation(4, b), evaluation(5, a)
    independent = evaluation(6, a, False)
    independent['solve_run_id'] = '20260920-120000-000000ff'
    unknown = evaluation(7, a, False)
    unknown.pop('solve_run_id')
    another_unknown = dict(unknown, id='20260920-120000-00000008')
    result = Results([ea, eb, repeated, independent, unknown, another_unknown], run_root=tmp_path)
    assert result.listing()['total'] == 5 and len(result.listing()['groups']) == 2
    assert result.for_run(ea['id'])['evaluation_run_ids'] == [ea['id'], repeated['id']]
    assert result.for_run(eb['id'])['evaluation_run_ids'] == [eb['id']]
    assert result.for_run(independent['id'])['evaluation_run_ids'] == [independent['id']]
    assert result.for_run(unknown['id'])['evaluation_run_ids'] == [unknown['id']]
    assert result.for_run(another_unknown['id'])['evaluation_run_ids'] == [another_unknown['id']]
    fallback = dict(eb)
    fallback.pop('solve_run_id')
    assert Results([a, fallback], run_root=tmp_path).for_run(a['id'])['status'] == 'submitted'


def test_csv_redacts_nested_sensitive_fields_before_serializing_conditions(tmp_path):
    solve = run(1)
    solve['profile_snapshot']['parameters'] = {'temperature': .3, 'extra_body': {
        'password': 'AUDIT-PASSWORD-SENTINEL', 'nested': [{'client_secret': 'AUDIT-CLIENT-SENTINEL'}]}}
    result = Results([solve, evaluation(2, solve)], run_root=tmp_path)
    for exported in (result.export_csv(), result.export_json()):
        assert 'AUDIT-PASSWORD-SENTINEL' not in exported and 'AUDIT-CLIENT-SENTINEL' not in exported
    conditions = json.loads(next(csv.DictReader(io.StringIO(result.export_csv())))['conditions_json'])
    assert conditions['parameters']['temperature'] == .3
    assert conditions['parameters']['extra_body'] == {'password': '[REDACTED]', 'nested': [{'client_secret': '[REDACTED]'}]}


def test_orphan_attempt_identity_keeps_retries_but_not_independent_solves(tmp_path):
    first = run(1, 'evaluate', attempt_id='first', evaluation={'category': 'test_failed'})
    repeated = run(2, 'evaluate', attempt_id='first', evaluation={'category': 'passed'})
    independent = run(3, 'evaluate', attempt_id='second', evaluation={'category': 'test_failed'})
    result = Results([first, repeated, independent], run_root=tmp_path)
    assert result.listing()['total'] == 2
    assert result.for_run(first['id'])['evaluation_run_ids'] == [first['id'], repeated['id']]
    assert result.for_run(first['id'])['status'] == 'passed'
    assert result.for_run(independent['id'])['evaluation_run_ids'] == [independent['id']]
    assert result.listing()['summary']['instances'] == 1 and result.listing()['summary']['resolution_rate'] == 0


@pytest.mark.parametrize(('status',), [('completed',), ('cancelled',)])
def test_storage_preserves_experiment_first_attempt_and_allows_unrelated_cleanup(status):
    from local_setup.webui.common import RUNS, job_dir, new_id, save_json
    from local_setup.webui.queues import Queues
    from local_setup.webui.server import Manager
    from local_setup.webui.storage import candidates, delete, preview, summary

    manager = Manager()
    manager.queues = Queues(manager)
    first = run(21, id=new_id(), attempt_id='first', experiment_id='experiment')
    assessed = evaluation(22, first, id=new_id())
    manual = evaluation(26, first, False, id=new_id())
    retry = run(23, id=new_id(), attempt_id='retry', attempt_number=2, experiment_id='experiment')
    retry_assessed = evaluation(24, retry, False, id=new_id())
    unrelated = run(25, id=new_id())
    manager.records = {r['id']: r for r in (first, assessed, manual, retry, retry_assessed, unrelated)}
    for record in manager.records.values():
        save_json(job_dir(record['id']) / 'meta.json', record)
    stale = preview(['run:' + first['id']], manager)
    queue = {'id': 'experiment', 'kind': 'experiment', 'status': status, 'dataset': 'verified',
             'profile_snapshots': {'one': first['profile_snapshot']}, 'items': [{
                 'instance_id': first['instance_id'], 'profile_id': 'one', 'first_attempt_id': 'first',
                 'status': 'completed', 'attempts': [
                     {'run_id': r['id'], 'attempt_id': attempt, 'stage': stage, 'status': 'completed'}
                     for r, attempt, stage in ((first, 'first', 'solve'), (assessed, 'first', 'evaluate'),
                                               (retry, 'retry', 'solve'), (retry_assessed, 'retry', 'evaluate'))]}]}
    manager.queues.records[queue['id']] = queue
    before = Results(list(manager.records.values()), [queue], RUNS).experiment(queue['id'])['summary']
    assert before['resolution_rate'] == 0 and before['evaluated'] == 1
    assert 'run:' + first['id'] not in candidates(manager) and 'run:' + assessed['id'] not in candidates(manager)
    assert 'run:' + manual['id'] not in candidates(manager)
    listed = summary(manager)['items']
    assert {item['id'] for item in listed if item['id'].startswith('run:')} == {
        'run:' + r['id'] for r in (retry, retry_assessed, unrelated)}
    assert all(isinstance(item['bytes'], int) for item in listed)
    with pytest.raises(ValueError, match='受保护'):
        preview(['run:' + assessed['id']], manager)
    with pytest.raises(ValueError, match='状态已变化'):
        delete(stale['token'], manager)
    removable = ['run:' + r['id'] for r in (retry, retry_assessed, unrelated)]
    assert delete(preview(removable, manager)['token'], manager)['removed'] == removable
    assert all(not job_dir(r['id']).exists() for r in (retry, retry_assessed, unrelated))
    assert Results(list(manager.records.values()), [queue], RUNS).experiment(queue['id'])['summary'] == before
    assert all((job_dir(r['id']) / 'meta.json').is_file() for r in (first, assessed, manual))


def test_storage_summary_keeps_unfinished_queue_and_linked_manual_evaluation_protected():
    from local_setup.webui.common import job_dir, new_id, save_json
    from local_setup.webui.queues import Queues
    from local_setup.webui.server import Manager
    from local_setup.webui.storage import preview, summary

    manager = Manager()
    manager.queues = Queues(manager)
    solve = run(1, id=new_id(), attempt_id='pending-queue')
    assessed = evaluation(2, solve, id=new_id())
    unrelated = run(3, id=new_id())
    manager.records = {r['id']: r for r in (solve, assessed, unrelated)}
    for record in manager.records.values():
        save_json(job_dir(record['id']) / 'meta.json', record)
    manager.queues.records['paused-queue'] = {'id': 'paused-queue', 'kind': 'batch', 'status': 'paused', 'items': [
        {'attempts': [{'run_id': solve['id'], 'attempt_id': 'pending-queue', 'stage': 'solve', 'status': 'completed'}]}]}
    data = summary(manager)
    assert [item['id'] for item in data['items'] if item['id'].startswith('run:')] == ['run:' + unrelated['id']]
    assert sum(group['bytes'] or 0 for group in data['categories']) > 0
    with pytest.raises(ValueError, match='受保护'):
        preview(['run:' + assessed['id']], manager)


def test_results_distinguish_resolution_from_tests_and_deduplicate_evaluations(tmp_path):
    solve = run(1)
    first = evaluation(2, solve, passed=False)
    second = evaluation(3, solve, passed=False)
    error = run(4, 'evaluate', solve_run_id=solve['id'], evaluation={}, status='failed')
    data = Results([solve, first, second, error], run_root=tmp_path).listing()
    assert data['total'] == data['summary']['instances'] == 1
    assert data['summary']['resolution_rate'] == 0
    item = data['items'][0]
    assert item['tests']['overall']['pass_rate'] == pytest.approx(20 / 21)
    assert item['tests']['repair']['passed'] == 0 and item['tests']['regression']['passed'] == 20
    assert item['evaluation_run_id'] == second['id'] and item['has_later_evaluation_error']
    assert item['latest_evaluation_status'] == 'evaluation_error' and len(item['evaluation_run_ids']) == 3
    assert item['cost']['estimated'] == .12 and item['timing']['total_seconds'] is None
    assert item['timing']['preparation_seconds'] is None
    results = Results([solve, first, second, error], run_root=tmp_path)
    assert results.for_run(first['id'])['attempt_id'] == solve['id']
    assert results.for_run('missing') is None


def test_latest_valid_evaluation_uses_completion_time_not_solve_or_start_time(tmp_path):
    early, later, other = run(1), run(2), run(3)
    other['profile_snapshot']['connection_id'] = 'other-service'
    completed_first = evaluation(6, later, False, finished='2026-09-20T12:10:00+00:00')
    completed_last = evaluation(4, early, True, finished='2026-09-20T20:12:00+08:00')
    failed_repeat = evaluation(7, early, False, status='failed', finished='2026-09-20T12:13:00+00:00')
    failed_repeat['evaluation'] = {'category': 'infrastructure', 'errors': 1}
    index = Results([early, later, other, completed_first, completed_last, failed_repeat, evaluation(5, other)], run_root=tmp_path)
    chosen = index.detail(early['id'])
    assert chosen['evaluation_run_id'] == completed_last['id'] and chosen['has_later_evaluation_error']
    assert chosen['evaluated_at'] == '2026-09-20T12:12:00+00:00' and chosen['evaluation_time_source'] == 'finished'
    assert index.listing()['summary']['resolution_rate'] == 1
    compared = index.compare()
    assert compared['summary']['common_instances'] == 1
    assert all(group['summary']['resolution_rate'] == 1 for group in compared['groups'])
    assert [group['conditions']['connection_id'] for group in compared['groups']] == [None, 'other-service']
    assert [cell['solve_run_id'] for cell in compared['rows'][0]['cells']] == [early['id'], other['id']]
    # Late grading changes the selected result without moving the established model columns.
    reverse = Results(list(reversed(list(index.records.values()))), run_root=tmp_path).compare()
    assert reverse['groups'] == compared['groups'] and reverse['rows'] == compared['rows']
    exported = list(csv.DictReader(io.StringIO(index.export_csv())))
    assert {row['solve_run_id'] for row in exported if row['included_in_summary'] == 'True'} == {early['id'], other['id']}
    assert next(row for row in exported if row['solve_run_id'] == early['id'])['evaluated_at'] == chosen['evaluated_at']
    # A later valid failure replaces success; this policy never selects the best score.
    completed_first['finished'] = '2026-09-20T12:14:00+00:00'
    assert Results([early, later, completed_first, completed_last], run_root=tmp_path).listing()['summary']['resolution_rate'] == 0


def test_evaluation_time_legacy_fallback_and_repeat_order(tmp_path):
    solve = run(1)
    earlier_start = evaluation(2, solve, False, finished='2026-09-20T12:20:00+00:00')
    later_start = evaluation(3, solve, True, finished='2026-09-20T12:10:00+00:00')
    item = Results([solve, earlier_start, later_start], run_root=tmp_path).detail(solve['id'])
    assert item['status'] == 'test_failed' and item['evaluation_run_id'] == earlier_start['id']
    assert item['evaluation_run_ids'] == [later_start['id'], earlier_start['id']]
    earlier_start['finished'] = 'invalid timestamp'
    del later_start['finished']
    item = Results([solve, earlier_start, later_start], run_root=tmp_path).detail(solve['id'])
    assert item['status'] == 'passed' and item['evaluation_time_source'] == 'created'
    assert item['evaluated_at'] == later_start['created']


def test_association_ignores_parent_id_and_rejects_mismatched_patch(tmp_path):
    solve, other = run(1), run(2, patch_hash='patch-b')
    correct = evaluation(3, solve, parent_id=other['id'])
    mismatch = evaluation(4, solve)
    mismatch['patch_hash'] = 'unrelated'
    result = Results([solve, other, correct, mismatch], run_root=tmp_path)
    assert result.detail(solve['id'])['evaluation_run_id'] == correct['id']
    assert result.detail(other['id'])['evaluation_run_id'] is None
    assert result.detail(mismatch['id'])['association'] == 'evaluation_only'
    assert result.detail(mismatch['id'])['cost']['estimated'] is None


def test_history_requires_common_valid_cases_and_preserves_configuration_groups(tmp_path):
    a, b = run(1), run(2, instance_id='django__django-10097')
    b['profile_snapshot'] = dict(b['profile_snapshot'], limits={'step_limit': 10, 'cost_limit': 3})
    rows = [a, evaluation(3, a), b, evaluation(4, b)]
    data = Results(rows, run_root=tmp_path).compare()
    assert len(data['groups']) == 2 and data['summary']['common_instances'] == 0
    assert all(group['summary']['resolution_rate'] is None for group in data['groups'])
    c = run(5)
    c['profile_snapshot'] = b['profile_snapshot']
    data = Results(rows + [c, evaluation(6, c, False)], run_root=tmp_path).compare()
    assert data['summary']['common_instances'] == 1
    assert sorted(group['summary']['resolution_rate'] for group in data['groups']) == [0, 1]
    assert sum(row['included'] for row in data['rows']) == 1


def test_experiment_first_attempt_fixed_denominator_retry_does_not_replace(tmp_path):
    first = run(1, attempt_id='first', attempt_number=1, experiment_id='experiment')
    retry = run(3, attempt_id='retry', attempt_number=2, experiment_id='experiment', patch_hash='patch-b')
    q = {'id': 'experiment', 'kind': 'experiment', 'dataset': 'verified', 'auto_approve': False,
         'profile_snapshots': {'one': first['profile_snapshot']}, 'items': [
             {'instance_id': first['instance_id'], 'profile_id': 'one', 'first_attempt_id': 'first', 'attempts': []},
             {'instance_id': 'django__django-10097', 'profile_id': 'one', 'first_attempt_id': 'unstarted', 'attempts': []}]}
    records = [first, evaluation(2, first, False), retry, evaluation(4, retry)]
    index = Results(records, [q], tmp_path)
    data = index.experiment('experiment')
    assert data['summary']['denominator'] == 2 and data['summary']['resolution_rate'] == 0
    assert len(data['groups']) == 1 and data['groups'][0]['summary']['denominator'] == 2
    assert data['summary']['evaluated'] == 1 and len(data['retry_results']) == 1
    assert data['rows'][0]['cells'][0]['status'] == 'test_failed'
    assert any(cell['status'] == 'pending' for row in data['rows'] for cell in row['cells'])
    assert json.loads(index.export_json(experiment_id='experiment', dataset='missing', group_ids=['missing']))['summary']['denominator'] == 2
    assert len(list(csv.DictReader(io.StringIO(index.export_csv(experiment_id='experiment', dataset='missing', group_ids=[]))))) == 2
    exported = list(csv.DictReader(io.StringIO(index.export_csv(experiment_id='experiment'))))
    assert all(row['denominator_scope'] == 'planned_first_attempts' and row['denominator_count'] == '2' for row in exported)


def test_unknown_cost_excluded_diagnostics_and_safe_export(tmp_path):
    solve = run(1, stats={'calls': 1, 'cost': 0})
    solve['profile_snapshot']['profile_name'] = '=HYPERLINK("bad")'
    solve['profile_snapshot']['parameters'] = {'api_key': 'sk-sensitivevalue123'}
    solve['profile_snapshot']['runtime'] = {'system_template': 'private full prompt'}
    solve['profile_snapshot']['configs'] = {'swebench': {'agent': {'system_template': 'frozen private prompt'}}}
    diagnostic = run(2, model='deterministic')
    result = Results([solve, evaluation(3, solve), diagnostic], run_root=tmp_path)
    assert result.listing()['total'] == 1 and result.listing()['summary']['cost_unknown'] == 1
    assert result.detail(solve['id'])['cost']['estimated'] is None
    exported = result.export_json()
    assert 'private.example' not in exported and 'sk-sensitive' not in exported
    assert 'private full prompt' not in exported
    assert 'frozen private prompt' not in exported
    assert "'=HYPERLINK" in result.export_csv()
    assert json.loads(exported)['items'][0]['tests']['repair']['pass_rate'] == 1
    solve['profile_snapshot']['pricing'] = {'mode': 'free'}
    assert Results([solve], run_root=tmp_path).detail(solve['id'])['cost']['estimated'] == 0


def test_legacy_reports_read_only_and_missing_conditions_remain_unknown(tmp_path):
    legacy = run(1, 'evaluate', evaluation={}, profile_snapshot={}, stats={})
    folder = tmp_path / legacy['id']
    folder.mkdir()
    report = folder / ('model.' + legacy['id'] + '.json')
    report.write_text(json.dumps({'resolved_instances': 1, 'completed_instances': 1, 'error_instances': 0}), encoding='utf-8')
    before = report.read_bytes()
    item = Results([legacy], run_root=tmp_path).listing()['items'][0]
    assert item['status'] == 'passed' and item['association'] == 'evaluation_only'
    assert not item['conditions_known'] and item['tests']['repair']['total'] is None
    assert report.read_bytes() == before and list(folder.iterdir()) == [report]


def test_template_change_separates_history_and_identical_profiles_keep_experiment_columns(tmp_path):
    a, b = run(1), run(2)
    a['profile_snapshot']['configs'] = {'prompt': 'first'}
    b['profile_snapshot']['configs'] = {'prompt': 'second'}
    assert len(Results([a, b], run_root=tmp_path).listing()['groups']) == 2
    snapshots = {key: dict(a['profile_snapshot'], profile_id=key, profile_name=key) for key in ('one', 'two')}
    q = {'id': 'experiment', 'kind': 'experiment', 'dataset': 'verified', 'auto_approve': False,
         'profile_snapshots': snapshots, 'items': [
             {'instance_id': a['instance_id'], 'profile_id': key, 'first_attempt_id': key, 'status': 'failed',
              'attempts': [{'attempt_id': key, 'stage': 'download', 'status': 'failed'}]} for key in snapshots]}
    data = Results([], [q], tmp_path).experiment('experiment')
    assert len(data['groups']) == 2 and all(g['summary']['denominator'] == 1 for g in data['groups'])
    assert len(data['rows'][0]['cells']) == 2
    assert all(c['status'] == 'preparation_failed' for c in data['rows'][0]['cells'])
    assert data['summary']['errors'] == 2


def test_exports_honor_dataset_and_group_filters_without_fallback(tmp_path):
    verified, lite = run(1), run(2, dataset='lite')
    lite['profile_snapshot']['parameters'] = {'temperature': .3}
    result = Results([verified, lite], run_root=tmp_path)
    lite_group = result.detail(lite['id'])['group_id']
    assert [row['attempt_id'] for row in json.loads(result.export_json(dataset='lite'))['items']] == [lite['id']]
    assert [row['attempt_id'] for row in json.loads(result.export_json(group_ids=[lite_group]))['items']] == [lite['id']]
    filtered = list(csv.DictReader(io.StringIO(result.export_csv(dataset='lite', group_ids=[lite_group]))))
    assert len(filtered) == 1 and filtered[0]['dataset'] == 'lite'
    for groups in ([], ['does-not-exist'], [lite_group]):
        dataset = 'verified' if groups == [lite_group] else 'all'
        assert json.loads(result.export_json(dataset=dataset, group_ids=groups))['items'] == []
        assert list(csv.DictReader(io.StringIO(result.export_csv(dataset=dataset, group_ids=groups)))) == []


def test_connection_and_price_snapshot_are_distinct_conditions(tmp_path):
    a, b, c = run(1), run(2), run(3)
    for row in (a, b, c):
        row['profile_snapshot'].update(connection_id='connection-a', connection_name='Connection A',
                                       pricing={'source': 'custom', 'input_per_million': 2, 'output_per_million': 6})
    b['profile_snapshot']['connection_id'] = 'connection-b'
    c['profile_snapshot']['pricing'] = {'source': 'custom', 'input_per_million': 2, 'output_per_million': 8}
    index = Results([a, b, c], run_root=tmp_path)
    assert len(index.listing()['groups']) == 3
    rows = list(csv.DictReader(io.StringIO(index.export_csv())))
    assert len({row['configuration_fingerprint'] for row in rows}) == 3
    assert all(row['pricing_source'] == 'custom' and row['conditions_known'] == 'True' for row in rows)
    assert all('private.example' not in row['conditions_json'] and 'api_base' not in row['conditions_json'] for row in rows)
    prices = [json.loads(row['conditions_json'])['pricing'] for row in rows]
    assert {price['output_per_million'] for price in prices} == {6, 8}
    assert all(price['input_per_million'] == 2 and price['source'] == 'custom' for price in prices)


def test_preparation_timing_requires_completed_same_attempt_stages_and_exports_denominators(tmp_path):
    solve = run(3, attempt_id='chosen')
    prepare = run(1, 'download', attempt_id='chosen')
    cache = run(2, 'cache')
    unrelated = run(4, 'download', attempt_id='another')
    failed = run(5, 'download', attempt_id='chosen', status='failed')
    evaluation_record = evaluation(6, solve)
    queue = {'id': 'queue', 'kind': 'batch', 'items': [{'attempts': [{'run_id': cache['id'], 'attempt_id': 'chosen', 'stage': 'cache'}]}]}
    index = Results([prepare, cache, solve, unrelated, failed, evaluation_record], [queue], tmp_path)
    item = index.detail(solve['id'])
    assert item['timing'] == {'preparation_seconds': 20, 'solve_seconds': 10, 'evaluation_seconds': 10, 'total_seconds': 40}
    exported = list(csv.DictReader(io.StringIO(index.export_csv())))[0]
    assert exported['preparation_seconds'] == '20.0' and exported['total_seconds'] == '40.0'
    assert exported['selection_policy'] == 'latest_valid_per_instance_configuration'
    assert exported['denominator_scope'] == 'valid_evaluations_per_configuration' and exported['denominator_count'] == '1'
    assert exported['included_in_summary'] == 'True'
    del cache['finished']
    assert Results([prepare, cache, solve, evaluation_record], [queue], tmp_path).detail(solve['id'])['timing']['total_seconds'] is None


def test_comparison_export_marks_common_denominator_and_excludes_repeated_attempts(tmp_path):
    first, second, other = run(1), run(3, patch_hash='second'), run(5, patch_hash='other')
    other['profile_snapshot']['connection_id'] = 'second-connection'
    index = Results([first, evaluation(2, first, False), second, evaluation(4, second), other, evaluation(6, other)], run_root=tmp_path)
    groups = [g['group_id'] for g in index.listing()['groups']]
    data = json.loads(index.export_json(group_ids=groups))
    assert data['comparison']['summary']['common_instances'] == 1
    assert data['export_columns']['denominator_scope'] == 'common_valid_evaluations'
    rows = list(csv.DictReader(io.StringIO(index.export_csv(group_ids=groups))))
    assert len(rows) == 3 and all(row['denominator_count'] == '1' for row in rows)
    assert sum(row['included_in_summary'] == 'True' for row in rows) == 2


def test_results_routes_require_session_and_export_safe_metadata():
    from fastapi.testclient import TestClient
    from local_setup.webui.common import RUNS, save_json
    from local_setup.webui.server import app, NONCE
    solve = run(41, model='openai/results-route-fixture')
    solve['profile_snapshot'] = dict(solve['profile_snapshot'], model_name=solve['model'])
    for record in [solve, evaluation(42, solve, False)]:
        save_json(RUNS / record['id'] / 'meta.json', record)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.get('/api/results').status_code in {401, 403}
        assert client.post('/session', json={'nonce': NONCE}).status_code == 200
        page = client.get('/api/results', params={'search': 'results-route-fixture'}).json()
        assert page['total'] == 1 and page['summary']['resolution_rate'] == 0
        detail = client.get('/api/results/' + solve['id'])
        assert detail.status_code == 200 and detail.json()['tests']['repair']['failed'] == 1
        compare = client.get('/api/results/compare', params={'group_ids': page['groups'][0]['group_id']}).json()
        assert compare['summary']['common_instances'] == 0
        for format in ('csv', 'json'):
            export = client.get('/api/results/export', params={'format': format})
            assert export.status_code == 200 and 'attachment' in export.headers['content-disposition']
            assert 'private.example' not in export.text and 'model_patch' not in export.text
        assert client.get('/api/results/export', params={'group_ids': 'missing'}).json()['items'] == []
        assert client.get('/api/results/export', params={'dataset': 'missing'}).json()['items'] == []
