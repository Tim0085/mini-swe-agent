import csv
import io
import json

import pytest

from local_setup.webui.patches import catalog, conversation_for, inspect_patch, source_paths
from local_setup.webui.results import Results
from local_setup.webui.tests.test_results import evaluation, run

DATASET = 'rebench-34d5a588'
OTHER = 'rebench-12345678'
INSTANCE = 'django__django-10097'
SNAPSHOT = {'dataset': DATASET, 'benchmark': 'swe-rebench-classic',
            'source': 'nebius/SWE-rebench-leaderboard',
            'source_revision': '34d5a58864acf91613740a09ec5d205228dcfa39', 'split': 'test'}
PATCH = 'diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new\n'


def pair(folder, identity=None):
    folder.mkdir(parents=True, exist_ok=True)
    identity = identity or {}
    prediction = folder / 'predictions.jsonl'
    prediction.write_text(json.dumps({'instance_id': INSTANCE, 'model_name_or_path': 'openai/test',
                                     'model_patch': PATCH, **identity}) + '\n', encoding='utf-8')
    trajectory = folder / 'trajectory.traj.json'
    trajectory.write_text(json.dumps({'info': {'instance_id': INSTANCE, 'exit_status': 'Submitted', 'submission': PATCH,
                                             'config': {'model': {'model_name': 'openai/test'}}, **identity},
                                      'messages': [{'role': 'assistant', 'content': 'saved model response'}]}), encoding='utf-8')
    return prediction, trajectory


def test_fixed_benchmark_patch_requires_known_dataset_and_preserves_legacy_inputs(tmp_path):
    old = pair(tmp_path / 'legacy')
    current = pair(tmp_path / 'current', {'dataset': DATASET, 'benchmark_snapshot': SNAPSHOT,
                                        'case_key': DATASET + ':' + INSTANCE})
    other = pair(tmp_path / 'other', {'dataset': OTHER})
    paths = [(p, '') for p in (*old, *current, *other)]
    selection = catalog(INSTANCE, paths, dataset=DATASET)
    assert len(selection['items']) == 1 and selection['items'][0]['copies'] == 2
    assert selection['recommended'] == str(current[0]) and len(selection['rejected']) == 4
    assert selection['items'][0]['case_key'] == DATASET + ':' + INSTANCE
    assert '未注明基准版本' in inspect_patch(old[0], INSTANCE, dataset=DATASET)['reason']
    assert inspect_patch(old[0], INSTANCE)['usable']
    assert inspect_patch(old[0], INSTANCE, dataset='verified')['usable']
    assert inspect_patch(old[0], INSTANCE, dataset='lite')['usable']
    assert not inspect_patch(current[0], INSTANCE)['usable']
    assert not inspect_patch(current[0], INSTANCE, dataset='verified')['usable']


@pytest.mark.parametrize(('format_index',), [(0,), (1,)])
def test_record_or_embedded_case_identity_is_accepted_but_conflicts_are_rejected(tmp_path, format_index):
    old = pair(tmp_path / 'old')[format_index]
    identity = {'instance_id': INSTANCE, 'dataset': DATASET, 'benchmark_snapshot': SNAPSHOT}
    assert inspect_patch(old, INSTANCE, identity, dataset=DATASET)['usable']
    by_case = pair(tmp_path / 'case', {'case_key': DATASET + ':' + INSTANCE})[format_index]
    assert inspect_patch(by_case, INSTANCE, dataset=DATASET)['usable']
    for embedded in ({'dataset': OTHER}, {'case_key': DATASET + ':django__django-12345'},
                     {'dataset': DATASET, 'benchmark_snapshot': {**SNAPSHOT, 'source_revision': '9' * 40}}):
        path = pair(tmp_path / 'conflict', embedded)[format_index]
        assert not inspect_patch(path, INSTANCE, identity, dataset=DATASET)['usable']


def test_conversation_does_not_cross_benchmark_even_for_same_patch_and_model(tmp_path):
    old = pair(tmp_path / 'old', {'dataset': 'verified'})
    current = pair(tmp_path / 'current', {'dataset': DATASET, 'benchmark_snapshot': SNAPSHOT})
    assert conversation_for(current[0], INSTANCE, [(old[1], '')], dataset=DATASET)['messages'] == []
    result = conversation_for(current[0], INSTANCE, [(old[1], ''), (current[1], '')], dataset=DATASET)
    assert [message['content'] for message in result['messages']] == ['saved model response']


