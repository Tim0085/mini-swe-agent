"""One public catalog sync and one complete new instance, no images/models."""
import json
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[3]
locator=json.loads((ROOT/'.local/webui/audit/fix-instance.json').read_text())
state=Path(locator['state']);record=json.loads((state/'service.json').read_text())
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(path,body=None):
    headers={'Cookie':'mini_session='+record['nonce']}
    if body is not None:headers['Content-Type']='application/json'
    request=urllib.request.Request(f"http://127.0.0.1:{record['port']}{path}",headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with opener.open(request,timeout=30) as response:return json.load(response)

def wait(run):
    previous=''
    while run['status'] not in {'completed','failed','cancelled','interrupted','cleanup_required'}:
        time.sleep(.5);run=api('/api/runs/'+run['id'])
        if run.get('phase')!=previous:
            previous=run.get('phase');print(previous,flush=True)
    assert run['status']=='completed',run.get('error')
    return run

assert api('/health')['identity']==record['identity'] and api('/api/environment')['active_run'] is None
sync=wait(api('/api/library/sync',{'dataset':'verified','request_id':uuid4().hex,'allow_network':True}))
catalog=json.loads((state/'catalog/verified.json').read_text(encoding='utf-8'))
assert all(not set(row)&{'patch','test_patch','eval_script','FAIL_TO_PASS'} for row in catalog['items'])
candidate=next(row for row in catalog['items'] if not (state/'datasets/verified'/(row['instance_id']+'.json')).exists() and not (ROOT/'.local/swebench-eval-data'/(row['instance_id']+'-v5.json')).exists())
instance=candidate['instance_id'];cfg=api('/api/config')
run=wait(api('/api/runs',{'kind':'cache','dataset':'verified','instance_id':instance,'allow_data_download':True,'allow_image_download':False,'request_id':uuid4().hex,'settings_version':cfg['version']}))
path=state/'datasets/verified'/(instance+'.json');full=json.loads(path.read_text(encoding='utf-8'))
assert full[0]['instance_id']==instance and full[0]['problem_statement']
result={'catalog_count':len(catalog['items']),'catalog_bytes':(state/'catalog/verified.json').stat().st_size,'reference_patch_excluded':True,'new_instance':instance,'new_instance_bytes':path.stat().st_size,'image_downloads':0,'model_calls':0,'sync_run':sync['id'],'cache_run':run['id'],'isolated':True}
out=ROOT/'collaboration/artifacts/2026-09-16/instance-library';out.mkdir(parents=True,exist_ok=True)
(out/'network-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False),flush=True)
