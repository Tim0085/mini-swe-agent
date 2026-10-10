"""Fresh-container execution of pinned, unchanged SWE-bench Pro V2 verifiers."""
import argparse
import ast
import hashlib
import io
import json
import re
import shlex
import sys
import tarfile
import time
import tomllib
from pathlib import Path

REVISION = '66f92766bba642462d4bbe5479e83f91f9211862'
TEST_FILES = ('test.sh', 'run_script.sh', 'parser.py', 'config.json', 'test_patch.patch')
LOG_FILES = ('reward.txt', 'output.json', 'run-script-stdout.txt', 'run-script-stderr.txt', 'test_output.txt')


def check_harness(row: dict) -> None:
    from .pro_data import asset_paths
    if not re.fullmatch(r'[a-f0-9]{40}', row.get('base_commit', '')):
        raise ValueError('Pro 题目缺少有效 base_commit。')
    asset_paths(row)


def evaluation_command(dataset_path: Path, predictions_path: Path, instance_id: str, run_id: str,
                       report_dir: Path, timeout: int = 3000,
                       expected_image_id: str | None = None) -> tuple[list[str], dict[str, str]]:
    from .common import STATE
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3000 or int(timeout) != timeout:
        raise ValueError('Pro 评测时间限制无效。')
    command = [sys.executable, '-X', 'utf8', '-m', 'local_setup.webui.pro_harness',
               '--dataset', str(Path(dataset_path).resolve()), '--predictions', str(Path(predictions_path).resolve()),
               '--instance', instance_id, '--run-id', run_id, '--report-dir', str(Path(report_dir).resolve()),
               '--timeout', str(int(timeout))]
    if expected_image_id:
        command.extend(['--image-id', expected_image_id])
    return command, {'PYTHONUTF8': '1', 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
                     'MINI_WEB_STATE': str(STATE), 'HF_HUB_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1'}


