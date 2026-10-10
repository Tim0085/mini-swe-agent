import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_setup.webui.common import ROOT
from local_setup.webui.profiles import (
    check_snapshot, freeze_profile, list_profiles, load_snapshot_secret, profile_price_available,
    refresh_credentials, save_connection, save_connection_key, save_profile, set_default, validate_parameters,
    test_profile as run_profile_test,
)
from local_setup.webui.profile_model import make_profile_model, priced_usage, tool_roundtrip


def workspace(tmp_path):
    root = tmp_path / 'config'
    (root / '.local').mkdir(parents=True)
    (root / 'local_setup').mkdir()
    (root / '.local/.env').write_text("MSWEA_MODEL_NAME='openai/legacy-offline'\nOPENAI_API_BASE='https://example.invalid/v1'\n", encoding='utf-8')
    (root / 'local_setup/windows.yaml').write_bytes((ROOT / 'local_setup/webui/defaults.yaml').read_bytes())
    return {'root': root, 'state': tmp_path / 'state'}


def profile(connection, **kwargs):
    return {'name': '测试模型', 'connection_id': connection['id'], 'model_name': 'alias-for-any-model',
            'pricing': {'mode': 'custom', 'input_per_million': 2, 'output_per_million': 6}, **kwargs}


def test_profiles_preserve_legacy_and_isolate_price_versions(tmp_path):
    options = workspace(tmp_path)
    original = {str(p): p.read_bytes() for p in options['root'].rglob('*') if p.is_file()}
    a = save_connection({'name': '中转 A', 'api_base': 'https://a.invalid/v1'}, **options)
    b = save_connection({'name': '中转 B', 'api_base': 'https://b.invalid/v1'}, **options)
    pa = save_profile(profile(a), **options)
    pb = save_profile(profile(b, pricing={'mode': 'custom', 'input_per_million': 4, 'output_per_million': 12}), **options)
    sa = freeze_profile(pa['id'], {'step_limit': 12, 'cost_limit': 1}, **options)
    sb = freeze_profile(pb['id'], **options)
    response = {'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 10}}}
    assert priced_usage(response, sa['pricing'])['cost'] * 2 == priced_usage(response, sb['pricing'])['cost']
    assert not check_snapshot(sa, **options)['changed']
    assert sa['limits']['step_limit'] == 12 and profile_price_available(sa)
    assert not profile_price_available('legacy', **options)
    set_default(pa['id'], **options)
    assert list_profiles(**options)['default_profile_id'] == pa['id']
    assert {str(p): p.read_bytes() for p in options['root'].rglob('*') if p.is_file()} == original
    save_profile(pb | {'name': '另一个档案改名'}, **options)
    assert not check_snapshot(sa, **options)['changed']
    save_profile(pa | {'parameters': {'temperature': .5}}, **options)
    assert check_snapshot(sa, **options) == {'changed': True, 'credentials_only': False, 'reason': '模型参数、价格或服务配置已变化，请重新建立任务。'}
    with pytest.raises(ValueError):
        refresh_credentials(sa, **options)


def test_credentials_are_encrypted_and_rotation_requires_confirmation(tmp_path):
    options = workspace(tmp_path)
    connection = save_connection({'name': '连接', 'api_base': 'https://example.invalid/v1'}, **options)
    connection = save_connection_key(connection['id'], 'test-local-secret-one', connection['version'], **options)
    saved = save_profile(profile(connection), **options)
    snapshot = freeze_profile(saved['id'], **options)
    assert load_snapshot_secret(snapshot, **options) == 'test-local-secret-one'
    assert 'test-local-secret-one' not in json.dumps(snapshot)
    save_connection_key(connection['id'], 'test-local-secret-two', connection['version'], **options)
    assert check_snapshot(snapshot, **options)['credentials_only']
    with pytest.raises(ValueError):
        load_snapshot_secret(snapshot, **options)
    updated = refresh_credentials(snapshot, **options)
    assert load_snapshot_secret(updated, **options) == 'test-local-secret-two'
    assert snapshot['configuration_fingerprint'] == updated['configuration_fingerprint']
    assert all(b'test-local-secret' not in p.read_bytes() for p in options['state'].rglob('*') if p.is_file())


@contextmanager
def local_service(entered=None, release=None):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            encoded = json.dumps({'data': [{'id': 'alias-for-any-model'}, {'id': 'claude-custom-alias'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append(body)
            if len(captured) == 1 and entered is not None:
                entered.set()
                if release is not None:
                    release.wait(20)
            usage = {'prompt_tokens': 100, 'completion_tokens': 20}
            if self.path.endswith('/responses'):
                output = [{'type': 'function_call', 'name': 'connection_probe', 'call_id': 'probe-1', 'arguments': '{"marker":"mini-probe"}'}] if body.get('tool_choice') != 'none' else [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]
                value = {'object': 'response', 'id': 'response-local', 'model': body['model'], 'output': output, 'usage': {'input_tokens': 100, 'output_tokens': 20}}
            else:
                if body.get('tool_choice') == 'none':
                    message = {'role': 'assistant', 'content': 'OK'}
                else:
                    probe = bool(body.get('tool_choice'))
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'connection_probe' if probe else 'bash', 'arguments': '{"marker":"mini-probe"}' if probe else '{"command":"echo offline"}'}}]}
                value = {'id': 'chat-local', 'object': 'chat.completion', 'created': 1, 'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop' if body.get('tool_choice') == 'none' else 'tool_calls'}], 'usage': usage}
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
        if release is not None:
            release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(('protocol',), [('chat',), ('responses',)])
