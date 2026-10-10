import json
from pathlib import Path

from local_setup.webui.patches import catalog, conversation_for, inspect_patch

INSTANCE = 'django__django-10097'
PATCH = 'diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new\n'


def write_pair(folder: Path, patch: str = PATCH):
    trajectory = folder / (INSTANCE + '.traj.json')
    prediction = folder / (INSTANCE + '.predictions.jsonl')
    trajectory.write_text(json.dumps({'info': {'exit_status': 'Submitted', 'submission': patch, 'config': {'model': {'model_name': 'openai/test'}}},
                                     'messages': [{'role': 'system', 'content': 'private system fixture'}, {'role': 'user', 'content': '修复样例'}, {'role': 'assistant', 'content': '已保存的回复'}, {'role': 'tool', 'content': 'execution output'}]}), encoding='utf-8')
    prediction.write_text(json.dumps({'instance_id': INSTANCE, 'model_name_or_path': 'openai/test', 'model_patch': patch}) + '\n', encoding='utf-8')
    return trajectory, prediction


def test_choices_merge_duplicate_and_exclude_smoke_and_unsubmitted(tmp_path):
    trajectory, prediction = write_pair(tmp_path)
    smoke = tmp_path / 'smoke.traj.json'
    smoke.write_text(json.dumps({'info': {'exit_status': 'Submitted', 'submission': ''}}), encoding='utf-8')
    incomplete = tmp_path / 'incomplete.traj.json'
    incomplete.write_text(json.dumps({'info': {'exit_status': ''}}), encoding='utf-8')
    result = catalog(INSTANCE, [(p, '') for p in [trajectory, prediction, smoke, incomplete]])
    assert len(result['items']) == 1 and result['recommended'] == str(prediction)
    assert result['items'][0]['copies'] == 2 and result['items'][0]['has_conversation']
    assert result['items'][0]['files'] == ['example.py']
    assert len(result['rejected']) == 2 and all(x['reason'] for x in result['rejected'])


def test_distinct_patches_require_choice_and_mismatched_instance_excluded(tmp_path):
    trajectory, prediction = write_pair(tmp_path)
    different = tmp_path / 'different.predictions.jsonl'
    different.write_text(prediction.read_text().replace('+new', '+different'), encoding='utf-8')
    result = catalog(INSTANCE, [(trajectory, ''), (prediction, ''), (different, '')])
    assert len(result['items']) == 2 and result['recommended'] is None
    assert not inspect_patch(prediction, 'django__django-10098')['usable']
    assert not inspect_patch(trajectory, INSTANCE, 'django__django-10098')['usable']


def test_matching_conversation_never_uses_different_patch_or_model(tmp_path):
    trajectory, prediction = write_pair(tmp_path)
    before = trajectory.read_bytes()
    result = conversation_for(prediction, INSTANCE, [(trajectory, '')])
    assert [m['role'] for m in result['messages']] == ['user', 'assistant', 'tool']
    assert result['source'] == trajectory.name and '不是本次评测' in result['note']
    assert trajectory.read_bytes() == before
    prediction.write_text(prediction.read_text().replace('openai/test', 'openai/other'), encoding='utf-8')
    assert conversation_for(prediction, INSTANCE, [(trajectory, '')])['messages'] == []
    prediction.write_text(prediction.read_text().replace('openai/other', 'openai/test').replace('+new', '+different'), encoding='utf-8')
    assert conversation_for(prediction, INSTANCE, [(trajectory, '')])['messages'] == []


def test_unknown_trajectory_is_not_recommended_and_broken_file_is_isolated(tmp_path):
    trajectory, prediction = write_pair(tmp_path)
    unknown = tmp_path / 'unknown.traj.json'
    unknown.write_bytes(trajectory.read_bytes())
    broken = tmp_path / 'broken.traj.json'
    broken.write_text('{"info":', encoding='utf-8')
    result = catalog(INSTANCE, [(unknown, ''), (broken, ''), (prediction, '')])
    assert len(result['items']) == 1 and len(result['rejected']) == 2
    assert inspect_patch(unknown, INSTANCE)['association'] == 'unknown'
    assert conversation_for(prediction, INSTANCE, [(unknown, '')])['messages'] == []


def test_oversized_conversation_is_explicit_and_private_text_redacted(tmp_path):
    trajectory, prediction = write_pair(tmp_path)
    data = json.loads(trajectory.read_text())
    data['messages'] = [{'role': 'assistant', 'content': 'sk-example-offline-secret'}, {'role': 'tool', 'content': 'x' * 200001}]
    trajectory.write_text(json.dumps(data), encoding='utf-8')
    result = conversation_for(prediction, INSTANCE, [(trajectory, '')])
    assert result['truncated'] and len(result['messages']) == 2
    assert result['messages'][1]['display_truncated'] and len(result['messages'][1]['content']) < 5000
    assert result['messages'][0]['content'] == '[REDACTED]'
