import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from local_setup.webui.common import ROOT, STATE, redact, read_json
from local_setup.webui.settings import Settings, read_settings, save_settings, replace_key
from local_setup.webui.server import app, manager, NONCE
from local_setup.webui.bench import prediction, summarize_report, load_instance, check_harness


@pytest.fixture
def client():
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.post('/session', json={'nonce': NONCE}).status_code == 200
        yield client
        if manager.active:
            manager.cancel(manager.active)
        deadline = time.time() + 20
        while manager.active and time.time() < deadline:
            time.sleep(.1)
        assert manager.active is None


def wait_for(client, run_id, states):
    deadline = time.time() + 40
    while time.time() < deadline:
        run = client.get('/api/runs/' + run_id).json()
        if run['status'] in states:
            return run
        if run['status'] == 'failed':
            pytest.fail(str(run))
        time.sleep(.15)
    pytest.fail('Timed out: ' + str(run))


def start(client, folder):
    return {'kind': 'offline', 'task': '中文离线验证', 'cwd': str(folder), 'request_id': uuid4().hex,
            'settings_version': client.get('/api/config').json()['version']}


def test_security_and_validation(client):
    assert client.get('/api/config', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/config', headers={'Host': 'evil.example'}).status_code == 403
    assert client.post('/api/runs', content='{}', headers={'Content-Type': 'text/plain'}).status_code == 415
    secret = 'sk-testfixture-do-not-echo'
    result = client.put('/api/key', json={'key': secret, 'version': 17})
    assert result.status_code == 422 and secret not in result.text
    client.cookies.clear()
    assert client.get('/api/config').status_code == 401
    assert client.post('/session', json={'nonce':'wrong'}).status_code == 403


def test_directory_browser(client, tmp_path):
    (tmp_path / '中文目录').mkdir()
    (tmp_path / '.venv').mkdir()
    response = client.get('/api/directories', params={'path':str(tmp_path)})
    assert response.status_code == 200
    assert response.json()['children'] == [{'name':'中文目录','path':str(tmp_path / '中文目录')}]
    assert client.get('/api/directories',params={'path':str(tmp_path / 'missing')}).status_code == 400


def test_real_worker_approval_rejection_reconnect_and_duplicate(client, tmp_path):
    folder = tmp_path / '中文项目'
    folder.mkdir()
    body = start(client, folder)
    response = client.post('/api/runs', json=body)
    assert response.status_code == 200, response.text
    run_id = response.json()['id']
    assert client.post('/api/runs', json=body).json()['id'] == run_id
    assert client.post('/api/runs', json=start(client, folder)).status_code == 409
    run = wait_for(client, run_id, {'awaiting_confirmation'})
    ticket = run['pending']['ticket']
    assert len(run['pending']['actions']) == 1 and str(folder) == run['pending']['cwd']
    assert client.get('/api/runs/' + run_id).json()['pending']['ticket'] == ticket
    answer = {'ticket': ticket, 'approve': False, 'feedback': '请结束离线检查'}
    assert client.post(f'/api/runs/{run_id}/approve', json=answer).status_code == 200
    assert client.post(f'/api/runs/{run_id}/approve', json=answer).status_code == 409
    run = wait_for(client, run_id, {'awaiting_confirmation'})
    assert run['pending']['ticket'] != ticket
    assert client.post(f'/api/runs/{run_id}/approve', json={'ticket':run['pending']['ticket'], 'approve': True}).status_code == 200
    run = wait_for(client, run_id, {'completed'})
    assert run['stats'] == {'calls': 2, 'cost': 0.0}
    assert run['result']['exit_status'] == 'Submitted'
    artifact = client.get(f'/api/runs/{run_id}/artifact/trajectory.traj.json')
    assert artifact.status_code == 200 and 'User rejected' in artifact.text
    events = client.get(f'/api/runs/{run_id}/events?after=2')
    assert 'event: done' in events.text and 'id: 1\n' not in events.text
    assert client.get(f'/api/runs/{run_id}/artifact/request.json').status_code == 404


def test_cancel_owns_worker_only(client, tmp_path):
    run_id = client.post('/api/runs', json=start(client,tmp_path)).json()['id']
    wait_for(client, run_id, {'awaiting_confirmation'})
    process = manager.process
    assert client.post(f'/api/runs/{run_id}/cancel', json={}).json()['status'] == 'stopping'
    process.wait(timeout=15)
    wait_for(client,run_id,{'cancelled'})
    assert process.poll() is not None
    assert client.get(f'/api/runs/{run_id}').json()['pending'] is None


def test_settings_roundtrip_comments_unknown_fields_conflict_and_key(tmp_path):
    (tmp_path / '.local').mkdir()
    (tmp_path / 'local_setup').mkdir()
    env = tmp_path / '.local/.env'
    config = tmp_path / 'local_setup/windows.yaml'
    env.write_bytes(b"# keep env comment\r\nMSWEA_MODEL_NAME='openai/old'\r\nOPENAI_API_BASE='https://example.com/v1'\r\nOTHER='keep'\r\n")
    original = (ROOT / 'local_setup/windows.yaml').read_text(encoding='utf-8') + '\n# keep yaml comment\ncustom: untouched\n'
    config.write_bytes(b'\xef\xbb\xbf' + original.replace('\n','\r\n').encode())
    before = read_settings(tmp_path)
    changed = Settings(**({k:before[k] for k in Settings.model_fields} | {'model_name':'new-model','api_base':'https://new.example/v1','timeout':60}))
    after = save_settings(changed,before['version'],tmp_path)
    assert after['model_name'] == 'openai/new-model' and after['timeout'] == 60
    assert config.read_bytes().startswith(b'\xef\xbb\xbf') and b'\r\n' in config.read_bytes()
    assert b'custom: untouched' in config.read_bytes() and b'# keep yaml comment' in config.read_bytes()
    assert b"OTHER='keep'\r\n" in env.read_bytes()
    assert all(line.endswith(b'\r\n') for line in env.read_bytes().splitlines(keepends=True))
    with pytest.raises(ValueError,match='其他窗口'):
        save_settings(changed,before['version'],tmp_path)
    replace_key('offline-dpapi-test-fixture',tmp_path)
    assert b'offline-dpapi-test-fixture' not in (tmp_path / '.local/api-key.dpapi').read_bytes()
    assert read_settings(tmp_path)['key_configured']


def test_prediction_and_official_report_boundaries(tmp_path):
    traj = tmp_path / 'input.traj.json'
    traj.write_text(json.dumps({'instance_id':'django__django-10097','info':{'exit_status':'Submitted','submission':'diff --git a/x b/x\r\n+x\r\n','config':{'model':{'model_name':'openai/test'}}}}),encoding='utf-8')
    assert '\r' not in prediction(traj,'django__django-10097')['model_patch']
    with pytest.raises(ValueError,match='不一致'):
        prediction(traj,'django__django-10098')
    assert summarize_report({'completed_instances':1,'resolved_instances':0,'unresolved_instances':1,'error_instances':0})['category']=='test_failed'
    assert summarize_report({'completed_instances':0,'error_instances':1})['category']=='incomplete'
    check_harness(load_instance('verified','django__django-10097'))


def test_reuse_preserves_payload_and_does_not_silently_redact(client,tmp_path):
    body=start(client,tmp_path)
    run=client.post('/api/runs',json=body).json()
    wait_for(client,run['id'],{'awaiting_confirmation'})
    response=client.get('/api/runs/'+run['id']+'/reuse')
    assert response.status_code==200
    assert response.json()['task']==body['task'] and response.json()['cwd']==body['cwd']
    assert not response.json()['auto_approve'] and not response.json()['allow_download']
    request_file=STATE/'runs'/run['id']/'request.json'
    payload=json.loads(request_file.read_text(encoding='utf-8'))
    payload['task']='example sk-not-a-real-fixture-key'
    request_file.write_text(json.dumps(payload),encoding='utf-8')
    assert client.get('/api/runs/'+run['id']+'/reuse').status_code==409
    assert json.loads(request_file.read_text(encoding='utf-8'))['task']==payload['task']


def test_windows_command_unicode_failure_and_timeout(tmp_path):
    from windows_env import WindowsLocalEnvironment
    env = WindowsLocalEnvironment(cwd=str(tmp_path),timeout=1)
    assert '中文' in env.execute({'command': "printf '中文\\n'\nprintf 'second line\\n'"})['output']
    assert env.execute({'command':'exit 7'})['returncode']==7
    assert env.execute({'command':'sleep 5'})['exception_info']=='Command timed out.'


def test_redaction_nested():
    assert redact({'api_key':'private','nested':[{'content':'Bearer fakecredential sk-test1234567'}]}) == {'api_key':'[REDACTED]','nested':[{'content':'Bearer [REDACTED] [REDACTED]'}]}
