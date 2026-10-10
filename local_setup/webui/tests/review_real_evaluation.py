"""Explicit local regression using the user's already cached item and patch."""
import json
import time
import urllib.request
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
locator=json.loads((ROOT/'.local/webui/audit/fix-instance.json').read_text())
state=Path(locator['state'])
assert state.resolve().is_relative_to((ROOT/'.local/webui/fix-instances').resolve())
record=json.loads((state/'service.json').read_text())
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path,body=None):
    headers={'Cookie':'mini_session='+record['nonce']}
    if body is not None:headers['Content-Type']='application/json'
    request=urllib.request.Request(f"http://127.0.0.1:{record['port']}{path}",headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with opener.open(request,timeout=20) as response:return json.load(response)


assert api('/health')['identity']==record['identity']
assert api('/api/environment')['active_run'] is None
run=api('/api/runs',{'kind':'evaluate','dataset':'verified','instance_id':'django__django-10097',
                    'prediction_path':str(ROOT/'.local/django__django-10097-20260914-224547.predictions.jsonl'),
                    'settings_version':api('/api/config')['version'],'request_id':str(uuid.uuid4()),'allow_download':False})
print('Started isolated evaluation:',run['id'],flush=True)
phases=[]
while run['status'] not in {'completed','failed','cancelled','interrupted','cleanup_required'}:
    time.sleep(2)
    run=api('/api/runs/'+run['id'])
    phase=run.get('phase')
    if phase and (not phases or phase!=phases[-1]):
        phases.append(phase)
        print('Phase:',phase,flush=True)
result={'run_id':run['id'],'status':run['status'],'evaluation':{k:run.get('evaluation',{}).get(k) for k in ['category','verdict','resolved','completed','unresolved','errors','infrastructure','ambiguous']},
        'observed_phases':phases,'progress_available':bool(run.get('evaluation_progress')),'cleanup_pending':run.get('cleanup_pending',[]),
        'paid_model_calls':0,'download_allowed':False,'production_service_used':False}
events=state/'runs'/run['id']/'events.jsonl'
recorded=[json.loads(x) for x in events.read_text(encoding='utf-8').splitlines()]
result['recorded_stages']=[x['data']['stage'] for x in recorded if x['kind']=='evaluation_progress']
(ROOT/'collaboration/artifacts/2026-09-15/review-fixes/real-evaluation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False),flush=True)
assert run['status']=='completed' and run['evaluation']['category']=='passed'
assert result['recorded_stages'] and not result['cleanup_pending']
