import ctypes
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from local_setup.webui import profiles
from local_setup.webui.common import STATE, read_json
from local_setup.webui.profile_model import make_profile_model
from local_setup.webui.profiles import freeze_profile
from local_setup.webui.server import manager
from test_profile_api import api_client, create, preview
from test_profiles import local_service


@contextmanager
def protocol_service():
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append(body)
            if self.path.endswith('/responses'):
                output = [{'id': 'message-local', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                           'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}]
                if body.get('tool_choice') != 'none':
                    name = body['tools'][0]['name']
                    output = [{'id': 'function-local', 'type': 'function_call', 'status': 'completed',
                               'name': name, 'call_id': 'probe-local',
                               'arguments': json.dumps({'marker': 'mini-probe'} if name == 'connection_probe' else {'command': 'echo offline'})}]
                value = {'object': 'response', 'id': 'response-local', 'created_at': 1, 'status': 'completed',
                         'model': body['model'], 'output': output, 'parallel_tool_calls': False, 'error': None,
                         'tools': body['tools'], 'tool_choice': body.get('tool_choice', 'auto'),
                         'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                                   'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}
            else:
                message = {'role': 'assistant', 'content': 'OK'}
                if body.get('tool_choice') != 'none':
                    name = body['tools'][0]['function']['name']
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'probe-local', 'type': 'function',
                               'function': {'name': name, 'arguments': json.dumps({'marker': 'mini-probe'} if name == 'connection_probe' else {'command': 'echo offline'})}}]}
                value = {'id': 'chat-local', 'object': 'chat.completion', 'created': 1, 'model': body['model'],
                         'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop' if body.get('tool_choice') == 'none' else 'tool_calls'}],
                         'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
            encoded = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def deny_replacement(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        yield
    finally:
        assert kernel.CloseHandle(handle)


@pytest.mark.parametrize(('protocol', 'output_key'), [('chat', 'max_completion_tokens'), ('responses', 'max_output_tokens')])
def test_preview_probe_and_real_model_query_share_protocol_parameters(api_client, protocol, output_key):
    with protocol_service() as (base, captured):
        _, profile = create(api_client, base, protocol)
        params = {'max_completion_tokens': 640, 'reasoning_effort': 'high'}
        if protocol == 'responses':
            params['extra_body'] = {'reasoning': {'effort': 'high', 'summary': 'auto'}}
        response = api_client.put('/api/profiles/' + profile['id'], json=profile | {'parameters': params})
        assert response.status_code == 200, response.text
        profile = response.json()
        endpoint = '/api/profiles/' + profile['id'] + '/test'
        plan = api_client.post(endpoint + '/preview', json={})
        assert plan.status_code == 200, plan.text
        assert plan.json()['parameters'][output_key] == 256
        body = {'token': plan.json()['token'], 'request_id': uuid4().hex}
        index_before = (STATE / 'profiles/index.json').read_bytes()
        probe = api_client.post(endpoint, json=body)
        assert probe.status_code == 200 and probe.json()['result']['status'] == 'passed', probe.text
        assert (STATE / 'profiles/index.json').read_bytes() == index_before
        assert len(captured) == 2
        snapshot = freeze_profile(profile['id'])
        original = json.dumps(snapshot, sort_keys=True)
        make_profile_model(snapshot, 'api-fixture-secret')._query([{'role': 'user', 'content': 'offline parameter check'}])
        assert json.dumps(snapshot, sort_keys=True) == original
        assert len(captured) == 3
        assert [payload[output_key] for payload in captured] == [256, 256, 640]
        assert all(payload['model'] == plan.json()['api_model_id'] == 'alias-for-any-model' for payload in captured)
        if protocol == 'responses':
            assert plan.json()['parameters']['reasoning'] == {'effort': 'high', 'summary': 'auto'}
            assert all(payload['reasoning'] == {'effort': 'high', 'summary': 'auto'} for payload in captured)
            assert all('reasoning_effort' not in payload and 'max_completion_tokens' not in payload for payload in captured)
        else:
            assert plan.json()['parameters']['reasoning_effort'] == 'high'
            assert all(payload['reasoning_effort'] == 'high' and 'max_tokens' not in payload for payload in captured)


def test_conflicting_extra_parameters_are_rejected_before_http(api_client):
    with local_service() as (base, captured):
        _, profile = create(api_client, base, 'responses')
        endpoint = '/api/profiles/' + profile['id']
        for parameters in ({'temperature': .2, 'extra_body': {'temperature': .7}},
                           {'max_tokens': 100, 'max_completion_tokens': 200}):
            assert api_client.put(endpoint, json=profile | {'parameters': parameters}).status_code == 400
        response = api_client.put(endpoint, json=profile | {'parameters': {
            'reasoning_effort': 'high', 'extra_body': {'reasoning': {'effort': 'low'}}}})
        assert response.status_code == 200
        assert api_client.post(endpoint + '/test/preview', json={}).status_code == 400
        with pytest.raises(ValueError, match='冲突'):
            make_profile_model(freeze_profile(profile['id']), 'api-fixture-secret')
        assert captured == []


def test_closing_missing_probe_blocks_late_dispatch_and_requires_fresh_preview(api_client):
    with local_service() as (base, captured):
        _, profile = create(api_client, base)
        body = preview(api_client, profile)
        endpoint = '/api/profiles/' + profile['id'] + '/test'
        status_url = endpoint + '/' + body['request_id']
        assert api_client.get(status_url).status_code == 404
        assert api_client.post(status_url + '/close', json={'confirmed': False}).status_code == 400
        assert api_client.get(status_url).status_code == 404
        closed = api_client.post(status_url + '/close', json={'confirmed': True})
        assert closed.status_code == 200 and closed.json()['closed'] and closed.json()['status'] == 'incomplete'
        assert read_json(STATE / 'profiles/tests' / (body['request_id'] + '.json')) == closed.json()
        assert api_client.post(endpoint, json=body).json() == closed.json()
        assert api_client.post(status_url + '/close', json={'confirmed': True}).json() == closed.json()
        assert captured == []
        assert api_client.post(endpoint, json=body | {'request_id': uuid4().hex}).status_code == 400
        assert captured == []
        fresh = preview(api_client, profile)
        assert fresh['request_id'] != body['request_id']
        assert api_client.post(endpoint, json=fresh).json()['result']['status'] == 'passed'
        assert len(captured) == 2
        assert api_client.get(status_url).json() == closed.json()
        assert api_client.post(endpoint, json=body).json() == closed.json() and len(captured) == 2


@pytest.mark.skipif(os.name != 'nt', reason='Windows sharing semantics')
def test_probe_final_save_sharing_violation_keeps_terminal_result_without_replay(api_client):
    entered, release = threading.Event(), threading.Event()
    with local_service(entered, release) as (base, captured):
        _, profile = create(api_client, base)
        body = preview(api_client, profile)
        endpoint = '/api/profiles/' + profile['id'] + '/test'
        status_url = endpoint + '/' + body['request_id']
        path = STATE / 'profiles/tests' / (body['request_id'] + '.json')
        identity = (str(STATE), body['request_id'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api_client.post, endpoint, json=body)
            try:
                assert entered.wait(15)
                assert identity in profiles.PROBE_ACTIVE and manager.tool_testing
                assert api_client.get(status_url).json()['status'] == 'running'
                assert api_client.post(status_url + '/close', json={'confirmed': True}).status_code == 409
                assert len(captured) == 1
                with deny_replacement(path):
                    release.set()
                    response = future.result(timeout=20)
                    assert response.status_code == 200 and response.json()['status'] == 'completed', response.text
                    assert response.json()['result']['status'] == 'passed' and response.json()['persistence_error']
                    assert read_json(path)['status'] == 'running'
                    assert not manager.tool_testing and identity not in profiles.PROBE_ACTIVE
                    assert identity in profiles.PROBE_UNSAVED and len(captured) == 2
                    queried = api_client.get(status_url)
                    assert queried.status_code == 200 and queried.json()['status'] == 'completed'
                    assert queried.json()['result'] == response.json()['result'] and queried.json()['persistence_error']
                    assert api_client.post(endpoint, json=body).json()['status'] == 'completed'
                    assert api_client.post(status_url + '/close', json={'confirmed': True}).status_code == 409
                    listed = next(p for p in api_client.get('/api/profiles').json()['profiles'] if p['id'] == profile['id'])
                    assert listed['last_test']['status'] == 'passed' and len(captured) == 2
            finally:
                release.set()
                future.result(timeout=20)
        saved = api_client.get(status_url)
        assert saved.status_code == 200 and saved.json()['status'] == 'completed'
        assert 'persistence_error' not in saved.json() and identity not in profiles.PROBE_UNSAVED
        assert read_json(path) == saved.json()
        assert api_client.post(endpoint, json=body).json() == saved.json() and len(captured) == 2
        assert api_client.post(status_url + '/close', json={'confirmed': True}).json() == saved.json()
        assert not list(path.parent.glob(path.name + '.*.tmp'))
