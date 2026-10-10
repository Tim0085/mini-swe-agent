"""Pinned public Pro V2 data and separately cached official verifier assets."""
import hashlib
import io
import json
import os
import re
import time
import tomllib
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import httpx

from .common import STATE, atomic_bytes, read_json, save_json

PRO_ID = 'pro-v2-2d52cb3d'
HF_REVISION = '2d52cb3df914a3fcf80c7f66738b3a88ae37fc50'
GH_REVISION = '66f92766bba642462d4bbe5479e83f91f9211862'
SOURCE_URL = f'https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/resolve/{HF_REVISION}/data/default/test-00000-of-00001.parquet'
SOURCE_BYTES = 6511689
SOURCE_SHA256 = '1067f72c1f43bbc578276888625db3b836cebb535546c9eaf824c5d5dd104a7f'
ASSET_BASE_URL = f'https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/{GH_REVISION}/v2/tasks'
PUBLIC_COLUMNS = ['instance_id', 'repo', 'base_commit', 'problem_statement', 'requirements', 'interface',
                  'repo_language', 'issue_specificity', 'issue_categories', 'docker_image', 'hard', 'version']
ASSET_FILES = ('instruction.md', 'task.toml', 'tests/test.sh', 'tests/run_script.sh', 'tests/parser.py',
               'tests/config.json', 'tests/test_patch.patch')
MANIFEST_PATH = Path(__file__).parent / 'vendor/pro/asset-manifest.json'
PUBLIC_MANIFEST_PATH = MANIFEST_PATH.with_name('public-manifest.json')
PUBLIC_CACHE = STATE / 'pro-data' / HF_REVISION / 'public.json'
INSTANCE_PATTERN = r'instance_[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9a-f]{40}(?:-v(?:[0-9a-f]{40}|nan))?'
MAX_ASSET_BYTES = 16 * 1024 * 1024


@lru_cache(maxsize=1)
def manifest() -> dict:
    data = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    if data['github_revision'] != GH_REVISION or len(data['files']) != 642:
        raise ValueError('SWE-bench Pro 官方资源清单版本不匹配。')
    return data['files']


@lru_cache(maxsize=1)
def public_manifest() -> dict:
    data = read_json(PUBLIC_MANIFEST_PATH)
    if (data.get('source_revision') != HF_REVISION or data.get('source_sha256') != SOURCE_SHA256
            or set(data.get('rows', {})) != set(manifest())):
        raise ValueError('SWE-bench Pro 公共资料校验清单版本不匹配。')
    return data['rows']


def public_case(row: dict) -> dict:
    if not isinstance(row, dict) or not set(PUBLIC_COLUMNS) <= row.keys():
        raise ValueError('SWE-bench Pro 公共资料字段不完整，请重新缓存题目。')
    public = {key: row[key] for key in PUBLIC_COLUMNS}
    digest = hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    if public_manifest().get(public['instance_id']) != digest:
        raise ValueError('SWE-bench Pro 公共资料与固定来源不一致，请重新缓存题目。')
    return public


def _public_rows(value: dict) -> list[dict]:
    if (not isinstance(value, dict) or set(value) != {'source_revision', 'source_sha256', 'items'}
            or value['source_revision'] != HF_REVISION or value['source_sha256'] != SOURCE_SHA256
            or not isinstance(value['items'], list) or len(value['items']) != 642):
        raise ValueError('SWE-bench Pro 公共资料缓存版本无效，请允许下载后重新缓存。')
    rows = value['items']
    if any(not isinstance(row, dict) or set(row) != set(PUBLIC_COLUMNS) for row in rows):
        raise ValueError('SWE-bench Pro 公共资料缓存包含缺失或非公开字段。')
    rows = [public_case(row) for row in rows]
    if {row['instance_id'] for row in rows} != set(public_manifest()):
        raise ValueError('SWE-bench Pro 公共资料缓存存在重复或缺失题目。')
    return rows


