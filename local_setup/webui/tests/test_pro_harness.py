import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from local_setup.webui.pro_harness import evaluation_command, normalize_result, parse_required
from local_setup.webui.pro_runtime import start_solver, capture_patch, wrap_action
from local_setup.webui.tests.test_pro_data import INSTANCE, fixture_assets, row
from local_setup.webui.pro_data import GH_REVISION, normalize_case


def test_reward_authority_and_required_test_semantics():
    config = {'fail_to_pass': "['repair', 'repair']", 'pass_to_pass': '["regression"]'}
    output = {'tests': [{'name': 'repair', 'status': 'FAILED'}, {'name': 'regression', 'status': 'PASSED'},
                        {'name': 'unrelated', 'status': 'PASSED'}]}
    failed = normalize_result(config, output, '0\n')
    assert not failed['resolved'] and failed['official_reward'] == 0
    assert failed['tests_status'] == {'FAIL_TO_PASS': {'success': [], 'failure': ['repair']},
                                       'PASS_TO_PASS': {'success': ['regression'], 'failure': []}}
    output['tests'].append({'name': 'repair', 'status': 'PASSED'})
    assert normalize_result(config, output, '1')['resolved']
    assert not normalize_result(config, output, '0')['resolved']
    with pytest.raises(ValueError, match='reward'):
        normalize_result(config, output, '2')
    with pytest.raises(ValueError, match='明细'):
        normalize_result(config, {'invalid': []}, '1')
    with pytest.raises(ValueError, match='任何测试'):
        normalize_result(config, {'tests': []}, '0')
    with pytest.raises(ValueError, match='有效测试'):
        normalize_result({'fail_to_pass': [], 'pass_to_pass': []}, output, '1')
    with pytest.raises(ValueError):
        parse_required('{"not": "a list"}')
    with pytest.raises(ValueError, match='镜像'):
        start_solver('ghcr.io/scaleapi/swe-bench_pro-v2:mutable', 'mini-web-test')
    with pytest.raises(ValueError, match='容器名称'):
        start_solver('sha256:' + '1' * 64, 'unrelated-container')
    with pytest.raises(ValueError, match='目录'):
        capture_patch('unowned', '/')
    with pytest.raises(ValueError, match='基线'):
        capture_patch('unowned', '/app', 'mutable-head')
    for seconds in (0, -1, float('nan'), 3001):
        with pytest.raises(ValueError, match='时间限制'):
            wrap_action('echo untouched', seconds)


