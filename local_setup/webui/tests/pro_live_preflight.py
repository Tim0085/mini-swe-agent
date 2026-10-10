"""Run the unchanged Pro verifier with synthetic data on one already cached image."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from local_setup.webui.bench import docker as docker_command
from local_setup.webui.pro_harness import REVISION, run_evaluation
from local_setup.webui.pro_runtime import capture_patch, prepare_solver, start_solver


def main():
    import docker
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    args = parser.parse_args()
    directory = ROOT / '.local/webui/pro-live-preflight' / uuid4().hex
    directory.mkdir(parents=True)
    client = docker.from_env(timeout=120)
    image = client.images.get(args.image_id)
    assert image.id == args.image_id
    images_before = {item.id: sorted(item.tags) for item in client.images.list()}
    containers_before = {item.id for item in client.containers.list(all=True)}
    source = ROOT / '.local/webui/pro-20261010/upstream-eval/v2/tasks'
    candidates = list(source.glob('*/tests/test.sh'))
    official = next(path for path in candidates if hashlib.sha256(path.read_bytes()).hexdigest() ==
                    'b6f26eecd87c1696a3702ee115d31f9d42f4704cf2116419cd879f66b60976d0')
    assets = {'tests/test.sh': official}
    tests = ('import json\nfrom pro_preflight_value import VALUE\n'
             'print(json.dumps({"tests": [{"name": "repair", "status": "PASSED" if VALUE == 1 else "FAILED"}, '
             '{"name": "regression", "status": "PASSED" if isinstance(VALUE, int) else "FAILED"}]}))\n')
    test_patch = ('diff --git a/test_pro_preflight.py b/test_pro_preflight.py\nnew file mode 100644\n'
                  '--- /dev/null\n+++ b/test_pro_preflight.py\n@@ -0,0 +1,' + str(len(tests.splitlines())) + ' @@\n'
                  + ''.join('+' + line + '\n' for line in tests.splitlines()))
    fixture = {'run_script.sh': '#!/bin/bash\npython3 test_pro_preflight.py\n',
               'parser.py': 'import sys\nfrom pathlib import Path\nPath(sys.argv[3]).write_bytes(Path(sys.argv[1]).read_bytes())\n',
               'config.json': json.dumps({'fail_to_pass': "['repair']", 'pass_to_pass': '["regression"]'}),
               'test_patch.patch': test_patch}
    for name, content in fixture.items():
        path = directory / 'assets' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')
        assets['tests/' + name] = path
    solver = start_solver(image.id, 'mini-web-pro-preflight-' + directory.name)
    try:
        baseline = docker_command('exec', solver, 'git', '-C', '/testbed', 'rev-parse', 'HEAD')
        baseline.check_returncode()
        base_commit = baseline.stdout.strip()
        prepared = prepare_solver(solver, {'base_commit': base_commit})
        assert prepared['cwd'] == '/testbed' and prepared['network_mode'] == 'none'
        change = docker_command('exec', '-w', '/testbed', solver, '/bin/bash', '-lc', 'printf "VALUE = 1\\n" > pro_preflight_value.py')
        change.check_returncode()
        captured = capture_patch(solver, '/testbed')
        assert 'new file mode' in captured and '+VALUE = 1' in captured
        assert docker_command('exec', '-w', '/testbed', solver, 'git', 'diff', '--cached', '--name-only').stdout.strip() == ''
        assert docker_command('exec', solver, '/bin/bash', '-lc', 'test ! -e /tests/test.sh').returncode == 0
    finally:
        docker_command('rm', '-f', solver, timeout=60).check_returncode()
    results = []
    for label, value in [('negative', 0), ('positive', 1)]:
        folder = directory / label
        row = {'instance_id': 'fixture__pro-1', 'docker_image': image.id, 'base_commit': base_commit}
        patch = captured.replace('+VALUE = 1', '+VALUE = ' + str(value))
        prediction = {'instance_id': row['instance_id'], 'model_name_or_path': 'synthetic-no-model', 'model_patch': patch}
        summary = run_evaluation(row, prediction, assets, 'pro-preflight-' + directory.name + '-' + label,
                                  folder, 60, image.id)
        detail = json.loads(next(folder.glob('logs/run_evaluation/pro/*/report.json')).read_text())[row['instance_id']]
        assert summary['error_instances'] == 0, detail
        assert summary['completed_instances'] == 1 and summary['resolved_instances'] == value, detail
        assert detail['resolved'] == bool(value)
        assert len(detail['tests_status']['FAIL_TO_PASS']['success']) == value
        assert len(detail['tests_status']['PASS_TO_PASS']['success']) == 1
        results.append({'fixture': label, 'resolved': detail['resolved'], 'tests_status': detail['tests_status'],
                         'report': str(next(folder.glob('logs/run_evaluation/pro/*/report.json')))})
    images_after = {item.id: sorted(item.tags) for item in client.images.list()}
    containers_after = {item.id for item in client.containers.list(all=True)}
    assert images_before == images_after and containers_before == containers_after
    client.close()
    summary = {'kind': 'synthetic-official-verifier-preflight-not-benchmark-score', 'official_revision': REVISION,
               'image_id': image.id, 'official_test_sh_sha256': hashlib.sha256(official.read_bytes()).hexdigest(),
               'solver_network': prepared['network_mode'], 'fresh_verifier': True, 'captured_untracked_patch': True,
               'cases': results, 'original_images_and_containers_preserved': True, 'containers_removed': True,
               'model_calls': 0, 'downloaded_images': 0}
    artifact = ROOT / 'collaboration/artifacts/2026-10-10/swebench-pro/live-synthetic-preflight.json'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
