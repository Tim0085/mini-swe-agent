"""Run the pinned official SWE-rebench grader in a separate Python process."""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REVISION = 'e4907b7a90eafaa1f0a6428fd04fe31cdd8b4284'
SOURCE = Path(__file__).parent / 'vendor' / 'rebench'


def source_manifest() -> dict:
    manifest = json.loads((SOURCE / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('revision') != REVISION:
        raise ValueError('SWE-rebench 评测源码版本不匹配，请修复工作台源码。')
    for name, info in manifest['files'].items():
        path = SOURCE / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
            raise ValueError('SWE-rebench 评测源码校验失败：' + name)
    return manifest


def official_image(row: dict) -> str:
    image = row.get('docker_image') or row.get('image_name')
    if not isinstance(image, str) or not re.fullmatch(r'(?:docker.io/)?swerebench/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?', image):
        raise ValueError('SWE-rebench 题目必须使用 swerebench 官方预建镜像。')
    if row.get('image_name') and row['image_name'] != image:
        raise ValueError('题目的 image_name 与 docker_image 不一致，不能确定官方运行环境。')
    return image


def check_harness(row: dict) -> None:
    manifest = source_manifest()
    for key in ('instance_id', 'repo', 'base_commit', 'test_patch', 'problem_statement'):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError('SWE-rebench 题目缺少有效字段：' + key)
    if not re.fullmatch(r'[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+-\d+(?:_interface)?', row['instance_id']):
        raise ValueError('SWE-rebench 实例编号无效。')
    if not re.fullmatch(r'[a-fA-F0-9]{40}', row['base_commit']):
        raise ValueError('SWE-rebench base_commit 必须是完整提交编号。')
    config = row.get('install_config')
    if not isinstance(config, dict) or config.get('log_parser') not in manifest['log_parsers']:
        raise ValueError('题目未提供固定官方评测器支持的 install_config/log_parser。')
    if not isinstance(config.get('test_cmd'), (str, list)) or not config['test_cmd']:
        raise ValueError('SWE-rebench 题目缺少官方测试命令。')
    for field in ('FAIL_TO_PASS', 'PASS_TO_PASS'):
        tests = json.loads(row[field]) if isinstance(row.get(field), str) else row.get(field)
        if not isinstance(tests, list) or any(not isinstance(test, str) for test in tests):
            raise ValueError('SWE-rebench 测试清单格式无效：' + field)
        if field == 'FAIL_TO_PASS' and not tests:
            raise ValueError('SWE-rebench 缺少需修复测试，不能进行有效评测。')
    official_image(row)


def evaluation_command(dataset_path: Path, predictions_path: Path, instance_id: str, run_id: str,
                       report_dir: Path, timeout: int) -> tuple[list[str], dict[str, str]]:
    source_manifest()
    command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
               '--dataset', str(Path(dataset_path).resolve()), '--predictions', str(Path(predictions_path).resolve()),
               '--instance', instance_id, '--run-id', run_id, '--report-dir', str(Path(report_dir).resolve()),
               '--timeout', str(timeout)]
    return command, {'PYTHONUTF8': '1', 'PYTHONPATH': str(SOURCE.resolve()), 'HF_HUB_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1'}


def load_official() -> None:
    source_manifest()
    if any(name == 'swebench' or name.startswith('swebench.') for name in sys.modules):
        raise RuntimeError('SWE-rebench 官方源码仅允许在独立子进程加载。')
    sys.path.insert(0, str(SOURCE.resolve()))
    import swebench
    if Path(swebench.__file__).resolve() != (SOURCE / 'swebench/__init__.py').resolve():
        raise RuntimeError('加载了错误的 SWE-bench 评测模块。')


def make_spec(row: dict):
    from swebench.harness.constants import MAP_REPO_TO_EXT
    from swebench.harness.test_spec.create_scripts import make_eval_script_list
    from swebench.harness.test_spec.test_spec import TestSpec, _make_remote_eval_compatible
    check_harness(row)
    config = row['install_config']
    commands = make_eval_script_list(row, config, 'testbed', '/testbed', row['base_commit'], row['test_patch'])
    commands = _make_remote_eval_compatible(commands, config.get('install') if row.get('image_name') is not None else None)
    return TestSpec(
        instance_id=row['instance_id'], repo=row['repo'], version=row.get('version'),
        repo_script_list=[], env_script_list=[], eval_script_list=commands, arch='x86_64',
        FAIL_TO_PASS=json.loads(row['FAIL_TO_PASS']) if isinstance(row['FAIL_TO_PASS'], str) else row['FAIL_TO_PASS'],
        PASS_TO_PASS=json.loads(row['PASS_TO_PASS']) if isinstance(row['PASS_TO_PASS'], str) else row['PASS_TO_PASS'],
        language=MAP_REPO_TO_EXT[row['repo']], docker_specs=config.get('docker_specs') or {},
        namespace='swerebench', install_config=config, image_name=official_image(row),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Pinned official SWE-rebench evaluation; uses cached images only.')
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--predictions', required=True, type=Path)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--report-dir', required=True, type=Path)
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.run_id) or not 1 <= args.timeout <= 86400:
        parser.error('Invalid run ID or timeout.')
    records = json.loads(args.dataset.read_text(encoding='utf-8-sig'))
    matching = [row for row in records if row.get('instance_id') == args.instance]
    if len(matching) != 1:
        raise ValueError('评测数据必须包含唯一匹配的 SWE-rebench 题目。')
    row = matching[0]
    check_harness(row)
    predictions = [json.loads(line) for line in args.predictions.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if len(predictions) != 1 or predictions[0].get('instance_id') != args.instance:
        raise ValueError('SWE-rebench 评测仅接受本题的一份补丁。')
    prediction = predictions[0]
    if not isinstance(prediction.get('model_patch'), str) or not prediction['model_patch'].strip():
        raise ValueError('不能评测空补丁。')
    model = prediction.get('model_name_or_path', '')
    if not re.fullmatch(r'[A-Za-z0-9_.@+-]+', model) or '..' in model:
        raise ValueError('评测模型标识无效。')
    prediction['model_patch'] = prediction['model_patch'].replace('\r\n', '\n')
    args.report_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.report_dir)
    load_official()
    import docker
    from swebench.harness.eval import run_instance
    from swebench.harness.reporting import make_run_report
    spec = make_spec(row)
    print('SWE-rebench official grader ' + REVISION, flush=True)
    client = docker.from_env(timeout=600)
    try:
        client.images.get(spec.instance_image_key)
        try:
            run_instance(spec, prediction, False, False, client, args.run_id, args.timeout)
        finally:
            make_run_report({args.instance: prediction}, [row], args.run_id)
    finally:
        client.close()


if __name__ == '__main__':
    main()
