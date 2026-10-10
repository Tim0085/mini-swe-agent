import json
from pathlib import Path

import pytest

from local_setup.webui.patches import conversation_for
from local_setup.webui.tests.test_patch_choices import INSTANCE, write_pair


def saved_solve(folder: Path, name: str, attempt: str):
    folder.mkdir(parents=True)
    trajectory, prediction = write_pair(folder)
    data = json.loads(trajectory.read_text(encoding='utf-8'))
    data['messages'] = [{'role': 'assistant', 'content': name}]
    trajectory.write_text(json.dumps(data), encoding='utf-8')
    return trajectory, prediction, {'id': folder.name, 'kind': 'swebench', 'attempt_id': attempt,
                                    'instance_id': INSTANCE, 'dataset': 'verified'}


@pytest.mark.parametrize(('source_kind',), [('same_folder',), ('solve_id',), ('attempt',), ('source_prediction',), ('source_trajectory',)])
def test_identical_patch_uses_actual_solve_even_if_newer_trajectory_is_first(tmp_path, source_kind):
    a, pa, ma = saved_solve(tmp_path / '20261009-120000-00000001', 'first solve', 'first')
    b, pb, mb = saved_solve(tmp_path / '20261009-120000-00000002', 'later solve', 'second')
    selected = tmp_path / 'evaluation.predictions.jsonl'
    selected.write_bytes(pa.read_bytes())
    source = {'instance_id': INSTANCE, 'dataset': 'verified'}
    if source_kind == 'same_folder':
        selected = pa
    elif source_kind == 'solve_id':
        source.update(solve_run_id=ma['id'], attempt_id='first')
    elif source_kind == 'attempt':
        source['attempt_id'] = 'first'
    else:
        source['patch_source'] = {'path': str(pa if source_kind == 'source_prediction' else a)}
    result = conversation_for(selected, INSTANCE, [(b, mb), (a, ma)], dataset='verified', source=source)
    assert result['association'] == 'matched' and result['source_run_id'] == ma['id']
    assert [m['content'] for m in result['messages']] == ['first solve']


def test_ambiguous_or_missing_origin_never_substitutes_identical_patch(tmp_path):
    a, pa, ma = saved_solve(tmp_path / '20261009-120000-00000001', 'first solve', 'first')
    b, pb, mb = saved_solve(tmp_path / '20261009-120000-00000002', 'later solve', 'second')
    selected = tmp_path / 'input.predictions.jsonl'
    selected.write_bytes(pa.read_bytes())
    paths = [(b, mb), (a, ma)]
    result = conversation_for(selected, INSTANCE, paths)
    assert result['association'] == 'ambiguous' and not result['messages'] and '来源待确认' in result['note']
    for source in ({'solve_run_id': '20261009-120000-ffffffff'}, {'attempt_id': 'removed'},
                   {'solve_run_id': ma['id'], 'attempt_id': 'second'},
                   {'patch_source': {'path': str(tmp_path / 'removed' / 'predictions.jsonl')}}):
        assert conversation_for(selected, INSTANCE, paths, source=source)['messages'] == []
    a.write_text(a.read_text(encoding='utf-8').replace('+new', '+changed'), encoding='utf-8')
    assert conversation_for(selected, INSTANCE, paths, source={'solve_run_id': ma['id']})['messages'] == []
    # Selecting the trajectory itself remains authoritative, even with duplicate paths.
    direct = conversation_for(b, INSTANCE, [(b, mb), (b, mb)])
    assert direct['association'] == 'matched' and len(direct['messages']) == 1


def test_conversation_route_uses_saved_request_source_not_latest_identical_patch():
    from fastapi.testclient import TestClient
    from local_setup.webui.common import job_dir, new_id, save_json
    from local_setup.webui.server import NONCE, app

    a, pa, ma = saved_solve(job_dir(new_id()), 'request source solve', 'first')
    b, pb, mb = saved_solve(job_dir(new_id()), 'other solve', 'second')
    for path, record in [(a, ma), (b, mb)]:
        record.update(status='completed', title='offline conversation fixture', created='2026-10-09T12:00:00+00:00')
        path.rename(path.with_name('trajectory.traj.json'))
        save_json(path.parent / 'meta.json', record)
    evaluation_id = new_id()
    folder = job_dir(evaluation_id)
    folder.mkdir()
    (folder / 'input.predictions.jsonl').write_bytes(pa.read_bytes())
    save_json(folder / 'meta.json', {'id': evaluation_id, 'kind': 'evaluate', 'status': 'completed', 'title': 'offline evaluation fixture',
                                   'created': '2026-10-09T12:01:00+00:00', 'instance_id': INSTANCE, 'dataset': 'verified'})
    save_json(folder / 'request.json', {'queue_context': {'solve_run_id': ma['id'], 'attempt_id': 'first'}})
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.get('/api/runs/' + evaluation_id + '/conversation-source').status_code in {401, 403}
        assert client.post('/session', json={'nonce': NONCE}).status_code == 200
        result = client.get('/api/runs/' + evaluation_id + '/conversation-source').json()
        assert result['association'] == 'matched' and result['source_run_id'] == ma['id']
        assert [message['content'] for message in result['messages']] == ['request source solve']
