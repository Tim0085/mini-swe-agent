"""Start an isolated UI diagnostic instance without production credentials."""
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


def setup():
    state=ROOT/'.local/webui/implementation-20260920'/('browser-'+uuid4().hex[:8])
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
    rows=[]
    for index in range(12):
        instance='audit__fixture-'+str(1000+index)
        row={'instance_id':instance,'repo':'audit/fixture','problem_statement':'本机隔离题目 '+str(index)+'：验证模型对比和自动评测。'}
        rows.append(public_row(row,index))
        save_json(state/'datasets/verified'/(instance+'.json'),[row])
    save_json(state/'catalog/verified.json',{'dataset':'verified','items':rows,'synced_at':'2026-09-20T00:00:00+00:00','note':'仅隔离浏览器验收'})
    # Read-only copies of official result summaries; no raw conversations or keys.
    count=0
    for path in (ROOT/'.local/webui/runs').glob('*/meta.json'):
        record=json.loads(path.read_text(encoding='utf-8'))
        if record.get('kind')!='evaluate' or not record.get('evaluation',{}).get('detail'):continue
        record={key:record[key] for key in ('id','title','kind','status','created','updated','finished','model','instance_id','dataset','evaluation') if key in record}
        record['evaluation'].pop('report_path',None)
        save_json(state/'runs'/record['id']/'meta.json',record)
        count+=1
    from local_setup.webui.profiles import save_connection,save_profile,freeze_profile
    connection=save_connection({'name':'隔离验收连接','api_base':'https://example.invalid/v1'})
    for model_index in range(2):
        profile=save_profile({'name':'离线演示 '+chr(65+model_index),'connection_id':connection['id'],
            'model_name':'fixture-'+str(model_index),'pricing':{'mode':'free'}})
        snapshot=freeze_profile(profile['id'])
        for case_index in range(2):
            solve_id=f'20260920-120000-{100+model_index*10+case_index:08x}'
            eval_id=f'20260920-120000-{200+model_index*10+case_index:08x}'
            instance='audit__fixture-'+str(1000+case_index)
            resolved=model_index==0 or case_index==0
            solve={'id':solve_id,'kind':'swebench','title':'隔离可视化样本','status':'completed','created':'2026-09-20T12:00:00+00:00',
                'finished':'2026-09-20T12:00:10+00:00','updated':'2026-09-20T12:00:10+00:00','model':snapshot['model_name'],
                'dataset':'lite','instance_id':instance,'profile_snapshot':snapshot,'attempt_id':solve_id,'solve_run_id':solve_id,
                'patch_hash':'browser-fixture-'+str(model_index)+'-'+str(case_index),'stats':{'calls':2,'cost':0},'result':{'exit_status':'Submitted','submission':'isolated fixture'}}
            evaluation=solve | {'id':eval_id,'kind':'evaluate','solve_run_id':solve_id,'evaluation_run_id':eval_id,
                'evaluation':{'resolved':int(resolved),'completed':1,'errors':0,'category':'passed' if resolved else 'test_failed',
                'detail':{instance:{'tests_status':{'FAIL_TO_PASS':{'success':['fix'] if resolved else [],'failure':[] if resolved else ['fix']},
                                                'PASS_TO_PASS':{'success':['regression'],'failure':[]}}}}}}
            save_json(state/'runs'/solve_id/'meta.json',solve)
            save_json(state/'runs'/eval_id/'meta.json',evaluation)
    log=(state/'service.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'local_setup/webui/launch.py'),'--serve'],
        cwd=ROOT,env=os.environ.copy(),stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    log.close()
    for _ in range(100):
        if (state/'service.json').exists():break
        if process.poll() is not None:raise RuntimeError('Isolated service exited; inspect private service.log')
        time.sleep(.1)
    location={'state':str(state),'config_root':str(config),'port':port,'pid':process.pid,'copied_reports':count}
    save_json(ROOT/'.local/webui/implementation-20260920/browser-instance.json',location)
    print(json.dumps({k:v for k,v in location.items() if k!='state'}))


if __name__=='__main__':setup()
