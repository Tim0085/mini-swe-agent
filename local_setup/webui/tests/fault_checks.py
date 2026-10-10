"""Real process/state failure checks in a private instance, without paid models."""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from live_instance import ROOT, LOCATOR, start

OUT=ROOT/'collaboration/artifacts/2026-09-15/workbench-fixes/fault-checks.json'
checks=[]


def alive(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:
        return False
    code=ctypes.c_ulong()
    kernel.GetExitCodeProcess(handle,ctypes.byref(code))
    kernel.CloseHandle(handle)
    return code.value==259


class Instance:
    def __init__(self):
        locator=json.loads(LOCATOR.read_text())
        self.state=Path(locator['state'])
        self.config=Path(locator['config_root'])
        self.reload_record()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def reload_record(self):
        self.record=json.loads((self.state/'service.json').read_text())
        self.base=f"http://127.0.0.1:{self.record['port']}"

    def api(self,path,body=None,method=None):
        headers={'Cookie':'mini_session='+self.record['nonce']}
        if body is not None:
            headers['Content-Type']='application/json'
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
        with self.opener.open(request,timeout=12) as response:
            return json.load(response)

    def until(self,read,accept,seconds=35):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=read()
            if accept(value):
                return value
            time.sleep(.1)
        raise RuntimeError('Timed out waiting for the existing instance; do not restart blindly.')

    def run(self,scenario='basic'):
        work=self.state/'workspaces'/uuid.uuid4().hex
        work.mkdir(parents=True)
        run=self.api('/api/runs',{'kind':'offline','offline_scenario':scenario,'task':'isolated lifecycle fixture','cwd':str(work),
                               'request_id':uuid.uuid4().hex,'settings_version':self.api('/api/config')['version']})
        return run['id'],work

    def state_of(self,run_id,states):
        return self.until(lambda:self.api('/api/runs/'+run_id),lambda x:x['status'] in states)

    def events(self,run_id):
        path=self.state/'runs'/run_id/'events.jsonl'
        if not path.exists():
            return []
        content=path.read_text(encoding='utf-8',errors='ignore')
        return [json.loads(line) for line in content.split('\n')[:-1] if line]

    def shutdown(self,action='exit',allow_pending=False):
        before=self.api('/health')
        pid=self.record['pid']
        self.api('/api/lifecycle',{'action':action,'mode':'stop','allow_pending_cleanup':allow_pending})
        ready=self.until(lambda:self.api('/api/environment')['lifecycle'],lambda x:x['status'] in {'ready','blocked'})
        assert ready['status']=='ready',ready
        self.api('/api/lifecycle/ack',{'id':ready['id']})
        if action=='exit':
            self.until(lambda:alive(pid),lambda x:not x)
            try:
                self.api('/health')
            except OSError:
                return
            raise AssertionError('Server socket still open')
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            try:
                result=self.api('/health')
                if result['boot_id']!=before['boot_id']:
                    self.reload_record()
                    return
            except OSError:
                pass
            time.sleep(.2)
        raise RuntimeError('Restart observation timed out; recheck this same handle.')

    def cold_start(self):
        assert not alive(self.record['pid'])
        env=os.environ | {'MINI_WEB_STATE':str(self.state),'MINI_WEB_CONFIG_ROOT':str(self.config),'MINI_WEB_PORT':str(self.record['port']),'MINI_WEB_TESTING':'1','PYTHONUTF8':'1'}
        process=subprocess.run([str(ROOT/'.venv/Scripts/python.exe'),str(ROOT/'local_setup/webui/launch.py'),'--no-browser'],cwd=ROOT,env=env,capture_output=True,timeout=45)
        assert process.returncode==0
        self.reload_record()
        assert self.api('/health')['identity']==self.record['identity']


def passed(name):
    checks.append(name)
    print('PASS',name,flush=True)


if __name__=='__main__':
    start()
    instance=Instance()
    try:
        for scenario in ['basic','slow_model','slow_shell']:
            run_id,work=instance.run(scenario)
            if scenario=='basic':
                instance.state_of(run_id,{'awaiting_confirmation'})
            elif scenario=='slow_model':
                instance.until(lambda:instance.api('/api/runs/'+run_id),lambda x:x.get('phase')=='等待模型回复')
            else:
                ready=instance.state_of(run_id,{'awaiting_confirmation'})
                instance.api(f'/api/runs/{run_id}/approve',{'ticket':ready['pending']['ticket'],'approve':True})
                lines=instance.until(lambda:instance.events(run_id),lambda rows:any(x['kind']=='log' and 'STREAM_STARTED' in x['data'] for x in rows))
                shell=next(x['data']['pid'] for x in lines if x['kind']=='command_process')
                assert alive(shell)
                assert 'STREAM_FINISHED' not in '\n'.join(x['data'] for x in lines if x['kind']=='log')
            worker=instance.api('/api/environment')['worker_pid']
            assert worker and alive(worker)
            instance.shutdown()
            assert not alive(worker)
            if scenario=='slow_shell':
                assert not alive(shell)
            final=json.loads((instance.state/'runs'/run_id/'meta.json').read_text(encoding='utf-8'))
            assert final['status']=='cancelled' and not final['pending'] and not final['cleanup_pending']
            passed('actual service exit during '+scenario)
            instance.cold_start()

        run_id,work=instance.run()
        instance.state_of(run_id,{'awaiting_confirmation'})
        worker=instance.api('/api/environment')['worker_pid']
        os.kill(instance.record['pid'],15)
        instance.until(lambda:alive(worker),lambda x:not x)
        instance.cold_start()
        assert instance.api('/api/runs/'+run_id)['status']=='interrupted'
        assert instance.api('/api/environment')['active_run'] is None
        passed('abrupt owned-service termination kills worker and recovers without rerunning')

        original_pid=instance.record['pid']
        (instance.state/'service.json').write_text('{broken',encoding='utf-8')
        env=os.environ | {'MINI_WEB_STATE':str(instance.state),'MINI_WEB_CONFIG_ROOT':str(instance.config),'MINI_WEB_PORT':str(instance.record['port']),'MINI_WEB_TESTING':'1'}
        launched=subprocess.run([str(ROOT/'.venv/Scripts/python.exe'),str(ROOT/'local_setup/webui/launch.py'),'--no-browser'],env=env,capture_output=True,timeout=30)
        assert launched.returncode==0 and alive(original_pid)
        instance.api('/api/lifecycle/repair-link',{})
        assert json.loads((instance.state/'service.json').read_text())['pid']==original_pid
        assert any(p.read_text()=='{broken' for p in (instance.state/'recovery').glob('service-*.json'))
        passed('corrupt locator reuses live instance and preserves damaged file')

        poison=instance.state/'runs/20000101-000000-ffffffff'
        poison.mkdir()
        (poison/'meta.json').write_text('broken',encoding='utf-8')
        for i in range(1000):
            run_id=f'20000101-000000-{i:08x}'
            folder=instance.state/'runs'/run_id
            folder.mkdir()
            (folder/'meta.json').write_text(json.dumps({'id':run_id,'request_id':'scale-'+uuid.uuid4().hex,'status':'completed','kind':'offline',
                'title':'scale-fixture '+str(i),'model':'deterministic','created':'2000-01-01T00:00:00+00:00','updated':'2000-01-01T00:00:00+00:00','containers':[]}),encoding='utf-8')
        instance.shutdown('restart')
        begin=time.monotonic()
        page=instance.api('/api/history?search=scale-fixture&limit=20&offset=980')
        elapsed=time.monotonic()-begin
        assert page['total']==1000 and len(page['items'])==20 and elapsed<3
        assert instance.api('/api/environment')['issues']
        assert (poison/'meta.json').read_text()=='broken'
        for offset in range(0,1000,100):
            ids=['run:'+f'20000101-000000-{i:08x}' for i in range(offset,offset+100)]
            plan=instance.api('/api/storage/preview',{'ids':ids})
            instance.api('/api/storage',{'token':plan['token']},'DELETE')
        assert instance.api('/api/history?search=scale-fixture')['total']==0
        passed('1000-record pagination, corrupt-record isolation, and exact fixture cleanup')

        yaml=instance.config/'local_setup/windows.yaml'
        yaml.write_text('broken: [',encoding='utf-8')
        view=instance.api('/api/config')
        assert not view['valid'] and view['api_base']=='https://example.invalid/v1'
        body={key:view[key] for key in ['model_name','api_base','model_class','step_limit','cost_limit','timeout']}
        rebuilt=instance.api('/api/config',{'settings':body,'version':view['version'],'repair':True},'PUT')
        assert rebuilt['valid']
        passed('config repair remains reachable and preserves readable relay settings')

        run_id,work=instance.run('edit')
        for _ in range(2):
            ready=instance.state_of(run_id,{'awaiting_confirmation'})
            instance.api(f'/api/runs/{run_id}/approve',{'ticket':ready['pending']['ticket'],'approve':True})
        done=instance.state_of(run_id,{'completed'})
        assert (work/'outcome.txt').is_file()
        assert any(x['path']=='outcome.txt' and x['change']=='新增' for x in done['changes']['files'])
        passed('real offline edit produces independent before-after file inventory')

        orphan_id='20000101-000001-ffffffff'
        orphan=instance.state/'runs'/orphan_id
        orphan.mkdir()
        # A synthetic orphan record exercises the real unavailable-Docker path; no container is created.
        entry={'id':orphan_id,'status':'cleanup_required','outcome':'interrupted','kind':'swebench','title':'synthetic orphan fixture',
               'created':'2000-01-01T00:00:00+00:00','updated':'2000-01-01T00:00:00+00:00','model':'deterministic',
               'containers':['mini-web-'+orphan_id],'cleanup_pending':['mini-web-'+orphan_id]}
        (orphan/'meta.json').write_text(json.dumps(entry),encoding='utf-8')
        instance.shutdown('restart')
        result=instance.api('/api/runs/'+orphan_id+'/cleanup',{})
        if result['status']=='cleanup_required':
            instance.api('/api/lifecycle',{'action':'exit','mode':'stop'})
            blocked=instance.until(lambda:instance.api('/api/environment')['lifecycle'],lambda x:x['status']=='blocked')
            assert blocked['can_exit_with_pending'] and alive(instance.record['pid'])
            instance.shutdown(allow_pending=True)
            assert json.loads((orphan/'meta.json').read_text())['status']=='cleanup_required'
            passed('unavailable Docker retains cleanup record; explicit keep-record exit closes Python')
            instance.cold_start()
            assert instance.api('/api/runs/'+orphan_id)['cleanup_pending']
            instance.shutdown(allow_pending=True)
        else:
            assert not result['cleanup_pending']
            instance.shutdown()
            passed('available Docker confirms synthetic orphan absent and cleanup completes')
        assert orphan.resolve().parent==(instance.state/'runs').resolve()
        (orphan/'meta.json').unlink()
        (orphan/'events.jsonl').unlink(missing_ok=True)
        orphan.rmdir()
        instance.cold_start()

        instance.shutdown()
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps({'passed':True,'checks':checks,'paid_model_calls':0,'production_service_touched':False,'history_page_seconds':elapsed},ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception as error:
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps({'passed':False,'checks':checks,'error':str(error),'paid_model_calls':0,'production_service_touched':False},ensure_ascii=False,indent=2),encoding='utf-8')
        raise
