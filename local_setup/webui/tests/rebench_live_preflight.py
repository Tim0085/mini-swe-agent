"""Synthetic Docker preflight for the pinned grader; never a benchmark score."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from local_setup.webui.rebench_harness import REVISION, load_official


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id', required=True)
    args = parser.parse_args()
    if not args.image_id.startswith('sha256:') or len(args.image_id) != 71:
        parser.error('Pass an existing immutable SHA256 image ID; tags are not accepted.')
    directory = ROOT / '.local/webui/rebench-live-preflight' / uuid4().hex
    directory.mkdir(parents=True)
    os.chdir(directory)
    load_official()
    import docker
    from swebench.harness.eval import run_instance
    from swebench.harness.test_spec.test_spec import REMOTE_CONDA_ACTIVATE, TestSpec

    client = docker.from_env(timeout=120)
    image = client.images.get(args.image_id)
    images_before = {item.id: sorted(item.tags) for item in client.images.list()}
    containers_before = {item.id for item in client.containers.list(all=True)}
    assert image.id == args.image_id
    label = 'mini-rebench-preflight-' + directory.name
    tests = ('import sys\nsys.path.insert(0, "/testbed")\n'
             'from rebench_preflight_value import VALUE\n'
             'def test_fix():\n    assert VALUE == 1\n'
             'def test_regression():\n    assert isinstance(VALUE, int)\n')
    commands = [REMOTE_CONDA_ACTIVATE, 'conda activate testbed',
                'mkdir -p /tmp/rebench_preflight', 'cd /tmp/rebench_preflight',
                "cat > test_preflight.py <<'FIXTURE_EOF'\n" + tests + 'FIXTURE_EOF',
                'export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1',
                ": '>>>>> Start Test Output'",
                'python -m pytest -c /dev/null --rootdir=. -rA -q test_preflight.py',
                ": '>>>>> End Test Output'"]
    results = []
    try:
        for name, value in [('negative', 0), ('positive', 1)]:
            run_id = label + '-' + name
            spec = TestSpec(instance_id='fixture__rebench-1', repo='fixture/rebench', version='synthetic',
                            repo_script_list=[], env_script_list=[], eval_script_list=commands,
                            arch='x86_64', FAIL_TO_PASS=['test_preflight.py::test_fix'],
                            PASS_TO_PASS=['test_preflight.py::test_regression'], language='py',
                            docker_specs={}, namespace='synthetic-preflight', image_name=image.id,
                            install_config={'log_parser': 'parse_log_pytest', 'test_cmd': 'pytest -rA'})
            patch = ('diff --git a/rebench_preflight_value.py b/rebench_preflight_value.py\n'
                     'new file mode 100644\n--- /dev/null\n+++ b/rebench_preflight_value.py\n'
                     '@@ -0,0 +1 @@\n+VALUE = ' + str(value) + '\n')
            prediction = {'instance_id': spec.instance_id, 'model_name_or_path': 'synthetic-no-model',
                          'model_patch': patch}
            _, report = run_instance(spec, prediction, False, False, client, run_id, 60,
                                     container_kwargs={'network_mode': 'none', 'labels': {'mini.preflight': label}})
            detail = report[spec.instance_id]
            logdir = directory / 'logs/run_evaluation' / run_id / 'synthetic-no-model' / spec.instance_id
            assert detail['resolved'] is bool(value), detail
            assert len(detail['tests_status']['FAIL_TO_PASS']['success']) == value
            assert len(detail['tests_status']['PASS_TO_PASS']['success']) == 1
            files = {}
            for filename in ['patch.diff', 'eval.sh']:
                content = (logdir / filename).read_bytes()
                assert b'\r' not in content, filename
                files[filename] = {'lf_only': True, 'sha256': hashlib.sha256(content).hexdigest()}
            results.append({'fixture': name, 'resolved': detail['resolved'],
                            'tests_status': detail['tests_status'], 'files': files,
                            'report': str(logdir / 'report.json')})
    finally:
        for container in client.containers.list(all=True, filters={'label': 'mini.preflight=' + label}):
            assert container.id not in containers_before
            container.remove(force=True)
        remaining = client.containers.list(all=True, filters={'label': 'mini.preflight=' + label})
        images_after = {item.id: sorted(item.tags) for item in client.images.list()}
        assert all(images_after.get(key) == tags for key, tags in images_before.items())
        assert containers_before <= {item.id for item in client.containers.list(all=True)}
        assert not remaining
        client.close()
    summary = {'kind': 'synthetic-docker-preflight-not-benchmark-score', 'official_grader_revision': REVISION,
               'image_id': image.id, 'network': 'none', 'cases': results, 'containers_removed': True,
               'existing_images_and_tags_preserved': True, 'existing_containers_preserved': True,
               'downloaded_images': 0, 'model_calls': 0, 'private_artifacts': str(directory)}
    target = ROOT / 'collaboration/artifacts/2026-10-08/rebench/live-synthetic-preflight.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
