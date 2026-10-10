import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from local_setup.webui.common import ROOT, STATE
from local_setup.webui.pro_data import (ASSET_FILES, GH_REVISION, PUBLIC_COLUMNS, SOURCE_BYTES,
                                       SOURCE_SHA256, asset_dir, asset_paths, download_source,
                                       ensure_asset_bundle, load_case, manifest, normalize_case, read_source, solver_input)

SOURCE = ROOT / '.local/webui/pro-20261010/upstream-data/v2-default.parquet'
FIXTURES = SOURCE.parent / 'asset-fixture'
INSTANCE = ('instance_qutebrowser__qutebrowser-0833b5f6f140d04200ec91605f88704dd18e2970'
            '-v059c6fdc75567943479b23ebca7c07b5e9a7f34c')


def source_bytes():
    if not SOURCE.is_file():
        pytest.skip('Pinned upstream Parquet fixture has not been downloaded; no network in this test.')
    return SOURCE.read_bytes()


def fixture_assets():
    paths = {name: FIXTURES / INSTANCE / name for name in ASSET_FILES}
    if not all(path.is_file() for path in paths.values()):
        pytest.skip('Pinned private official assets are absent; no automatic test downloads.')
    return {name: path.read_bytes() for name, path in paths.items()}


def row():
    return next(item for item in read_source(source_bytes()).to_pylist() if item['instance_id'] == INSTANCE)


@contextmanager
def source_server(content: bytes, assets: dict):
    requests = []
    state = {'corrupt': '', 'missing': '', 'source': content}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            name = self.path.removeprefix('/assets/' + INSTANCE + '/')
            if self.path == '/source':
                raw = state['source']
            elif name in assets and name != state['missing']:
                raw = assets[name] + (b'corrupt' if name == state['corrupt'] else b'')
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_manifest_full_scope_and_no_solution_paths():
    assert len(manifest()) == 642
    assert all(set(files) == set(ASSET_FILES) for files in manifest().values())
    assert all(len(digest) == 64 for files in manifest().values() for digest in files.values())
    assert not {'patch', 'test_patch', 'fail_to_pass', 'pass_to_pass', 'before_repo_set_cmd'} & set(PUBLIC_COLUMNS)
    assert asset_dir({'instance_id': INSTANCE}).is_relative_to(STATE / 'pro-assets')
    assert len(asset_dir({'instance_id': INSTANCE}).name) == 16
    with pytest.raises(ValueError, match='题号'):
        asset_dir({'instance_id': '../' + INSTANCE})
    with pytest.raises(ValueError, match='版本'):
        asset_dir({'instance_id': INSTANCE, 'asset_revision': 'old-revision'})


def test_fixed_source_full_642_case_metadata_and_public_projection():
    raw = source_bytes()
    assert len(raw) == SOURCE_BYTES and hashlib.sha256(raw).hexdigest() == SOURCE_SHA256
    rows = read_source(raw).to_pylist()
    assert len(rows) == len({item['instance_id'] for item in rows}) == 642
    assert set(item['instance_id'] for item in rows) == set(manifest())
    assert sum(item['hard'] for item in rows) == 51
    assert max(len(item['instance_id']) for item in rows) == 120
    assert all(item['version'] == '2.0.0' and item['docker_image'] ==
               'ghcr.io/scaleapi/swe-bench_pro-v2:' + item['instance_id'] for item in rows)
    assert all(item[key] for item in rows for key in ['problem_statement', 'requirements', 'interface'])
    with pytest.raises(ValueError, match='校验'):
        read_source(raw[:-1] + b'X')
    with pytest.raises(ValueError, match='公开字段'):
        read_source(raw, ['patch'])


def test_offline_http_source_checks_hash_and_does_not_persist_answers():
    raw = source_bytes()
    with source_server(raw, {}) as (base, state, requests):
        assert download_source(source_url=base + '/source') == raw
        state['source'] = raw[:-1] + b'X'
        with pytest.raises(ValueError, match='校验'):
            download_source(source_url=base + '/source')
        assert requests == ['/source', '/source']


def test_official_assets_cache_reuse_instruction_identity_and_tamper_rejection():
    public = row()
    assets = fixture_assets()
    with source_server(b'', assets) as (base, state, requests):
        paths = ensure_asset_bundle(public, asset_base_url=base + '/assets')
        count = len(requests)
        assert ensure_asset_bundle(public, asset_base_url='http://127.0.0.1:1') == paths
        assert len(requests) == count
        normalized = normalize_case(public | {'patch': 'answer-sentinel', 'test_patch': 'test-sentinel'}, paths)
        assert normalized['asset_revision'] == GH_REVISION
        assert normalized['working_dir'] == normalized['pro_task']['working_dir'] == '/app'
        assert normalized['pro_task']['agent_timeout'] == normalized['pro_task']['verifier_timeout'] == 3000
        assert normalized['pro_task']['agent_network'] == 'no-network'
        assert normalized['image_name'] == normalized['docker_image']
        assert 'sentinel' not in json.dumps(normalized)
        assert solver_input(normalized).encode() == assets['instruction.md']
        with pytest.raises(ValueError, match='说明校验'):
            solver_input(normalized | {'instruction': normalized['instruction'] + 'modified'})
        with pytest.raises(ValueError, match='镜像'):
            asset_paths(public | {'docker_image': 'untrusted/image:latest'})


def test_full_case_load_over_local_http_has_no_hidden_model_content(tmp_path):
    with source_server(source_bytes(), fixture_assets()) as (base, state, requests):
        loaded = load_case(INSTANCE, source_url=base + '/source', asset_base_url=base + '/assets',
                           public_cache_path=tmp_path / 'public.json')
        assert loaded['instance_id'] == INSTANCE and loaded['repo'] == 'qutebrowser/qutebrowser'
        assert set(loaded) == set(PUBLIC_COLUMNS) | {'asset_revision', 'image_name', 'instruction', 'pro_task', 'working_dir'}
        assert solver_input(loaded) == loaded['instruction']
        assert requests[0] == '/source'
        previous = len(requests)
        with pytest.raises(ValueError, match='题号'):
            load_case('instance_invalid', source_url=base + '/source', asset_base_url=base + '/assets')
        assert len(requests) == previous


def test_failed_asset_download_preserves_existing_files_and_recovers_explicitly():
    public = row()
    assets = fixture_assets()
    with source_server(b'', assets) as (base, state, requests):
        paths = ensure_asset_bundle(public, asset_base_url=base + '/assets')
        paths['tests/test_patch.patch'].unlink()
        before = {name: path.read_bytes() for name, path in paths.items() if path.is_file()}
        state['corrupt'] = 'tests/test_patch.patch'
        with pytest.raises(ValueError, match='资源校验'):
            ensure_asset_bundle(public, asset_base_url=base + '/assets')
        assert {name: paths[name].read_bytes() for name in before} == before
        assert not paths['tests/test_patch.patch'].exists()
        with pytest.raises(ValueError, match='尚未完整缓存'):
            asset_paths(public)
        state['corrupt'] = ''
        state['missing'] = 'tests/test_patch.patch'
        with pytest.raises(httpx.HTTPStatusError):
            ensure_asset_bundle(public, asset_base_url=base + '/assets')
        assert {name: paths[name].read_bytes() for name in before} == before
        state['missing'] = ''
        assert ensure_asset_bundle(public, asset_base_url=base + '/assets') == asset_paths(public)
        assert paths['tests/test_patch.patch'].read_bytes() == assets['tests/test_patch.patch']
        assert all('solution' not in path and 'gold' not in path for path in requests)
