"""Real cached-container tests for bounded Pro command groups and base-relative capture."""
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from local_setup.webui.bench import docker
from local_setup.webui.pro_runtime import capture_patch, prepare_solver, start_solver, stop_action, wrap_action


def main():
    image = docker('image', 'inspect', 'swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest', '--format', '{{.Id}}')
    image.check_returncode()
    container = start_solver(image.stdout.strip(), 'mini-web-pro-runtimecheck-' + uuid4().hex)
    try:
        baseline = docker('exec', container, 'git', '-C', '/testbed', 'rev-parse', 'HEAD')
        baseline.check_returncode()
        base = baseline.stdout.strip()
        prepared = prepare_solver(container, {'base_commit': base})
        def execute(command, seconds=10, host_timeout=30):
            return docker('exec', '-w', '/testbed', container, '/bin/bash', '-lc', wrap_action(command, seconds), timeout=host_timeout)
        assert execute('printf "committed\\n" > pro_committed.txt && git add pro_committed.txt && '
                       'git -c user.name=ProFixture -c user.email=fixture@example.invalid -c core.hooksPath=/dev/null commit -m fixture').returncode == 0
        assert execute('printf "untracked\\n" > pro_untracked.txt').returncode == 0
        captured = capture_patch(container, '/testbed', base)
        assert '+committed' in captured and '+untracked' in captured
        timed = execute('printf "early\\n" > pro_timed.txt; (sleep 3; printf "late\\n" >> pro_timed.txt) & sleep 30', 1)
        assert timed.returncode in {124, 137}, timed
        patch_after_stop = capture_patch(container, '/testbed', base)
        time.sleep(3.2)
        assert capture_patch(container, '/testbed', base) == patch_after_stop
        assert '+early' in patch_after_stop and '+late' not in patch_after_stop
        assert execute('(sleep 2; printf "escaped\\n" > pro_background.txt) & true').returncode == 0
        time.sleep(2.2)
        assert docker('exec', container, '/bin/bash', '-lc', 'test ! -e /testbed/pro_background.txt').returncode == 0
        try:
            execute('printf "host-early\\n" > pro_host.txt; sleep 20; printf "host-late\\n" >> pro_host.txt', 30, 1)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError('The deliberately shorter host timeout did not fire')
        stop_action(container)
        assert docker('exec', container, '/bin/bash', '-lc', 'test ! -e /tmp/mini-pro-action.pid').returncode == 0
        host_patch = capture_patch(container, '/testbed', base)
        time.sleep(.5)
        assert capture_patch(container, '/testbed', base) == host_patch
        assert '+host-early' in host_patch and '+host-late' not in host_patch
    finally:
        docker('rm', '-f', container, timeout=60).check_returncode()
    assert docker('container', 'inspect', container).returncode != 0
    result = {'kind': 'synthetic-cached-container-check-not-benchmark-score', 'runtime': prepared,
              'committed_and_untracked_patch_captured': True, 'single_command_timeout_stopped_group': True,
              'normal_background_children_stopped': True, 'host_timeout_recovery_stopped_group': True,
              'patch_stable_after_stop': True, 'container_removed': True, 'model_calls': 0, 'image_downloads': 0}
    path = ROOT / 'collaboration/artifacts/2026-10-10/swebench-pro/runtime-process-check.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