@contextmanager
def _source_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        deadline = time.monotonic() + 90
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('另一项任务正在缓存 Pro 公共资料，请等待完成后重试。') from None
                time.sleep(.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def public_rows(emit=None, *, download=True, source_url=SOURCE_URL, cache_path: Path | None = None) -> list[dict]:
    path = cache_path if cache_path is not None else PUBLIC_CACHE
    try:
        return _public_rows(read_json(path))
    except (OSError, ValueError, TypeError, KeyError):
        if not download:
            raise ValueError('Pro 公共资料缓存缺失或校验失败，请允许下载后重新缓存。') from None
    with _source_lock(path):
        try:
            return _public_rows(read_json(path))
        except (OSError, ValueError, TypeError, KeyError):
            pass
        value = {'source_revision': HF_REVISION, 'source_sha256': SOURCE_SHA256,
                 'items': read_source(download_source(emit, source_url=source_url)).to_pylist()}
        rows = _public_rows(value)
        save_json(path, value)
        return rows


def _instance(row: dict) -> str:
    instance = row.get('instance_id', '')
    if not re.fullmatch(INSTANCE_PATTERN, instance) or instance not in manifest():
        raise ValueError('SWE-bench Pro 固定快照中没有此题号。')
    if row.get('asset_revision', GH_REVISION) != GH_REVISION:
        raise ValueError('SWE-bench Pro 题目资源版本不匹配，请重新缓存。')
    return instance


def _fetch(url: str, limit: int) -> bytes:
    chunks = []
    size = 0
    with httpx.stream('GET', url, follow_redirects=True, timeout=httpx.Timeout(45, connect=10)) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(128 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError('SWE-bench Pro 下载内容超过允许大小，已停止读取。')
            chunks.append(chunk)
    return b''.join(chunks)


def read_source(content: bytes, columns=PUBLIC_COLUMNS):
    import pyarrow.parquet as pq
    if len(content) != SOURCE_BYTES or hashlib.sha256(content).hexdigest() != SOURCE_SHA256:
        raise ValueError('SWE-bench Pro 固定数据校验失败；未替换目录或保存题目。')
    if columns is None or not set(columns) <= set(PUBLIC_COLUMNS):
        raise ValueError('SWE-bench Pro 目录和题目只允许读取公开字段。')
    table = pq.read_table(io.BytesIO(content), columns=columns)
    if table.num_rows != 642:
        raise ValueError('SWE-bench Pro 固定快照题目数量不匹配。')
    return table


def download_source(emit=None, *, source_url=SOURCE_URL) -> bytes:
    if emit:
        emit('phase', '读取 SWE-bench Pro V2 固定数据（约 6.51 MB），不会下载镜像')
    content = _fetch(source_url, SOURCE_BYTES)
    read_source(content, ['instance_id'])
    return content


def catalog_rows(emit=None) -> list[dict]:
    return public_rows(emit)


def asset_dir(row: dict) -> Path:
    return STATE / 'pro-assets' / GH_REVISION[:12] / hashlib.sha256(_instance(row).encode()).hexdigest()[:16]


def _validated_assets(row: dict, contents: dict[str, bytes]) -> dict:
    expected = manifest()[_instance(row)]
    if set(expected) != set(ASSET_FILES) or set(contents) != set(ASSET_FILES):
        raise ValueError('SWE-bench Pro 官方验证资源不完整，请重新缓存题目。')
    for name, raw in contents.items():
        if hashlib.sha256(raw).hexdigest() != expected[name]:
            raise ValueError(f'SWE-bench Pro 官方资源校验失败：{name}；请重新缓存题目。')
    task = tomllib.loads(contents['task.toml'].decode('utf-8'))
    image = 'ghcr.io/scaleapi/swe-bench_pro-v2:' + row['instance_id']
    if task['environment']['docker_image'] != image or row.get('docker_image', image) != image:
        raise ValueError('SWE-bench Pro 官方镜像与题目不匹配。')
    instruction = contents['instruction.md'].decode('utf-8')
    workspace = re.search(r'A code repository is available in the `(/app|/testbed)` directory', instruction)
    if not workspace or task['agent'].get('network_mode') != 'no-network':
        raise ValueError('SWE-bench Pro 官方任务环境约束不匹配。')
    if task['environment'].get('cpus') != 1 or task['environment'].get('memory_mb') != 4096:
        raise ValueError('SWE-bench Pro 官方任务资源限制尚未适配；已停止运行。')
    return {'working_dir': workspace.group(1), 'agent_timeout': float(task['agent']['timeout_sec']),
            'verifier_timeout': float(task['verifier']['timeout_sec']),
            'agent_network': task['agent']['network_mode'], 'verifier_network': task['verifier']['network_mode'],
            'cpus':task['environment']['cpus'],'memory_mb':task['environment']['memory_mb'],
            'storage_mb':task['environment']['storage_mb']}


def asset_paths(row: dict) -> dict[str, Path]:
    paths = {name: asset_dir(row) / name for name in ASSET_FILES}
    if not all(path.is_file() for path in paths.values()):
        raise ValueError('SWE-bench Pro 官方验证资源尚未完整缓存，请先缓存题目。')
    _validated_assets(row, {name: path.read_bytes() for name, path in paths.items()})
    return paths


def check_asset_bundle(row: dict) -> dict[str, Path]:
    return asset_paths(row)


def ensure_asset_bundle(row: dict, emit=None, *, asset_base_url=ASSET_BASE_URL) -> dict[str, Path]:
    instance = _instance(row)
    expected = manifest()[instance]
    paths = {name: asset_dir(row) / name for name in ASSET_FILES}
    contents = {}
    changed = []
    for name, path in paths.items():
        raw = path.read_bytes() if path.is_file() else b''
        if hashlib.sha256(raw).hexdigest() != expected[name]:
            if emit:
                emit('phase', f'缓存 SWE-bench Pro 官方题目资源：{name}')
            raw = _fetch(f'{asset_base_url}/{instance}/{name}', MAX_ASSET_BYTES)
            changed.append(name)
        contents[name] = raw
    _validated_assets(row, contents)
    # The immutable hashes make partially published bundles unusable to readers.
    # Verify the whole download first, so failed downloads preserve usable assets.
    for name in changed:
        atomic_bytes(paths[name], contents[name])
    return asset_paths(row)


def normalize_case(row: dict, paths: dict[str, Path]) -> dict:
    public = public_case(row)
    public['asset_revision'] = GH_REVISION
    public['image_name'] = public['docker_image']
    public['instruction'] = paths['instruction.md'].read_text(encoding='utf-8')
    public['pro_task'] = _validated_assets(public, {name: path.read_bytes() for name, path in paths.items()})
    public['working_dir'] = public['pro_task']['working_dir']
    return public


def load_case(instance: str, emit=None, *, source_url=SOURCE_URL, asset_base_url=ASSET_BASE_URL,
              public_cache_path: Path | None = None) -> dict:
    _instance({'instance_id': instance})
    row = next(row for row in public_rows(emit, source_url=source_url, cache_path=public_cache_path)
               if row['instance_id'] == instance)
    return normalize_case(row, ensure_asset_bundle(row, emit, asset_base_url=asset_base_url))


def solver_input(row: dict) -> str:
    expected = manifest()[_instance(row)]['instruction.md']
    instruction = row.get('instruction', '')
    if hashlib.sha256(instruction.encode('utf-8')).hexdigest() != expected:
        raise ValueError('SWE-bench Pro 官方题目说明校验失败，请重新缓存题目。')
    return instruction