class ProDockerProtocol(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, value=b'', raw=False):
        content = value if raw else json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/x-tar' if raw else 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Api-Version', '1.44')
        self.end_headers()
        self.wfile.write(content)

    def do_HEAD(self):
        self.server.requests.append(('HEAD', self.path))
        self.send(200, b'', raw=True)

    def do_GET(self):
        self.server.requests.append(('GET', self.path))
        if self.path == '/version':
            self.send(200, {'ApiVersion': '1.44', 'MinAPIVersion': '1.24'})
        elif self.path.endswith('/info'):
            self.send(200, {'OSType': 'linux'})
        elif '/images/' in self.path:
            if self.server.scenario == 'missing_image':
                self.send(404, {'message': 'Image missing'})
            else:
                self.send(200, {'Id': 'sha256:' + '1' * 64, 'RepoTags': [self.server.row['docker_image']]})
        elif '/exec/' in self.path:
            command = self.server.commands[self.path.split('/')[-2]]
            code = 0
            if 'bash /tests/test.sh' in command:
                code = 124 if self.server.scenario == 'timeout' else 42 if self.server.scenario == 'passed_nonzero' else 1 if self.server.scenario == 'failed' else 0
            if 'git apply --verbose' in command and self.server.scenario == 'bad_patch':
                code = 1
            self.send(200, {'Running': False, 'ExitCode': code, 'Pid': 0})
        elif '/archive?' in self.path:
            path = parse_qs(urlsplit(self.path).query)['path'][0]
            if path == '/tmp/pro-repository.txt':
                head = 'f' * 40 if self.server.scenario == 'wrong_head' else self.server.row['base_commit']
                files = {'pro-repository.txt': ('/app\n' + head + '\n').encode()}
            elif path == '/tmp/pro-apply.txt':
                files = {'pro-apply.txt': b'patch application transcript'}
            else:
                assert path == '/logs/verifier'
                config = json.loads(self.server.assets['tests/config.json'])
                repair = parse_required(config['fail_to_pass'])
                regression = parse_required(config['pass_to_pass'])
                passing = regression if self.server.scenario == 'failed' else repair | regression
                output = {'tests': [{'name': name, 'status': 'PASSED' if name in passing else 'FAILED'}
                                    for name in sorted(repair | regression)]}
                files = {'reward.txt': b'0\n' if self.server.scenario == 'failed' else b'1\n',
                         'output.json': json.dumps(output).encode(), 'run-script-stdout.txt': b'raw stdout',
                         'run-script-stderr.txt': b'raw stderr', 'test_output.txt': b'official verifier transcript'}
                if self.server.scenario == 'missing_reward':
                    files.pop('reward.txt')
                if self.server.scenario == 'missing_output':
                    files.pop('output.json')
                if self.server.scenario == 'malformed_output':
                    files['output.json'] = b'{invalid'
                if self.server.scenario == 'no_tests':
                    files['output.json'] = b'{"tests": []}'
                if self.server.scenario == 'bad_reward':
                    files['reward.txt'] = b'2'
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as archive:
                for name, value in files.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(value)
                    archive.addfile(member, io.BytesIO(value))
            self.send(200, buffer.getvalue(), raw=True)
        elif '/containers/' in self.path:
            self.send(200, {'Id': 'a' * 64, 'Name': '/sweb.eval.' + INSTANCE.lower() + '.protocol',
                            'Config': {}, 'State': {'Running': True}})
        else:
            self.send(400, {'message': 'Unexpected request'})

    def do_POST(self):
        self.server.requests.append(('POST', self.path))
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        data = json.loads(body) if body else {}
        if '/containers/create?' in self.path:
            self.server.creation = data
            self.send(201, {'Id': 'a' * 64, 'Warnings': []})
        elif self.path.endswith('/exec'):
            identity = str(len(self.server.commands))
            self.server.commands[identity] = data['Cmd'][-1]
            self.send(201, {'Id': identity})
        elif '/exec/' in self.path and self.path.endswith('/start'):
            assert data['Detach'] is True
            self.send(200, b'', raw=True)
        elif self.path.endswith('/start'):
            self.send(204, b'', raw=True)
        else:
            self.send(400, {'message': 'Pull/build is not allowed'})

    def do_PUT(self):
        self.server.requests.append(('PUT', self.path))
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        with tarfile.open(fileobj=io.BytesIO(body)) as archive:
            for entry in archive:
                self.server.uploads[entry.name] = archive.extractfile(entry).read()
        self.send(200, b'', raw=True)

    def do_DELETE(self):
        self.server.requests.append(('DELETE', self.path))
        if self.server.scenario == 'cleanup_failure':
            self.send(500, {'message': 'Cannot remove this container'})
        else:
            self.send(204, b'', raw=True)


