import ctypes
import json
import subprocess
import sys
import threading
from ctypes import wintypes
from uuid import uuid4

import pytest
from fastapi import HTTPException

from local_setup.webui.common import STATE, atomic, job_dir, new_id, now, read_json, save_json
from local_setup.webui.docker_runtime import DockerHostError, check_docker_result
from local_setup.webui.processes import Job
from local_setup.webui.queues import BatchControl, Queues
from local_setup.webui.runtime import stream_command
from local_setup.webui.server import Manager
from local_setup.webui.settings import Settings, inspect_settings


def saved_queue():
    settings=inspect_settings()
    return {'id':new_id(),'request_id':uuid4().hex,'dataset':'verified','operation':'cache',
            'status':'paused','created':now(),'current_run':None,'current_index':None,'reason':'',
            'settings_version':settings['version'],'settings':{k:settings[k] for k in Settings.model_fields},
            'allow_data_download':False,'allow_image_download':False,'auto_approve':False,'auto_evaluate':False,
            'total_budget':1,'diagnostics':{},'billing_unknown':False,
            'items':[{'instance_id':'audit__fixture-1000','stages':['cache'],'stage_index':0,'status':'pending',
                      'attempts':[],'retry':0,'prediction_path':'','error':''}]}


def lock_file(path):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
    assert handle not in (None,ctypes.c_void_p(-1).value)
    return kernel,handle


def test_timeout_stops_owned_command_and_is_fatal_for_docker(tmp_path):
    marker=tmp_path/'late-write.txt'
    events=[]
    result=stream_command([sys.executable,'-c','import pathlib,sys,time;time.sleep(5);pathlib.Path(sys.argv[1]).write_text("late")',str(marker)],
                          emit=lambda kind,data:events.append((kind,data)),timeout=.1)
    assert result['timed_out'] and result['returncode']==-1 and not marker.exists()
    assert events[0][0]=='command_process'
    with pytest.raises(DockerHostError,match='已停止本次求解和后续模型请求'):
        check_docker_result(result)
    assert check_docker_result({'returncode':1,'timed_out':False})['returncode']==1