def test_source_paths_carries_run_identity_and_filters_explicit_other_datasets(tmp_path):
    from local_setup.webui.common import job_dir, new_id
    current, old = new_id(), new_id()
    pair(job_dir(current))
    pair(job_dir(old))
    records = [{'id': key, 'kind': 'swebench', 'dataset': dataset, 'instance_id': INSTANCE, 'created': key}
               for key, dataset in [(current, DATASET), (old, 'verified')]]
    paths = source_paths(records, tmp_path, dataset=DATASET)
    assert len(paths) == 2 and all(path.parent == job_dir(current) for path, _ in paths)
    assert all(identity['dataset'] == DATASET for _, identity in paths)
    assert catalog(INSTANCE, paths, dataset=DATASET)['recommended']


@pytest.mark.parametrize(('source_mode',), [('explicit',), ('implicit',), ('attempt',), ('path',)])
def test_result_association_never_crosses_benchmark_or_snapshot(tmp_path, source_mode):
    old = run(1, instance_id=INSTANCE, attempt_id='same-attempt')
    current = run(2, instance_id=INSTANCE, dataset=DATASET, benchmark_snapshot=SNAPSHOT, attempt_id='same-attempt')
    assessed = evaluation(3, old, dataset=DATASET, benchmark_snapshot=SNAPSHOT)
    if source_mode != 'explicit':
        assessed.pop('solve_run_id')
    if source_mode == 'attempt':
        assessed['attempt_id'] = 'same-attempt'
    if source_mode == 'path':
        assessed['patch_source'] = {'path': str(tmp_path / old['id'] / 'predictions.jsonl')}
    result = Results([old, assessed], run_root=tmp_path)
    assert result.for_run(old['id'])['status'] == 'submitted'
    assert result.for_run(assessed['id'])['association'] == 'evaluation_only'
    # A trustworthy explicit source that is missing must not fall back to a new same-patch solve.
    if source_mode in {'explicit', 'path'}:
        result = Results([current, assessed], run_root=tmp_path)
        assert result.for_run(current['id'])['status'] == 'submitted'


def test_results_preserve_version_denominators_and_export_case_identity(tmp_path):
    old = run(1, instance_id=INSTANCE)
    current = run(3, instance_id=INSTANCE, dataset=DATASET, benchmark_snapshot=SNAPSHOT)
    other = run(5, instance_id=INSTANCE, dataset=OTHER,
                benchmark_snapshot={**SNAPSHOT, 'dataset': OTHER, 'source_revision': '12345678' + '0' * 32})
    records = [row for solve, number in [(old, 2), (current, 4), (other, 6)]
               for row in (solve, evaluation(number, solve, passed=solve is current, dataset=solve['dataset'],
                                            benchmark_snapshot=solve.get('benchmark_snapshot', {})))]
    index = Results(records, run_root=tmp_path)
    assert index.listing()['summary']['denominator'] == 3
    assert index.listing(dataset=DATASET)['summary']['resolution_rate'] == 1
    assert index.listing(dataset=OTHER)['summary']['resolution_rate'] == 0
    exported = next(csv.DictReader(io.StringIO(index.export_csv(dataset=DATASET))))
    assert exported['case_key'] == DATASET + ':' + INSTANCE
    assert json.loads(exported['benchmark_snapshot_json']) == SNAPSHOT
    assert index.compare()['requires_dataset'] and index.compare()['rows'] == []
    assert index.compare()['summary']['union_instances'] == 3
    assert len(index.compare(dataset=DATASET)['rows']) == 1


