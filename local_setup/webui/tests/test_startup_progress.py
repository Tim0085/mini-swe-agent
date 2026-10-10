import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from local_setup.webui.common import job_dir, new_id, now, read_json
from local_setup.webui.evaluation_progress import LINE_LIMIT, READ_SIZE, Progress
from local_setup.webui.server import NONCE, Manager, app, manager
from local_setup.webui.tests.test_execution_recovery import lock_file


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing semantics')
def test_locked_startup_record_keeps_ui_and_request_identity_until_explicit_recovery(tmp_path):
    original=Manager()
    interrupted={'id':new_id(),'request_id':uuid4().hex,'kind':'offline','title':'Startup recovery fixture',
                 'created':now(),'model':'deterministic','status':'running','containers':[],'pending':{'ticket':'previous'}}
    finished=interrupted | {'id':new_id(),'request_id':uuid4().hex,'status':'completed','pending':None}
    original.store(interrupted)
    original.store(finished)
    path=job_dir(interrupted['id'])/'meta.json'
    before=path.read_bytes()
    finished_before=(job_dir(finished['id'])/'meta.json').read_bytes()
    kernel,handle=lock_file(path)
    try:
        for _ in range(2):
            with TestClient(app,base_url='http://127.0.0.1:8765') as client:
                assert client.post('/session',json={'nonce':NONCE}).status_code==200
                assert client.get('/').status_code==200
                pending=client.get('/api/environment').json()['cleanup_pending']
                assert any(row['id']==interrupted['id'] and row['recovery_pending'] for row in pending)
                recovered=client.get('/api/runs/'+interrupted['id']).json()
                assert recovered['status']=='cleanup_required' and recovered['outcome']=='interrupted'
                assert recovered['pending'] is None and path.read_bytes()==before
                body={'kind':'offline','cwd':str(tmp_path),'request_id':interrupted['request_id'],'settings_version':'unused'}
                assert client.post('/api/runs',json=body).json()['id']==interrupted['id']
                response=client.post('/api/runs',json=body | {'request_id':uuid4().hex})
                assert response.status_code==409 and '状态恢复' in response.json()['detail']
                assert client.post('/api/runs/'+interrupted['id']+'/cleanup',json={}).json()['recovery_pending']
                assert manager.active is None and path.read_bytes()==before
                assert read_json(job_dir(interrupted['id'])/'recovery.json')['outcome']=='interrupted'
    finally:
        kernel.CloseHandle(handle)
        manager.retry_cleanup(interrupted['id'])
    with TestClient(app,base_url='http://127.0.0.1:8765') as client:
        assert client.post('/session',json={'nonce':NONCE}).status_code==200
        recovered=client.get('/api/runs/'+interrupted['id']).json()
        assert recovered['status']=='interrupted' and not recovered.get('recovery_pending')
        assert not (job_dir(interrupted['id'])/'recovery.json').exists()
        assert manager.request_ids[interrupted['request_id']]==interrupted['id']
        assert (job_dir(finished['id'])/'meta.json').read_bytes()==finished_before


def log_path(tmp_path):
    path=tmp_path/'logs/run_evaluation/run/model/issue/run_instance.log'
    path.parent.mkdir(parents=True)
    return path


def test_long_line_advances_cursor_and_retains_later_stage_with_bounded_display(tmp_path):
    path=log_path(tmp_path)
    path.write_bytes(('x'*70000+'\nGrading answer for fixture\n').encode())
    events=[]
    watcher=Progress(tmp_path,lambda kind,data:events.append((kind,data)))
    watcher.scan()
    assert watcher.positions[path]==READ_SIZE and len(watcher.pending[path]['text'])==LINE_LIMIT
    watcher.scan()
    assert watcher.positions[path]==path.stat().st_size and watcher.state['stage']=='汇总测试结果'
    logs=[data for kind,data in events if kind=='log']
    assert len(logs)==2 and '单行过长' in logs[0] and len(logs[0])<LINE_LIMIT+80
    watcher.scan()
    assert len([event for event in events if event[0]=='log'])==2


def test_utf8_and_stage_markers_survive_chunk_boundaries_and_final_unterminated_line(tmp_path):
    path=log_path(tmp_path)
    # One byte of a three-byte character is read in the first chunk.
    prefix=('x\n'*(READ_SIZE//2-1)+'a').encode()
    path.write_bytes(prefix+'中Grading ans'.encode())
    events=[]
    watcher=Progress(tmp_path,lambda kind,data:events.append((kind,data)))
    watcher.scan()
    assert watcher.pending[path]['decoder'].getstate()[0]==b'\xe4'
    watcher.scan()
    assert watcher.pending[path]['text']=='a中Grading ans'
    with path.open('ab') as stream:
        stream.write(b'wer for fixture')
    watcher.scan()
    assert watcher.state['stage']=='汇总测试结果'
    watcher.scan(final=True)
    logs=[data for kind,data in events if kind=='log']
    assert logs[-1]=='[评测阶段日志] a中Grading answer for fixture'
    assert not any('\ufffd' in text for text in logs)
    watcher.scan(final=True)
    assert len([event for event in events if event[0]=='log'])==len(logs)


def test_final_scan_drains_all_chunks_and_truncated_file_resets_pending_state(tmp_path):
    path=log_path(tmp_path)
    path.write_bytes(b'x'*(READ_SIZE*3)+b'\nResult for final fixture')
    events=[]
    watcher=Progress(tmp_path,lambda kind,data:events.append((kind,data)))
    watcher.scan(final=True)
    assert watcher.positions[path]==path.stat().st_size and watcher.state['stage']=='评测结果已生成'
    assert len([event for event in events if event[0]=='log'])==2
    path.write_bytes(b'Creating container new fixture\n')
    watcher.scan()
    assert watcher.state['stage']=='创建评测容器' and watcher.positions[path]==path.stat().st_size
    assert [data for kind,data in events if kind=='log'][-1]=='[评测阶段日志] Creating container new fixture'