@pytest.mark.parametrize(('scenario',), [(case,) for case in [
    'passed', 'failed', 'passed_nonzero', 'empty_patch', 'missing_image', 'wrong_image', 'wrong_head',
    'bad_patch', 'missing_reward', 'missing_output', 'malformed_output', 'no_tests', 'bad_reward', 'timeout', 'cleanup_failure',
]])
def test_actual_entrypoint_with_official_cached_assets_and_local_docker(tmp_path, scenario):
    public, assets = row(), fixture_assets()
    state = tmp_path / 'state'
    root = state / 'pro-assets' / GH_REVISION[:12] / hashlib.sha256(INSTANCE.encode()).hexdigest()[:16]
    for name, data in assets.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    normalized = normalize_case(public, {name: root / name for name in assets})
    dataset = tmp_path / 'dataset.json'
    dataset.write_text(json.dumps([normalized]), encoding='utf-8')
    patch = '' if scenario == 'empty_patch' else 'diff --git a/a b/a\r\n--- a/a\r\n+++ b/a\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n'
    predictions = tmp_path / 'predictions.jsonl'
    predictions.write_text(json.dumps({'instance_id': INSTANCE, 'model_name_or_path': 'offline-model',
                                        'model_patch': patch}) + '\n', encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0), ProDockerProtocol)
    server.requests, server.commands, server.uploads, server.creation = [], {}, {}, {}
    server.scenario, server.row, server.assets = scenario, public, assets
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    expected = 'sha256:' + ('2' if scenario == 'wrong_image' else '1') * 64
    command, env = evaluation_command(dataset, predictions, INSTANCE, 'protocol', tmp_path, 1, expected)
    env = os.environ | env | {'DOCKER_HOST': f'tcp://127.0.0.1:{server.server_port}', 'MINI_WEB_STATE': str(state)}
    for key in ('DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    try:
        result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, encoding='utf-8',
                                timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    summary = json.loads((tmp_path / 'offline-model.protocol.json').read_text(encoding='utf-8'))
    detail_path = next(tmp_path.glob('logs/run_evaluation/pro/*/report.json'))
    detail = json.loads(detail_path.read_text(encoding='utf-8'))[INSTANCE]
    assert len(detail_path.parent.name) == 16
    assert not any('/images/create' in path or '/build' in path for _, path in server.requests)
    assert not any('instruction' in key or 'solution' in key or 'gold' in key for key in server.uploads)
    if scenario in {'missing_image', 'wrong_image'}:
        assert result.returncode != 0 and not server.creation
        assert summary['completed_instances'] == 0 and summary['error_instances'] == 1
        return
    assert server.creation['Image'] == 'sha256:' + '1' * 64
    assert server.creation['Entrypoint'] == ['/bin/bash', '-lc']
    assert server.creation['HostConfig']['NetworkMode'] == 'bridge'
    assert server.creation['HostConfig']['NanoCpus'] == 1_000_000_000
    assert server.creation['HostConfig']['Memory'] == 4096 * 1024 * 1024
    assert any(method == 'DELETE' for method, _ in server.requests)
    assert not server.creation.get('Env')
    if scenario not in {'wrong_head', 'bad_patch'}:
        assert all(server.uploads[name] == assets['tests/' + name]
                   for name in ['test.sh', 'run_script.sh', 'parser.py', 'config.json', 'test_patch.patch'])
    if scenario == 'empty_patch':
        assert 'replay.patch' not in server.uploads and summary['empty_patch_instances'] == 1
    elif scenario != 'wrong_head':
        assert b'\r\n' not in server.uploads['replay.patch']
    if scenario in {'passed', 'failed', 'passed_nonzero', 'empty_patch'}:
        assert result.returncode == 0, result.stdout + result.stderr
        assert summary['completed_instances'] == 1 and summary['error_instances'] == 0
        assert summary['resolved_instances'] == int(scenario != 'failed')
        assert detail['official_reward'] == int(scenario != 'failed')
        assert detail['tests_status']['PASS_TO_PASS']['failure'] == []
        if scenario == 'failed':
            assert detail['tests_status']['FAIL_TO_PASS']['failure']
        assert (detail_path.parent / 'output.json').is_file()
    else:
        assert result.returncode != 0 and summary['error_instances'] == 1
        if scenario == 'cleanup_failure':
            assert summary['unremoved_containers'] and 'cleanup_error' in detail
        else:
            assert summary['completed_instances'] == 0 and summary['infra_failure_instances'] == 1


@pytest.mark.skipif(shutil.which('docker') is None, reason='A real Docker CLI forwards to the local protocol service')
@pytest.mark.parametrize(('scenario',), [('passed',), ('failed',)])
def test_worker_to_pro_grader_preserves_result_and_snapshot(tmp_path, scenario):
    from local_setup.webui.benchmarks import PRO_ID, case_key, snapshot
    public, assets = row(), fixture_assets()
    state, config = tmp_path / 's', tmp_path / 'c'
    root = state / 'pro-assets' / GH_REVISION[:12] / hashlib.sha256(INSTANCE.encode()).hexdigest()[:16]
    for name, data in assets.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    normalized = normalize_case(public, {name: root / name for name in assets})
    normalized.update(dataset=PRO_ID, case_key=case_key(PRO_ID, INSTANCE), benchmark_snapshot=snapshot(PRO_ID))
    dataset = state / 'datasets' / PRO_ID / (INSTANCE + '.json')
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps([normalized]), encoding='utf-8')
    predictions = tmp_path / 'input.predictions.jsonl'
    predictions.write_text(json.dumps({'instance_id': INSTANCE, 'model_name_or_path': 'offline-pro',
        'model_patch': 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n',
        'dataset': PRO_ID, 'case_key': case_key(PRO_ID, INSTANCE), 'benchmark_snapshot': snapshot(PRO_ID)}) + '\n', encoding='utf-8')
    folder = state / 'runs' / '20261010-000000-1234abcd'
    folder.mkdir(parents=True)
    (folder / 'request.json').write_text(json.dumps({'kind': 'evaluate', 'dataset': PRO_ID, 'instance_id': INSTANCE,
        'benchmark_snapshot': snapshot(PRO_ID), 'prediction_path': str(predictions),
        'allow_data_download': False, 'allow_image_download': False}), encoding='utf-8')
    (config / '.local').mkdir(parents=True)
    (config / '.local/.env').write_text("MSWEA_MODEL_NAME='openai/offline-pro'\n", encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0), ProDockerProtocol)
    server.requests, server.commands, server.uploads, server.creation = [], {}, {}, {}
    server.scenario, server.row, server.assets = scenario, public, assets
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    docker_config = tmp_path / 'docker-config'
    docker_config.mkdir()
    env = os.environ | {'MINI_WEB_STATE': str(state), 'MINI_WEB_CONFIG_ROOT': str(config),
        'MSWEA_GLOBAL_CONFIG_DIR': str(config / '.local'), 'MSWEA_CONFIGURED': 'true',
        'DOCKER_CONFIG': str(docker_config), 'DOCKER_CONTEXT': 'pro-worker-protocol', 'PYTHONUTF8': '1'}
    for key in ('DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    try:
        context = subprocess.run(['docker', '--config', str(docker_config), 'context', 'create', 'pro-worker-protocol',
            '--docker', f'host=tcp://127.0.0.1:{server.server_port}'], env=env, capture_output=True,
            text=True, encoding='utf-8', timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert context.returncode == 0, context.stderr
        process = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'local_setup.webui.worker', folder.name],
            cwd=Path(__file__).resolve().parents[3], env=env, capture_output=True, text=True,
            encoding='utf-8', timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert process.returncode == 0, process.stdout + process.stderr
    events = [json.loads(line.removeprefix('@@MINI_WEB@@')) for line in process.stdout.splitlines()
              if line.startswith('@@MINI_WEB@@')]
    assert {'kind': 'state', 'data': 'completed'} in events
    evaluation = next(event['data'] for event in events if event['kind'] == 'evaluation')
    assert evaluation['completed'] == 1 and evaluation['resolved'] == int(scenario == 'passed')
    assert evaluation['errors'] == 0 and evaluation['detail'][INSTANCE]['official_reward'] == int(scenario == 'passed')
    assert not any(event['kind'] in {'approval', 'billing', 'error'} for event in events)
    assert not any('/images/create' in path or '/build' in path for _, path in server.requests)
    assert any(method == 'DELETE' for method, _ in server.requests)