def test_inconsistent_request_identity_cannot_turn_solve_into_resolved_result(tmp_path):
    solve = run(1, instance_id=INSTANCE, dataset=DATASET, benchmark_snapshot=SNAPSHOT)
    assessed = evaluation(2, solve, dataset=DATASET, benchmark_snapshot=SNAPSHOT)
    folder = tmp_path / assessed['id']
    folder.mkdir()
    (folder / 'request.json').write_text(json.dumps({'dataset': 'verified', 'instance_id': INSTANCE}), encoding='utf-8')
    index = Results([solve, assessed], run_root=tmp_path)
    assert index.for_run(solve['id'])['status'] == 'submitted'
    assert index.for_run(assessed['id'])['status'] == 'unknown'
    assert index.for_run(assessed['id'])['identity_warning']
    assert index.listing()['summary']['resolved'] == 0


def test_experiment_first_attempt_lookup_requires_its_benchmark_and_case(tmp_path):
    solve = run(1, instance_id=INSTANCE, attempt_id='first')
    experiment = {'id': 'experiment', 'kind': 'experiment', 'dataset': DATASET, 'benchmark_snapshot': SNAPSHOT,
                  'profile_snapshots': {'one': solve['profile_snapshot']},
                  'items': [{'instance_id': INSTANCE, 'profile_id': 'one', 'first_attempt_id': 'first', 'attempts': []}]}
    data = Results([solve, evaluation(2, solve)], [experiment], tmp_path).experiment('experiment')
    assert data['summary']['denominator'] == 1 and data['summary']['resolved'] == 0
    assert data['rows'][0]['cells'][0]['case_key'] == DATASET + ':' + INSTANCE
    assert data['rows'][0]['cells'][0]['benchmark_snapshot'] == SNAPSHOT


def test_mixed_benchmarks_withhold_rates_and_exports_until_single_dataset_selected(tmp_path):
    records = []
    for index, (dataset, connection, passed) in enumerate([
        ('verified', 'model-a', True), (DATASET, 'model-a', True),
        ('verified', 'model-b', True), (DATASET, 'model-b', False),
    ]):
        solve = run(1 + index * 2, dataset=dataset, instance_id=INSTANCE)
        solve['profile_snapshot']['connection_id'] = connection
        records.extend([solve, evaluation(2 + index * 2, solve, passed=passed, dataset=dataset)])
    index = Results(records, run_root=tmp_path)
    overview = index.listing()
    assert overview['summary']['evaluated'] == 4 and overview['summary']['resolved'] == 3
    assert overview['summary']['resolution_rate'] is None and overview['summary']['requires_dataset']
    assert '先选择单一题集' in overview['note']
    assert all(group['summary']['resolution_rate'] is None for group in overview['groups'])
    compared = index.compare()
    assert compared['requires_dataset'] and compared['rows'] == compared['groups'] == []
    assert compared['summary']['common_instances'] == 0 and compared['summary']['union_instances'] == 2
    groups = [group['group_id'] for group in overview['groups']]
    exported = json.loads(index.export_json(group_ids=groups))
    assert exported['summary']['resolution_rate'] is None and exported['comparison']['requires_dataset']
    assert exported['export_columns']['denominators'] == {}
    assert all(row['requires_dataset'] == 'True' for row in csv.DictReader(io.StringIO(index.export_csv(group_ids=groups))))
    selected = index.compare(dataset=DATASET)
    assert selected['summary']['common_instances'] == 1
    assert sorted(group['summary']['resolution_rate'] for group in selected['groups']) == [0, 1]
    assert index.listing(dataset=DATASET)['summary']['resolution_rate'] == .5
    assert json.loads(index.export_json(dataset=DATASET))['summary']['requires_dataset'] is False
    for record in records:
        if record['dataset'] == DATASET:
            record['dataset'] = 'lite'
    legacy = Results(records, run_root=tmp_path)
    assert legacy.listing()['summary']['resolution_rate'] == .75
    assert legacy.compare()['summary']['common_instances'] == 2


def test_multiple_rebench_snapshots_also_require_a_dataset_filter(tmp_path):
    first = run(1, dataset=DATASET, instance_id=INSTANCE)
    second = run(3, dataset=OTHER, instance_id=INSTANCE)
    index = Results([first, evaluation(2, first, dataset=DATASET), second, evaluation(4, second, dataset=OTHER)], run_root=tmp_path)
    assert index.compare()['requires_dataset']
    assert index.listing()['summary']['resolution_rate'] is None
    assert index.listing()['summary']['evaluated'] == 2
    assert json.loads(index.export_json())['summary']['requires_dataset']