@pytest.mark.parametrize(('emits_event','outcome'),[(False,'completed'),(True,'interrupted')])
def test_pump_and_cleanup_release_slot_when_final_metadata_is_locked(emits_event,outcome):
    manager=Manager()
    run_id=new_id()
    meta={'id':run_id,'request_id':uuid4().hex,'kind':'swebench' if emits_event else 'offline','title':'file lock recovery',
          'created':now(),'model':'deterministic','status':'cleaning','outcome':'completed','containers':[]}
    manager.store(meta)
    script='print('+repr('@@MINI_WEB@@'+json.dumps({'kind':'billing','data':{'pending':True,'unknown':False}}))+')' if emits_event else 'pass'
    process=subprocess.Popen([sys.executable,'-c',script],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    job=Job(process)
    manager.active,manager.process,manager.job=run_id,process,job
    manager.idle.clear()
    kernel,handle=lock_file(job_dir(run_id)/'meta.json')
    try:
        manager.pump(run_id,process,job)
        assert process.poll() is not None and manager.active is None and manager.idle.is_set()
        assert manager.get(run_id)['recovery_pending'] and manager.get(run_id)['status']=='cleanup_required'
        assert read_json(job_dir(run_id)/'recovery.json')['outcome']==outcome
        if emits_event:assert manager.get(run_id)['billing']['unknown']
        assert manager.retry_cleanup(run_id)['recovery_pending']
        assert manager.active is None and manager.idle.is_set()
    finally:
        kernel.CloseHandle(handle)
        job.close()
    manager.recover()
    assert manager.get(run_id)['recovery_pending']
    assert manager.retry_cleanup(run_id)['status']==outcome
    assert not (job_dir(run_id)/'recovery.json').exists()
    assert not read_json(job_dir(run_id)/'meta.json').get('recovery_pending')


def test_cleanup_recovery_keeps_original_solve_and_advances_to_evaluation():
    manager=Manager()
    queues=Queues(manager)
    q=saved_queue()
    q.update(operation='solve',auto_evaluate=True,current_index=0)
    run_id=new_id()
    path=job_dir(run_id)/'predictions.jsonl'
    atomic(path,json.dumps({'instance_id':'audit__fixture-1000','model_name_or_path':'deterministic',
                           'model_patch':'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'})+'\n')
    child={'id':run_id,'request_id':uuid4().hex,'kind':'swebench','title':'cleanup recovery',
           'created':now(),'model':'deterministic','status':'cleanup_required','outcome':'completed',
           'containers':[],'cleanup_pending':[],'recovery_pending':True,'prediction':str(path),'stats':{'cost':.2,'calls':2}}
    manager.store(child)
    q['current_run']=run_id
    item=q['items'][0]
    item.update(stages=['download','solve','evaluate'],stage_index=1,status='running',
                attempts=[{'stage':'solve','request_id':child['request_id'],'run_id':run_id,'status':'starting','cost':0}])
    queues.store(q)
    queues.consume(q,child)
    assert q['status']=='paused' and q['current_run']==run_id and item['status']=='running'
    other=saved_queue()
    queues.store(other)
    queues.recover()
    q=queues.get(q['id']);item=q['items'][0]
    assert q['current_run']==run_id and item['status']=='running' and '多个未结束批次' in queues.recovery_issue
    manager.retry_cleanup(run_id)
    queues.tick()
    assert q['current_run'] is None and item['stage_index']==2 and item['status']=='pending'
    assert item['solve_run_id']==run_id and item['prediction_path']==str(path)
    assert len(item['attempts'])==1 and queues.spent(q)==pytest.approx(.2)
    queues.control(q['id'],BatchControl(action='finish'))
    assert not queues.recovery_issue
    queues.control(other['id'],BatchControl(action='finish'))


def test_bad_queue_records_are_isolated_and_multiple_queue_block_is_recomputed():
    manager=Manager()
    queues=Queues(manager)
    fixtures=[saved_queue() for _ in range(5)]
    fixtures[2].pop('status')
    fixtures[3]['items']=[None]
    fixtures[4]['items'][0].pop('attempts')
    paths=[STATE/'queues'/q['id']/'queue.json' for q in fixtures]
    try:
        for path,q in zip(paths,fixtures):save_json(path,q)
        originals=[p.read_bytes() for p in paths[2:]]
        queues.recover()
        assert all(q['id'] not in queues.records for q in fixtures[2:])
        assert [p.read_bytes() for p in paths[2:]]==originals
        assert '损坏批次' in queues.recovery_issue
        for path in paths[2:]:path.unlink()
        queues.recover()
        assert '多个未结束批次' in queues.recovery_issue
        queues.control(fixtures[1]['id'],BatchControl(action='finish'))
        assert queues.recovery_issue=='' and queues.get(fixtures[0]['id'])['status']=='paused'
        queues.control(fixtures[0]['id'],BatchControl(action='finish'))
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
            path.parent.rmdir()


def test_locked_queue_recovery_pauses_without_crashing_startup():
    queues=Queues(Manager())
    q=saved_queue()
    path=STATE/'queues'/q['id']/'queue.json'
    save_json(path,q)
    original=path.read_bytes()
    kernel,handle=lock_file(path)
    try:
        queues.recover()
        assert queues.get(q['id'])['status']=='paused' and q['id'] in queues.invalid_records
        assert path.read_bytes()==original
    finally:
        kernel.CloseHandle(handle)
    queues.recover()
    assert not queues.recovery_issue
    queues.control(q['id'],BatchControl(action='finish'))


def test_cancel_and_exit_claim_are_mutually_exclusive():
    manager=Manager()
    for _ in range(60):
        ticket=new_id()
        manager.lifecycle={'id':ticket,'status':'ready','action':'restart'}
        manager.lifecycle_ack=threading.Event()
        manager.accepting=False
        barrier=threading.Barrier(2)
        results={}
        def cancel():
            barrier.wait()
            try:
                results['cancel']=manager.cancel_lifecycle(ticket)['status']=='idle'
            except HTTPException as exc:
                assert exc.status_code==409
                results['cancel']=False
        def commit():
            barrier.wait()
            results['exit']=manager.commit_lifecycle_exit(ticket)
        threads=[threading.Thread(target=cancel),threading.Thread(target=commit)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=2)
        assert not any(thread.is_alive() for thread in threads)
        assert results['cancel']!=results['exit']
        assert manager.lifecycle['status']==('idle' if results['cancel'] else 'exiting')
        assert manager.accepting==results['cancel']
