"""Exercise benchmark selection in a test-owned service without downloads or model calls."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main():
    state = ROOT / '.local/webui/rebench-browser-tests' / uuid4().hex
    config = state / 'config-root'
    (config / '.local').mkdir(parents=True)
    (config / 'local_setup').mkdir()
    (config / '.local/.env').write_text("MSWEA_MODEL_NAME='openai/offline-browser'\nOPENAI_API_BASE='https://example.invalid/v1'\n", encoding='utf-8')
    (config / 'local_setup/windows.yaml').write_bytes((ROOT / 'local_setup/webui/defaults.yaml').read_bytes())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    os.environ.update(MINI_WEB_STATE=str(state), MINI_WEB_CONFIG_ROOT=str(config), MINI_WEB_TESTING='1', MINI_WEB_PORT=str(port),
                      MSWEA_GLOBAL_CONFIG_DIR=str(config / '.local'), MSWEA_CONFIGURED='true')
    from local_setup.webui.benchmarks import REBENCH_ID, REGISTRY, case_key, snapshot
    from local_setup.webui.common import save_json
    from local_setup.webui.profiles import save_connection, save_connection_key, save_profile
    for dataset in ('verified', REBENCH_ID):
        rows = []
        for index in range(10):
            instance = f'audit__fixture-{1000 + index}'
            rows.append({'instance_id':instance, 'repo':'audit/fixture', 'title':f'浏览器隔离测试题目 {index}',
                         'problem_statement':f'隔离验收 {dataset} 题目 {index}；不参与正式统计。', 'description_truncated':False,
                         'difficulty':'', 'created_at':'', 'image':'test-owned-fixture:unused', 'row_index':index,
                         'dataset':dataset, 'benchmark_snapshot':snapshot(dataset), 'case_key':case_key(dataset, instance)})
        save_json(state / 'catalog' / (dataset + '.json'), {'dataset':dataset, 'source':REGISTRY[dataset]['source'],
                  'benchmark_snapshot':snapshot(dataset), 'items':rows, 'synced_at':'2026-10-08T00:00:00+00:00', 'note':'仅隔离浏览器验收，未下载实际题目。'})
        instance = rows[0]['instance_id']
        run_id = '20261008-000000-' + ('00000001' if dataset == 'verified' else '00000002')
        save_json(state / 'runs' / run_id / 'meta.json', {'id':run_id, 'kind':'evaluate', 'title':'隔离结果样本', 'status':'completed',
                  'created':'2026-10-08T00:00:00+00:00', 'finished':'2026-10-08T00:00:01+00:00', 'updated':'2026-10-08T00:00:01+00:00',
                  'model':'openai/offline-ui', 'dataset':dataset, 'instance_id':instance, 'benchmark_snapshot':snapshot(dataset),
                  'case_key':case_key(dataset, instance), 'evaluation':{'resolved':1, 'completed':1, 'errors':0, 'category':'passed',
                  'detail':{instance:{'tests_status':{'FAIL_TO_PASS':{'success':['fixture-repair'], 'failure':[]},
                                                    'PASS_TO_PASS':{'success':['fixture-regression'], 'failure':[]}}}}}})
    connection = save_connection({'name':'浏览器隔离连接', 'api_base':'https://example.invalid/v1'})
    save_connection_key(connection['id'], 'isolated-dummy-not-a-real-key', connection['version'])
    profile = save_profile({'name':'浏览器隔离模型', 'connection_id':connection['id'], 'model_name':'fixture-ui', 'pricing':{'mode':'free'}})
    env = os.environ.copy()
    env['MINI_REBENCH_TEST_INSTANCE'] = str(state / 'instance.json')
    with (state / 'service.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'local_setup/webui/launch.py'), '--serve'],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    save_json(state / 'instance.json', {'state':str(state), 'port':port, 'pid':process.pid, 'dataset':REBENCH_ID, 'profile_id':profile['id']})
    try:
        for _ in range(150):
            if (state / 'service.json').exists():
                break
            if process.poll() is not None:
                raise RuntimeError('Isolated service exited; inspect its private service.log.')
            time.sleep(.1)
        else:
            raise RuntimeError('Isolated service did not start.')
        print(json.dumps({'state':str(state), 'port':port, 'pid':process.pid}), flush=True)
        subprocess.run(['node', str(Path(__file__).with_suffix('.cjs'))], cwd=ROOT, env=env, check=True)
    finally:
        process.terminate()
        process.wait(timeout=20)
        print('Stopped this test-owned service.', flush=True)


if __name__ == '__main__':
    main()