def test_two_request_inert_tool_roundtrip(tmp_path, protocol):
    options = workspace(tmp_path)
    with local_service() as (base, captured):
        connection = save_connection({'name': '本机协议检查', 'api_base': base}, **options)
        saved = save_profile(profile(connection, protocol=protocol), **options)
        snapshot = freeze_profile(saved['id'], **options)
        result = tool_roundtrip(snapshot, 'local-only-credential')
        assert result['status'] == 'passed' and result['calls'] == 2 and len(captured) == 2
        assert result['usage']['input_tokens'] == 200 and result['cost'] > 0
        assert captured[1]['tool_choice'] == 'none'
        assert 'local-only-credential' not in json.dumps(result)
        messages = captured[1]['input' if protocol == 'responses' else 'messages']
        assert any(x.get('role') == 'tool' or x.get('type') == 'function_call_output' for x in messages)


def test_model_uses_task_prices_without_global_mutation_or_secret_serialization(tmp_path):
    import litellm
    options = workspace(tmp_path)
    before = dict(litellm.model_cost.get('alias-for-any-model', {}))
    with local_service() as (base, captured):
        connection = save_connection({'name': '本机模型', 'api_base': base}, **options)
        saved = save_profile(profile(connection, model_name='claude-custom-alias'), **options)
        snapshot = freeze_profile(saved['id'], **options)
        model = make_profile_model(snapshot, 'local-only-credential')
        response = model.query([{'role': 'user', 'content': 'offline test'}])
        assert response['extra']['actions'][0]['command'] == 'echo offline'
        assert response['extra']['cost'] == pytest.approx(.00032)
        assert model.config.set_cache_control is None and model.config.model_kwargs['drop_params'] is False
        assert 'local-only-credential' not in json.dumps(model.serialize())
        assert captured[0]['model'] == 'claude-custom-alias'
    assert dict(litellm.model_cost.get('alias-for-any-model', {})) == before
    with pytest.raises(ValueError, match='使用量'):
        priced_usage({'usage': {}}, snapshot['pricing'])


def test_profile_rejects_secret_parameters_and_limits_test_before_request(tmp_path):
    options = workspace(tmp_path)
    with pytest.raises(ValueError, match='独立密钥'):
        save_connection({'name': '拒绝', 'api_base': 'https://example.invalid', 'key': 'never-write-this'}, **options)
    for params in [{'extra_body': {'api_key': 'never-send'}}, {'extra_body': {'max_tokens': 99999}}, {'max_tokens': 128, 'max_completion_tokens': 256}]:
        with pytest.raises(ValueError):
            validate_parameters(params)
    with local_service() as (base, captured):
        conn = save_connection({'name': '本机', 'api_base': base}, **options)
        saved = save_profile(profile(conn, pricing={'mode': 'custom', 'input_per_million': 100, 'output_per_million': 100}), **options)
        with pytest.raises(ValueError, match='超过'):
            tool_roundtrip(freeze_profile(saved['id'], **options), 'local-only-credential', budget_limit=.01)
        assert not captured
        frozen = freeze_profile(saved['id'], **options)
        save_profile(saved | {'model_name': 'different-after-preview'}, **options)
        with pytest.raises(ValueError, match='模型参数'):
            run_profile_test(saved['id'], confirmed=True, snapshot=frozen, **options)
        assert not captured


def test_cached_usage_price_and_unknown_cost_boundaries():
    pricing = {'available': True, 'source': 'custom', 'input_per_million': 2, 'output_per_million': 6, 'cached_input_per_million': .5}
    deepseek = {'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_cache_hit_tokens': 60}}
    responses = {'usage': {'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {'cached_tokens': 60}}}
    assert priced_usage(deepseek, pricing) == priced_usage(responses, pricing)
    assert priced_usage(deepseek, pricing)['cost'] == pytest.approx(.00023)
    for invalid in [{}, {'usage': {'prompt_tokens': 100}}, {'usage': {'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {'cached_tokens': 101}}}]:
        with pytest.raises(ValueError, match='使用量'):
            priced_usage(invalid, pricing)
    assert priced_usage(deepseek, pricing | {'input_per_million': 0, 'output_per_million': 0, 'cached_input_per_million': 0})['cost'] == 0
