"""Run frontend race checks in a disposable local service with no production credentials."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))


def main():
    state=ROOT/'.local/webui/frontend-state-tests'/uuid4().hex
    config=state/'config-root'
    (config/'.local').mkdir(parents=True)
    (config/'local_setup').mkdir()
    (config/'.local/.env').write_text("MSWEA_MODEL_NAME='openai/offline-browser'\nOPENAI_API_BASE='https://example.invalid/v1'\n",encoding='utf-8')
    (config/'local_setup/windows.yaml').write_bytes((ROOT/'local_setup/webui/defaults.yaml').read_bytes())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    os.environ.update(MINI_WEB_STATE=str(state),MINI_WEB_CONFIG_ROOT=str(config),MINI_WEB_TESTING='1',MINI_WEB_PORT=str(port),
                      MSWEA_GLOBAL_CONFIG_DIR=str(config/'.local'),MSWEA_CONFIGURED='true')
    from local_setup.webui.common import save_json
    from local_setup.webui.library import public_row
    from local_setup.webui.profiles import save_connection,save_connection_key,save_profile
    rows=[]
    for index in range(2):
        row={'instance_id':f'audit__fixture-{1000+index}','repo':'audit/fixture','problem_statement':'Isolated frontend state fixture.'}
        rows.append(public_row(row,index))
        save_json(state/'datasets/verified'/(row['instance_id']+'.json'),[row])
    save_json(state/'catalog/verified.json',{'dataset':'verified','items':rows,'synced_at':'2026-09-22T00:00:00+00:00','note':'Isolated frontend state fixture.'})
    for index in range(25):
        run_id=f'20260922-000000-{index:08x}'
        save_json(state/'runs'/run_id/'meta.json',{'id':run_id,'title':f'Isolated history fixture {index}',
            'kind':'offline','status':'completed','created':'2026-09-22T00:00:00+00:00','updated':'2026-09-22T00:00:01+00:00',
            'finished':'2026-09-22T00:00:01+00:00','model':'offline-fixture','diagnostic':True})
    connection=save_connection({'name':'Isolated frontend connection','api_base':'https://example.invalid/v1'})
    save_connection_key(connection['id'],'isolated-dummy-not-a-real-key',connection['version'])
    profile=save_profile({'name':'Isolated frontend model','connection_id':connection['id'],'model_name':'fixture-frontend',
                          'pricing':{'mode':'free'},'limits':{'step_limit':8,'cost_limit':1,'timeout':30}})
    location={'state':str(state),'port':port,'profile_id':profile['id']}
    env=os.environ.copy()
    env['MINI_FRONTEND_TEST_INSTANCE']=str(state/'instance.json')
    log=(state/'service.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'local_setup/webui/launch.py'),'--serve'],cwd=ROOT,env=env,
        stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    log.close()
    location['pid']=process.pid
    save_json(state/'instance.json',location)
    try:
        for _ in range(150):
            if (state/'service.json').exists():break
            if process.poll() is not None:raise RuntimeError('Isolated frontend service exited; inspect its private service.log.')
            time.sleep(.1)
        else:raise RuntimeError('Isolated frontend service did not start.')
        print(json.dumps({'state':str(state),'port':port,'pid':process.pid}),flush=True)
        result=subprocess.run(['node',str(Path(__file__).with_suffix('.cjs'))],cwd=ROOT,env=env)
        if result.returncode:raise RuntimeError(f'Frontend checks failed; evidence remains in {state}')
    finally:
        if os.getenv('MINI_FRONTEND_KEEP_SERVICE')=='1':
            print(f'Kept this test-owned service for joint verification: PID {process.pid}',flush=True)
        else:
            process.terminate()
            process.wait(timeout=20)
            print('Stopped this test-owned service.',flush=True)


if __name__=='__main__':main()
