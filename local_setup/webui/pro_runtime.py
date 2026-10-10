"""Offline solver containers and source-derived patches for SWE-bench Pro V2."""
import re
import shlex
import math

from .bench import docker

REPOSITORY_PROBE = ('for p in /app /testbed; do '
                    'if [ "$(git -C "$p" rev-parse --show-toplevel 2>/dev/null)" = "$p" ]; then '
                    'printf "%s\\n" "$p"; git -C "$p" rev-parse HEAD; exit; fi; done; '
                    'echo "No task Git repository found" >&2; exit 1')
ACTION_PID = '/tmp/mini-pro-action.pid'
STOP_ACTION = '''import os, signal, time
from pathlib import Path
p = Path('/tmp/mini-pro-action.pid')
if not p.exists():
    raise SystemExit(0)
text = p.read_text().strip()
if not text.isdecimal() or int(text) <= 1:
    raise SystemExit('Invalid Pro action process group')
group = int(text)
def live():
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] not in {'Z', 'X'}:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False
for sig in (signal.SIGTERM, signal.SIGKILL):
    if not live():
        break
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass
    for _ in range(10):
        if not live():
            break
        time.sleep(.1)
if live():
    raise SystemExit('Pro action process group is still running')
try:
    p.unlink()
except FileNotFoundError:
    pass
'''


def wrap_action(command: str, seconds: float) -> str:
    if not isinstance(command, str) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 3000:
        raise ValueError('Pro 命令或执行时间限制无效。')
    child = ('printf "%s\\n" "$$" > ' + ACTION_PID + '; exec timeout -k 2 '
             + str(seconds) + ' /bin/bash -c ' + shlex.quote(command))
    return ('if [ -e ' + ACTION_PID + ' ]; then echo "Unfinished Pro action" >&2; exit 125; fi; '
            'setsid /bin/bash -c ' + shlex.quote(child) + ' & runner=$!; wait "$runner"; status=$?; '
            'python3 -c ' + shlex.quote(STOP_ACTION) + ' || exit 125; exit "$status"')


def stop_action(container_id: str) -> None:
    result = docker('exec', container_id, 'python3', '-c', STOP_ACTION, timeout=15)
    if result.returncode:
        raise ValueError('无法确认 Pro 容器内命令已停止，未捕获或派发补丁；请检查清理结果。')


def start_solver(image: str, container_name: str) -> str:
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('Pro 求解必须使用已核实的本机不可变镜像 ID。')
    if not re.fullmatch(r'mini-web-[A-Za-z0-9_-]+', container_name):
        raise ValueError('Pro 求解容器名称无效。')
    result = docker('run', '--pull', 'never', '-d', '--rm', '--network', 'none', '--name', container_name,
                    '--cpus', '1', '--memory', '4096m', '--entrypoint', '/bin/bash', '-w', '/',
                    image, '-lc', 'sleep 7200', timeout=120)
    result.check_returncode()
    container_id = result.stdout.strip()
    if not re.fullmatch(r'[a-f0-9]{64}', container_id):
        raise ValueError('Docker 未返回有效的 Pro 求解容器 ID。')
    return container_id


def prepare_solver(container_id: str, row: dict) -> dict:
    if not re.fullmatch(r'[a-f0-9]{40}', row.get('base_commit', '')):
        raise ValueError('Pro 题目缺少有效的 base_commit。')
    inspect = docker('inspect', container_id, '--format',
                     '{{.HostConfig.NetworkMode}}|{{.Image}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}')
    inspect.check_returncode()
    network, image, cpus, memory = inspect.stdout.strip().split('|')
    if network != 'none':
        raise ValueError('Pro 求解容器必须断网，本次尚未请求模型。')
    if cpus != '1000000000' or memory != str(4096 * 1024 * 1024):
        raise ValueError('Pro 求解容器资源限制与已确认配置不同，本次尚未请求模型。')
    result = docker('exec', container_id, '/bin/bash', '-lc', REPOSITORY_PROBE)
    result.check_returncode()
    lines = result.stdout.strip().splitlines()
    if len(lines) != 2 or lines[0] not in {'/app', '/testbed'} or lines[1] != row['base_commit']:
        raise ValueError('Pro 镜像 Git 根或 HEAD 与官方题目不符，本次尚未请求模型。')
    return {'cwd': lines[0], 'head': lines[1], 'image_id': image, 'network_mode': network,
            'cpus': 1, 'memory_mb': 4096, 'storage_quota_enforced': False}


def capture_patch(container_id: str, cwd: str, base_commit: str | None = None) -> str:
    if cwd not in {'/app', '/testbed'}:
        raise ValueError('Pro 补丁来源目录无效。')
    if base_commit is not None and not re.fullmatch(r'[a-f0-9]{40}', base_commit):
        raise ValueError('Pro 补丁基线无效。')
    stop_action(container_id)
    command = ('git add -A && git -c core.pager=cat diff --cached --binary --no-ext-diff --no-textconv '
               + (base_commit or 'HEAD') + '; '
               'status=$?; git reset -q >/dev/null 2>&1; exit "$status"')
    result = docker('exec', '-w', cwd, container_id, '/bin/bash', '-lc',
                    'timeout -k 5 60 bash -c ' + shlex.quote(command), timeout=75)
    if result.returncode:
        raise ValueError('无法从 Pro 求解容器捕获真实补丁；保留原退出状态，没有替换为旧补丁。')
    patch = result.stdout.replace('\r\n', '\n')
    if len(patch.encode('utf-8')) > 20_000_000:
        raise ValueError('Pro 工作区补丁超过 20 MB，未导出截断内容。')
    return patch
