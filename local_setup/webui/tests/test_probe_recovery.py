import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from local_setup.webui.common import STATE, save_json
from local_setup.webui.profiles import freeze_profile, list_profiles
from local_setup.webui.profile_model import tool_roundtrip
from local_setup.webui.tests.test_profile_api import api_client, create, preview
from local_setup.webui.tests.test_profiles import workspace


@contextmanager
def diagnostic_service(code=422, delay=0):
    state = {'code': code, 'calls': [], 'delay': delay}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.update(code=200, delay=.8)
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(body)
            time.sleep(state['delay'])
            message = {'role': 'assistant', 'content': 'OK'} if body['tool_choice'] == 'none' else {
                'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'probe', 'type': 'function',
                'function': {'name': 'connection_probe', 'arguments': '{"marker":"mini-probe"}'}}]}
            value = {'error': {'code': 'provider_error', 'message': 'api-fixture-secret '+('invalid parameter '*100)}} if state['code'] != 200 else {
                'choices': [{'message': message}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}
            encoded = json.dumps(value).encode()
            self.send_response(state['code'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(('code', 'category'), [(404, 'endpoint'), (422, 'parameters'), (502, 'upstream')])
def test_failed_probe_can_be_queried_and_explicitly_retried(api_client, code, category):
    with diagnostic_service(code) as (base, service):
        _, profile = create(api_client, base)
        body = preview(api_client, profile)
        endpoint = '/api/profiles/'+profile['id']+'/test'
        record = api_client.post(endpoint, json=body).json()
        assert record['result']['category'] == category and record['result']['http_status'] == code
        assert record['result']['provider_code'] == 'provider_error'
        assert 'api-fixture-secret' not in json.dumps(record) and len(record['result']['message']) < 800
        assert api_client.get(endpoint+'/'+body['request_id']).json() == record
        assert api_client.post(endpoint, json=body).json() == record and len(service['calls']) == 1
        item = next(p for p in api_client.get('/api/profiles').json()['profiles'] if p['id'] == profile['id'])
        assert item['last_test']['status'] == 'failed' and item['test_current']
        service['code'] = 200
        retry = preview(api_client, profile)
        assert retry['request_id'] != body['request_id']
        assert api_client.post(endpoint, json=retry).json()['result']['status'] == 'passed'
        assert len(service['calls']) == 3 and all(r['model'] == 'alias-for-any-model' for r in service['calls'])
        assert api_client.get(endpoint+'/'+body['request_id']).json()['result']['status'] == 'failed'
        assert next(p for p in api_client.get('/api/profiles').json()['profiles'] if p['id'] == profile['id'])['test_current']


def test_old_legacy_success_is_loaded_and_changed_configuration_invalidates_it(tmp_path):
    options = workspace(tmp_path)
    snapshot = freeze_profile('legacy', **options)
    path = options['state'] / 'profiles/tests' / (uuid4().hex+'.json')
    save_json(path, {'profile_id': 'legacy', 'request_id': uuid4().hex, 'status': 'completed',
        'created': '2026-09-20T12:00:00+00:00', 'result': {'status': 'passed', 'fingerprint': snapshot['fingerprint'],
        'checked_at': '2026-09-20T12:00:01+00:00'}})
    original = path.read_bytes()
    item = list_profiles(**options)['profiles'][0]
    assert isinstance(list_profiles(**options)['connections'], list)
    assert item['test_current'] and item['last_test']['status'] == 'passed'
    env = options['root']/'.local/.env'
    env.write_text(env.read_text()+'OPENAI_API_KEY="rotated-test-secret"\n')
    assert not list_profiles(**options)['profiles'][0]['test_current']
    assert path.read_bytes() == original


def test_abandoned_probe_is_read_only_incomplete_and_not_replayed(api_client):
    request_id = uuid4().hex
    path = STATE/'profiles/tests'/(request_id+'.json')
    save_json(path, {'request_id': request_id, 'profile_id': 'legacy', 'status': 'running', 'session': 'previous-boot'})
    original = path.read_bytes()
    endpoint = '/api/profiles/legacy/test'
    assert api_client.get(endpoint+'/'+request_id).json()['status'] == 'incomplete'
    assert api_client.post(endpoint, json={'token': 'old', 'request_id': request_id}).json()['status'] == 'incomplete'
    assert api_client.get('/api/profiles/other/test/'+request_id).status_code == 404
    assert path.read_bytes() == original


def test_timeout_does_not_retry_and_reports_unknown_cost(api_client):
    with diagnostic_service(200, .2) as (base, service):
        _, profile = create(api_client, base)
        snapshot = freeze_profile(profile['id'])
        snapshot['limits']['timeout'] = .03
        result = tool_roundtrip(snapshot, 'api-fixture-secret')
        assert result['status'] == 'incomplete' and result['category'] == 'timeout'
        assert not result['cost_known'] and result['calls'] == 1 and len(service['calls']) == 1
