import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from local_setup.webui.rebench_harness import SOURCE, check_harness, evaluation_command, source_manifest


def example():
    return {'instance_id': 'example__project-42_interface', 'repo': 'example/project', 'base_commit': 'a' * 40,
            'version': '1', 'problem_statement': 'Repair the function.',
            'test_patch': 'diff --git a/tests/test_example.py b/tests/test_example.py\n--- a/tests/test_example.py\n+++ b/tests/test_example.py\n@@ -1 +1 @@\n-old\n+new\n',
            'FAIL_TO_PASS': ['tests/test_example.py::test_fix'], 'PASS_TO_PASS': ['tests/test_example.py::test_regression'],
            'image_name': 'swerebench/sweb.eval.x86_64.example_1776_project-42:latest',
            'docker_image': 'swerebench/sweb.eval.x86_64.example_1776_project-42:latest',
            'install_config': {'log_parser': 'parse_log_pytest', 'test_cmd': 'pytest -rA', 'python': '3.12',
                               'packages': '', 'install': 'python -m pip install -e .', 'docker_specs': {}}}


def run_python(code, *args):
    return subprocess.run([sys.executable, '-X', 'utf8', '-c', code, *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8', timeout=30,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def test_official_source_and_script_identity(tmp_path):
    manifest = source_manifest()
    for name, value in manifest['files'].items():
        if name not in manifest['changes']:
            assert value['upstream_sha256'] == hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
    row_path = tmp_path / 'case.json'
    row_path.write_text(json.dumps(example()), encoding='utf-8')
    check = run_python('''
import json, sys
from pathlib import Path
from local_setup.webui.rebench_harness import load_official, make_spec
load_official()
from swebench.harness.test_spec.test_spec import make_test_spec
row=json.loads(Path(sys.argv[1]).read_text())
for explicit_image in (True, False):
    if not explicit_image: row.pop('image_name')
    wrapped=make_spec(row)
    official=make_test_spec(row, namespace='swerebench')
    assert wrapped.eval_script==official.eval_script
    assert wrapped.FAIL_TO_PASS==official.FAIL_TO_PASS and wrapped.PASS_TO_PASS==official.PASS_TO_PASS
    assert wrapped.repo_script_list==wrapped.env_script_list==[]
    assert wrapped.get_instance_container_name('isolated-check')=='sweb.eval.example__project-42_interface.isolated-check'
print('official-script-equivalence')
''', row_path)
    assert check.returncode == 0, check.stderr
    assert 'official-script-equivalence' in check.stdout
    check_harness(example())
    invalid = example()
    invalid['docker_image'] = 'someoneelse/evaluation:latest'
    with pytest.raises(ValueError, match='官方'):
        check_harness(invalid)
    invalid = example()
    invalid['install_config']['log_parser'] = 'unsupported_parser'
    with pytest.raises(ValueError, match='log_parser'):
        check_harness(invalid)


class DockerProtocol(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, value=b'', raw=False):
        body = value if raw else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/vnd.docker.raw-stream' if raw else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Api-Version', '1.44')
        self.end_headers()
        if raw and body and '/exec/' in self.path:
            self.wfile.flush()
            # Docker switches to raw socket reads after the HTTP handshake.
            threading.Event().wait(.05)
        self.wfile.write(body)

    def do_HEAD(self):
        self.server.requests.append(('HEAD', self.path))
        if self.path == '/_ping':
            self.send(200, b'', raw=True)
        else:
            self.send(400, b'', raw=True)

    def do_GET(self):
        self.server.requests.append(('GET', self.path))
        if self.path == '/version':
            self.send(200, {'ApiVersion': '1.44', 'MinAPIVersion': '1.24'})
        elif self.path == '/_ping':
            self.send(200, b'OK', raw=True)
        elif self.path.endswith('/info'):
            self.send(200, {'OSType': 'linux'})
        elif '/images/' in self.path:
            if self.server.image_present:
                self.send(200, {'Id': 'sha256:' + '1' * 64, 'RepoTags': [example()['docker_image']]})
            else:
                self.send(404, {'message': 'Image missing'})
        elif '/exec/' in self.path:
            self.send(200, {'Running': False, 'ExitCode': 0, 'Pid': 0})
        elif '/containers/' in self.path:
            self.send(200, {'Id': 'a' * 64, 'Name': '/sweb.eval.example__project-42_interface.protocol', 'Config': {}, 'State': {'Running': True}})
        else:
            self.send(400, {'message': 'Unexpected request'})

    def do_POST(self):
        self.server.requests.append(('POST', self.path))
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        data = json.loads(body) if body else {}
        if '/containers/create?name=sweb.eval.example__project-42_interface.' in self.path:
            self.send(201, {'Id': 'a' * 64, 'Warnings': []})
        elif self.path.endswith('/exec'):
            exec_id = str(len(self.server.commands))
            self.server.commands[exec_id] = data['Cmd']
            self.send(201, {'Id': exec_id})
        elif '/exec/' in self.path and self.path.endswith('/start'):
            command = self.server.commands[self.path.split('/')[-2]]
            output = b''
            if command == ['/bin/bash', '/eval.sh']:
                if self.server.timeout:
                    self.server.killed.wait(8)
                fix = 'PASSED' if self.server.resolved else 'FAILED'
                output = ('>>>>> Start Test Output\n' + fix + ' tests/test_example.py::test_fix\n'
                          'PASSED tests/test_example.py::test_regression\n>>>>> End Test Output\n').encode()
            self.send(200, struct.pack('>BxxxL', 1, len(output)) + output, raw=True)
        elif '/containers/' in self.path and '/kill' in self.path:
            self.server.killed.set()
            self.send(204, b'', raw=True)
        elif '/containers/' in self.path and '/stop' in self.path and self.server.stop_failure:
            self.send(500, {'message': 'Container stop unavailable; use container kill'})
        elif '/containers/' in self.path and ('/start' in self.path or '/stop' in self.path):
            self.send(204, b'', raw=True)
        else:
            self.send(400, {'message': 'Unexpected request; pull/build is never allowed'})

    def do_PUT(self):
        self.server.requests.append(('PUT', self.path))
        data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                self.server.files[member.name] = archive.extractfile(member).read()
        self.send(200, b'', raw=True)

    def do_DELETE(self):
        self.server.requests.append(('DELETE', self.path))
        self.send(204, b'', raw=True)


@pytest.mark.parametrize(('image_present', 'resolved', 'timeout', 'stop_failure'), [
    (True, True, False, False), (True, False, False, False), (False, False, False, False),
    (True, False, True, False), (True, True, False, True),
])
def test_official_runner_protocol_and_missing_image(tmp_path, image_present, resolved, timeout, stop_failure):
    row = example()
    dataset = tmp_path / 'dataset.json'
    predictions = tmp_path / 'predictions.jsonl'
    dataset.write_text(json.dumps([row]), encoding='utf-8')
    predictions.write_text(json.dumps({'instance_id': row['instance_id'], 'model_name_or_path': 'offline-model',
                                      'model_patch': row['test_patch'].replace('\n', '\r\n')}) + '\n', encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0), DockerProtocol)
    server.requests, server.commands, server.files = [], {}, {}
    server.image_present, server.resolved = image_present, resolved
    server.timeout, server.stop_failure, server.killed = timeout, stop_failure, threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command, env = evaluation_command(dataset, predictions, row['instance_id'], 'protocol', tmp_path, 1 if timeout else 10)
    env = os.environ | env | {'DOCKER_HOST': f'tcp://127.0.0.1:{server.server_port}'}
    for key in ('DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    try:
        result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, encoding='utf-8',
                                timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not any('/images/create' in path or '/build' in path for _, path in server.requests)
    if not image_present:
        assert result.returncode != 0 and not any(method != 'GET' for method, _ in server.requests)
        assert not (tmp_path / 'offline-model.protocol.json').exists()
        return
    assert b'\r\n' not in server.files['patch.diff'] and b'\r\n' not in server.files['eval.sh']
    assert b'conda activate testbed' in server.files['eval.sh']
    assert any(method == 'DELETE' and '/containers/' in path for method, path in server.requests)
    summary = json.loads((tmp_path / 'offline-model.protocol.json').read_text())
    if timeout or stop_failure:
        assert server.killed.is_set()
    if timeout:
        assert result.returncode != 0 and summary['error_instances'] == 1
        assert summary['completed_instances'] == 0 and summary['resolved_instances'] == 0
        return
    assert result.returncode == 0, result.stdout + result.stderr
    detail = json.loads(next(tmp_path.glob('logs/run_evaluation/**/report.json')).read_text())[row['instance_id']]
    assert summary['completed_instances'] == 1 and summary['resolved_instances'] == int(resolved)
    assert detail['resolved'] is resolved
    assert len(detail['tests_status']['FAIL_TO_PASS']['success']) == int(resolved)
    assert len(detail['tests_status']['PASS_TO_PASS']['success']) == 1


@pytest.mark.skipif(shutil.which('docker') is None, reason='Worker launches the real Docker CLI against a local protocol service')
@pytest.mark.parametrize(('resolved',), [(True,), (False,)])
def test_real_worker_launches_pinned_grader_with_local_docker_protocol(tmp_path, resolved):
    """Cross both process boundaries; a direct runner test cannot catch worker startup errors."""
    from local_setup.webui.benchmarks import REBENCH_ID, case_key, snapshot

    root = Path(__file__).resolve().parents[3]
    state = tmp_path / 'state'
    config = tmp_path / 'config'
    (config / '.local').mkdir(parents=True)
    (config / '.local/.env').write_text("MSWEA_MODEL_NAME='openai/local-worker-fixture'\n", encoding='utf-8')
    folder = state / 'runs' / '20261008-000000-012abcde'
    folder.mkdir(parents=True)
    row = example() | {'dataset': REBENCH_ID, 'benchmark_snapshot': snapshot(REBENCH_ID),
                       'case_key': case_key(REBENCH_ID, example()['instance_id'])}
    dataset = state / 'datasets' / REBENCH_ID / (row['instance_id'] + '.json')
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps([row]), encoding='utf-8')
    prediction = tmp_path / 'input.predictions.jsonl'
    prediction.write_text(json.dumps({'instance_id': row['instance_id'], 'model_name_or_path': 'offline-model',
                                      'model_patch': row['test_patch']}) + '\n', encoding='utf-8')
    (folder / 'request.json').write_text(json.dumps({'kind': 'evaluate', 'dataset': REBENCH_ID,
        'instance_id': row['instance_id'], 'benchmark_snapshot': snapshot(REBENCH_ID),
        'prediction_path': str(prediction), 'allow_data_download': False, 'allow_image_download': False}), encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0), DockerProtocol)
    server.requests, server.commands, server.files = [], {}, {}
    server.image_present, server.resolved = True, resolved
    server.timeout, server.stop_failure, server.killed = False, False, threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    docker_config = tmp_path / 'docker-config'
    docker_config.mkdir()
    endpoint = f'tcp://127.0.0.1:{server.server_port}'
    env = os.environ | {'MINI_WEB_STATE': str(state), 'MINI_WEB_CONFIG_ROOT': str(config),
                        'MSWEA_GLOBAL_CONFIG_DIR': str(config / '.local'), 'MSWEA_CONFIGURED': 'true',
                        'DOCKER_CONFIG': str(docker_config), 'DOCKER_CONTEXT': 'worker-protocol', 'PYTHONUTF8': '1'}
    for key in ('DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        env.pop(key, None)
    try:
        context = subprocess.run(['docker', '--config', str(docker_config), 'context', 'create', 'worker-protocol',
                                  '--docker', 'host=' + endpoint], env=env, capture_output=True, text=True,
                                 encoding='utf-8', timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert context.returncode == 0, context.stderr
        process = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'local_setup.webui.worker', folder.name],
                                 cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=45,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert process.returncode == 0, process.stdout + process.stderr
    events = [json.loads(line.removeprefix('@@MINI_WEB@@')) for line in process.stdout.splitlines()
              if line.startswith('@@MINI_WEB@@')]
    assert {'kind': 'state', 'data': 'completed'} in events
    evaluation = next(item['data'] for item in events if item['kind'] == 'evaluation')
    assert evaluation['completed'] == 1 and evaluation['resolved'] == int(resolved) and evaluation['errors'] == 0, server.commands
    assert evaluation['detail'][row['instance_id']]['resolved'] is resolved
    assert any('/containers/create' in path for _, path in server.requests)
    assert any(method == 'DELETE' and '/containers/' in path for method, path in server.requests)
    assert not any('/images/create' in path or '/build' in path for _, path in server.requests)
    assert b'\r\n' not in server.files['patch.diff'] and b'\r\n' not in server.files['eval.sh']
    assert not any(item['kind'] in {'approval', 'billing', 'error'} for item in events)
