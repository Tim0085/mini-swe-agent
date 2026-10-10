import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from local_setup.webui.bench import dataset_file, load_instance
from local_setup.webui.benchmarks import PRO_ID
from local_setup.webui.common import ROOT, atomic_bytes, save_json
from local_setup.webui.library import data_cache_state
from local_setup.webui.pro_data import (HF_REVISION, PUBLIC_CACHE, PUBLIC_COLUMNS, SOURCE_SHA256,
                                      catalog_rows, load_case, public_manifest, public_rows, read_source)
from local_setup.webui.tests.test_pro_cache_identity import cached_case
from local_setup.webui.tests.test_pro_data import INSTANCE, fixture_assets, source_bytes, source_server


def test_original_case_remains_offline_without_shared_projection():
    record, _ = cached_case()
    previous = PUBLIC_CACHE.read_bytes() if PUBLIC_CACHE.is_file() else None
    PUBLIC_CACHE.unlink(missing_ok=True)
    try:
        assert load_instance(PRO_ID, INSTANCE, download=False) == record
        assert data_cache_state(PRO_ID, INSTANCE)['data_cached']
        assert not PUBLIC_CACHE.exists()
    finally:
        if previous is not None:
            atomic_bytes(PUBLIC_CACHE, previous)


def test_shared_public_cache_reuses_source_and_excludes_hidden_columns(tmp_path):
    path = tmp_path / 'public.json'
    with source_server(source_bytes(), fixture_assets()) as (base, _, requests):
        rows = public_rows(source_url=base + '/source', cache_path=path)
        assert len(rows) == len(public_manifest()) == 642
        assert len({row['instance_id'] for row in rows}) == 642
        assert all(set(row) == set(PUBLIC_COLUMNS) for row in rows)
        assert load_case(INSTANCE, source_url=base + '/source', asset_base_url=base + '/assets',
                         public_cache_path=path)['base_commit'] == next(row['base_commit'] for row in rows if row['instance_id'] == INSTANCE)
        assert public_rows(source_url='http://127.0.0.1:1', cache_path=path) == rows
        assert public_rows(download=False, cache_path=path) == rows
        assert requests.count('/source') == 1
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert set(saved) == {'source_revision', 'source_sha256', 'items'}
    assert saved['source_sha256'] == SOURCE_SHA256 and saved['source_revision'] == HF_REVISION
    assert not {'patch', 'test_patch', 'fail_to_pass', 'pass_to_pass'} & set(saved['items'][0])
    assert not list(tmp_path.glob('*.parquet'))


@pytest.mark.parametrize(('damage',), [('base_commit',), ('answer_column',), ('duplicate',), ('revision',)])
def test_corrupt_shared_cache_requires_authorization_and_preserves_old_file_on_failure(tmp_path, damage):
    path = tmp_path / 'public.json'
    raw = source_bytes()
    with source_server(raw, {}) as (base, state, requests):
        original = public_rows(source_url=base + '/source', cache_path=path)
        damaged = json.loads(path.read_text(encoding='utf-8'))
        if damage == 'base_commit':
            damaged['items'][0]['base_commit'] = '0' * 40
        elif damage == 'answer_column':
            damaged['items'][0]['patch'] = 'must-not-be-in-public-data'
        elif damage == 'duplicate':
            damaged['items'][1] = damaged['items'][0]
        else:
            damaged['source_revision'] = 'wrong'
        save_json(path, damaged)
        previous = path.read_bytes()
        with pytest.raises(ValueError, match='允许下载'):
            public_rows(download=False, source_url=base + '/source', cache_path=path)
        assert path.read_bytes() == previous and requests == ['/source']
        state['source'] = raw[:-1] + b'X'
        with pytest.raises(ValueError, match='校验'):
            public_rows(source_url=base + '/source', cache_path=path)
        assert path.read_bytes() == previous
        with pytest.raises(httpx.HTTPStatusError):
            public_rows(source_url=base + '/missing', cache_path=path)
        assert path.read_bytes() == previous
        state['source'] = raw
        assert public_rows(source_url=base + '/source', cache_path=path) == original
        assert public_rows(download=False, cache_path=path) == original


def test_bad_single_case_public_fields_repair_from_verified_projection_without_network():
    original, _ = cached_case()
    save_json(PUBLIC_CACHE, {'source_revision': HF_REVISION, 'source_sha256': SOURCE_SHA256,
                             'items': read_source(source_bytes()).to_pylist()})
    assert len(catalog_rows()) == 642
    path = dataset_file(PRO_ID, INSTANCE)
    for field, value in [('base_commit', '0' * 40), ('repo', 'wrong/repository'), ('requirements', 'incorrect')]:
        save_json(path, [original | {field: value}])
        previous = path.read_bytes()
        with pytest.raises(ValueError, match='允许下载'):
            load_instance(PRO_ID, INSTANCE, download=False)
        assert path.read_bytes() == previous
        assert not data_cache_state(PRO_ID, INSTANCE)['data_cached']
        assert load_instance(PRO_ID, INSTANCE, download=True) == original
        assert data_cache_state(PRO_ID, INSTANCE)['data_cached']
        assert json.loads(path.read_text(encoding='utf-8')) == [original]


@contextmanager
def delayed_source(raw):
    arrived, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            arrived.set()
            if not release.wait(15):
                return
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/source', arrived, release, requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def start_reader(url, path):
    code = ("import sys; from pathlib import Path; from local_setup.webui.pro_data import public_rows; "
            "print('ready',flush=True); "
            "print(len(public_rows(source_url=sys.argv[1],cache_path=Path(sys.argv[2]))),flush=True)")
    process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code, url, str(path)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert process.stdout.readline().strip() == 'ready'
    return process


def test_concurrent_processes_share_one_verified_download(tmp_path):
    path = tmp_path / 'public.json'
    with delayed_source(source_bytes()) as (url, arrived, release, requests):
        processes = [start_reader(url, path)]
        try:
            assert arrived.wait(5)
            processes.extend(start_reader(url, path) for _ in range(2))
            release.set()
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                assert process.returncode == 0, stderr
                assert stdout.strip() == '642'
            assert requests == ['/source']
            assert len(public_rows(download=False, cache_path=path)) == 642
        finally:
            release.set()
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)


def test_cancelled_download_preserves_cache_and_releases_cross_process_lock(tmp_path):
    path = tmp_path / 'public.json'
    atomic_bytes(path, b'preserve-this-invalid-cache-until-repaired')
    previous = path.read_bytes()
    with delayed_source(source_bytes()) as (url, arrived, release, requests):
        process = start_reader(url, path)
        try:
            assert arrived.wait(5)
            process.terminate()
            process.communicate(timeout=5)
            assert path.read_bytes() == previous
            release.set()
            assert len(public_rows(source_url=url, cache_path=path)) == 642
            assert requests == ['/source', '/source']
            assert not list(tmp_path.glob('*.tmp'))
        finally:
            release.set()
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