def parse_required(value) -> set[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = ast.literal_eval(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError('Pro 官方测试清单格式无效。')
    return set(value)


def normalize_result(config: dict, output: dict, reward: str) -> dict:
    if reward.strip() not in {'0', '1'}:
        raise ValueError('Pro 官方 verifier 没有返回有效 reward，不能判定求解结果。')
    if not isinstance(output, dict) or not isinstance(output.get('tests'), list):
        raise ValueError('Pro 官方 parser 没有生成有效测试明细。')
    tests = output['tests']
    if not tests:
        raise ValueError('Pro 验证器没有收集任何测试，结果需检查，不能直接归因于模型补丁。')
    if any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
           or not isinstance(item.get('status'), str) for item in tests):
        raise ValueError('Pro 官方测试明细格式无效。')
    passed = {item['name'] for item in tests if item['status'] == 'PASSED'}
    groups = {}
    for source, target in [('fail_to_pass', 'FAIL_TO_PASS'), ('pass_to_pass', 'PASS_TO_PASS')]:
        required = parse_required(config.get(source, []))
        groups[target] = {'success': sorted(required & passed), 'failure': sorted(required - passed)}
    if not any(group['success'] or group['failure'] for group in groups.values()):
        raise ValueError('Pro 题目缺少有效测试清单，不能形成有效成绩。')
    return {'resolved': reward.strip() == '1', 'official_reward': int(reward.strip()), 'tests_status': groups}


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(content), 0o644
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def _read_archive(container, path: str, allowed: tuple[str, ...]) -> dict[str, bytes]:
    chunks, _ = container.get_archive(path)
    content = io.BytesIO()
    for chunk in chunks:
        if content.tell() + len(chunk) > 100_000_000:
            raise ValueError('Pro 评测输出超过 100 MB，未读取截断报告。')
        content.write(chunk)
    content.seek(0)
    result = {}
    with tarfile.open(fileobj=content) as archive:
        for entry in archive:
            name = Path(entry.name).name
            if name in allowed and entry.isfile():
                if entry.size > 100_000_000:
                    raise ValueError('Pro 单份评测输出超过 100 MB。')
                if name in result:
                    raise ValueError('Pro 评测输出包含重复文件名。')
                result[name] = archive.extractfile(entry).read()
    return result


def _exec(container, command: str, timeout: int, *, cwd: str = '/', output: str = '/tmp/pro-command.log') -> int:
    wrapped = ('timeout -k 10 ' + str(timeout) + ' /bin/bash -c ' + shlex.quote(command)
               + ' > ' + shlex.quote(output) + ' 2>&1')
    exec_id = container.client.api.exec_create(container.id, ['/bin/bash', '-lc', wrapped], workdir=cwd)['Id']
    container.client.api.exec_start(exec_id, detach=True)
    deadline = time.monotonic() + timeout + 15
    while True:
        state = container.client.api.exec_inspect(exec_id)
        if not state['Running']:
            if state.get('ExitCode') in {124, 137}:
                raise TimeoutError('Pro 容器内执行达到时限；本次未形成有效评测成绩。')
            return int(state.get('ExitCode') if state.get('ExitCode') is not None else -1)
        if time.monotonic() >= deadline:
            raise TimeoutError('Pro 容器内执行状态超时；正在清理本次容器。')
        time.sleep(.2)


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')


def run_evaluation(row: dict, prediction: dict, assets: dict[str, Path], run_id: str,
                   report_dir: Path, timeout: int = 3000, expected_image_id: str | None = None) -> dict:
    """The caller must obtain assets through pro_data.asset_paths before invoking this runner."""
    import docker
    from .pro_runtime import REPOSITORY_PROBE
    instance = row['instance_id']
    model = prediction['model_name_or_path']
    if not re.fullmatch(r'[A-Za-z0-9_.@+-]+', model) or '..' in model:
        raise ValueError('Pro 评测模型标识无效。')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id) or not 1 <= timeout <= 3000:
        raise ValueError('Pro 评测任务编号或时间限制无效。')
    if prediction.get('instance_id') != instance or not isinstance(prediction.get('model_patch'), str):
        raise ValueError('Pro 评测必须提供本题的一份补丁。')
    patch = prediction['model_patch'].replace('\r\n', '\n').encode('utf-8')
    if len(patch) > 20_000_000:
        raise ValueError('Pro 补丁超过 20 MB。')
    folder = report_dir / 'logs/run_evaluation/pro' / hashlib.sha256(instance.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / f'{model}.{run_id}.json'
    detail = {'resolved': False, 'patch_applied': False, 'official_reward': None,
              'patch_sha256': hashlib.sha256(patch).hexdigest(), 'evaluator_revision': REVISION}
    summary = {'total_instances': 1, 'submitted_instances': 1, 'completed_instances': 0,
               'resolved_instances': 0, 'unresolved_instances': 0, 'error_instances': 0,
               'infra_failure_instances': 0, 'empty_patch_instances': int(not patch.strip()),
               'failure_reasons': {}, 'unremoved_containers': []}
    def log(message):
        with (folder / 'run_instance.log').open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(message + '\n')
        print(message, flush=True)
    client, container = None, None
    verifier_started, artifacts_read = False, False
    try:
        if 'task.toml' in assets:
            task = tomllib.loads(assets['task.toml'].read_text(encoding='utf-8'))
            if task['environment'].get('cpus') != 1 or task['environment'].get('memory_mb') != 4096:
                raise ValueError('Pro 官方资源要求变化，本版本仅支持 1 CPU / 4096 MiB。')
        config = json.loads(assets['tests/config.json'].read_text(encoding='utf-8'))
        for field in ('fail_to_pass', 'pass_to_pass'):
            parse_required(config.get(field, []))
        (folder / 'config.json').write_bytes(assets['tests/config.json'].read_bytes())
        client = docker.from_env(timeout=60)
        if client.info().get('OSType') != 'linux':
            raise ValueError('Pro 评测需要 Docker Linux 引擎。')
        image = client.images.get(row['docker_image'])
        if expected_image_id and image.id != expected_image_id:
            raise ValueError('Pro 镜像在准备后发生变化，未开始评测。')
        detail['image_id'] = image.id
        log('Creating container: Pro 独立评测环境')
        container = client.containers.create(image.id, command=['sleep 7200'], entrypoint=['/bin/bash', '-lc'],
            name='sweb.eval.' + instance.lower() + '.' + run_id, working_dir='/', network_mode='bridge',
            nano_cpus=1_000_000_000, mem_limit='4096m',
            labels={'mini-workbench': 'pro-evaluation', 'mini-run-id': run_id})
        container.start()
        if _exec(container, REPOSITORY_PROBE, 30, output='/tmp/pro-repository.txt'):
            raise ValueError('Pro 官方镜像缺少有效 Git 工作区。')
        probe = _read_archive(container, '/tmp/pro-repository.txt', ('pro-repository.txt',))
        lines = probe.get('pro-repository.txt', b'').decode('utf-8').strip().splitlines()
        if len(lines) != 2 or lines[0] not in {'/app', '/testbed'} or lines[1] != row.get('base_commit'):
            raise ValueError('Pro 评测镜像 HEAD 与题目基线不符。')
        detail['working_dir'], detail['base_commit'] = lines
        if _exec(container, 'mkdir -p /logs/verifier /tests', 30):
            raise ValueError('无法准备 Pro 验证器目录。')
        if patch.strip():
            log('now applying: 只重放本次模型补丁')
            if not container.put_archive('/tmp', _tar({'replay.patch': patch})):
                raise ValueError('Pro 补丁传输失败。')
            apply_code = _exec(container,
                'git apply --verbose /tmp/replay.patch || git apply --3way /tmp/replay.patch || patch --fuzz=3 -p1 -i /tmp/replay.patch',
                300, cwd=lines[0], output='/tmp/pro-apply.txt')
            detail['apply_returncode'] = apply_code
            for name, data in _read_archive(container, '/tmp/pro-apply.txt', ('pro-apply.txt',)).items():
                (folder / name).write_bytes(data)
            if apply_code:
                raise ValueError('Pro 补丁无法应用到官方基线，未运行测试。')
        detail['patch_applied'] = True
        log('copying to container: 传入固定官方验证器，求解容器不接收这些文件')
        payload = {name: assets['tests/' + name].read_bytes() for name in TEST_FILES}
        if not container.put_archive('/tests', _tar(payload)):
            raise ValueError('Pro 官方验证器传输失败。')
        log('Eval script: 执行官方 test.sh，单题测试时限 ' + str(timeout) + ' 秒')
        verifier_started = True
        code = _exec(container, 'bash /tests/test.sh', timeout, output='/logs/verifier/test_output.txt')
        detail['verifier_returncode'] = code
        artifacts = _read_archive(container, '/logs/verifier', LOG_FILES)
        for name, data in artifacts.items():
            (folder / name).write_bytes(data)
        artifacts_read = True
        if 'reward.txt' not in artifacts or 'output.json' not in artifacts:
            raise ValueError('Pro 官方验证器缺少 reward 或测试明细，不能判为未解决。')
        log('Grading answer: 读取官方 reward 与测试明细')
        detail.update(normalize_result(config, json.loads(artifacts['output.json']), artifacts['reward.txt'].decode('utf-8')))
        summary.update(completed_instances=1, resolved_instances=int(detail['resolved']),
                       unresolved_instances=int(not detail['resolved']))
        log('Result for ' + instance + ': ' + ('已解决' if detail['resolved'] else '未解决'))
    except Exception as exc:
        reason = type(exc).__name__ + ': ' + str(exc)
        detail['error'] = reason
        summary.update(error_instances=1, infra_failure_instances=1, failure_reasons={instance: reason})
        log('Pro 评测异常：' + reason)
    finally:
        if container is not None:
            if verifier_started and not artifacts_read:
                try:
                    for name, data in _read_archive(container, '/logs/verifier', LOG_FILES).items():
                        (folder / name).write_bytes(data)
                except Exception as exc:
                    log('Pro 中断后的日志未能完整取得：' + str(exc))
            try:
                container.remove(force=True)
            except Exception as exc:
                reason = 'Pro 评测容器清理失败：' + str(exc)
                detail['cleanup_error'] = reason
                summary.update(error_instances=1, unremoved_containers=[container.name])
                summary['failure_reasons'][instance] = reason
                log(reason)
        if client is not None:
            client.close()
        _write(folder / 'report.json', {instance: detail})
        _write(summary_path, summary)
    return summary


def main() -> None:
    from .pro_data import asset_paths
    parser = argparse.ArgumentParser(description='SWE-bench Pro V2: cached images and verified assets only.')
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--predictions', required=True, type=Path)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--report-dir', required=True, type=Path)
    parser.add_argument('--timeout', type=int, default=3000)
    parser.add_argument('--image-id')
    args = parser.parse_args()
    records = json.loads(args.dataset.read_text(encoding='utf-8-sig'))
    matching = [row for row in records if row.get('instance_id') == args.instance]
    predictions = [json.loads(line) for line in args.predictions.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if len(matching) != 1 or len(predictions) != 1:
        raise ValueError('Pro 评测仅接受唯一题目与唯一补丁。')
    check_harness(matching[0])
    summary = run_evaluation(matching[0], predictions[0], asset_paths(matching[0]), args.run_id,
                             args.report_dir, args.timeout, args.image_id)
    raise SystemExit(1 if summary['error_instances'] else 0)


if __name__ == '__main__':
    main()
