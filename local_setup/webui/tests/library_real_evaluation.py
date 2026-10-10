"""Exercise the real evaluation batch using one cached image and existing patch."""
import json
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[3]
locator=json.loads((ROOT/'.local/webui/audit/fix-instance.json').read_text())
state=Path(locator['state']);service=json.loads((state/'service.json').read_text())
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(path,body=None):
    headers={'Cookie':'mini_session='+service['nonce']}
    if body is not None:headers['Content-Type']='application/json'
    request=urllib.request.Request(f"http://127.0.0.1:{service['port']}{path}",headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with opener.open(request,timeout=30) as response:return json.load(response)

assert state.resolve().is_relative_to((ROOT/'.local/webui/fix-instances').resolve())
assert not api('/api/environment')['active_run']
plan=api('/api/queues/preview',{'dataset':'verified','instance_ids':['django__django-10097'],'operation':'evaluate',
                              'predictions':{'django__django-10097':str(ROOT/'.local/django__django-10097-20260914-224547.predictions.jsonl')},
                              'allow_data_download':False,'allow_image_download':False,'settings_version':api('/api/config')['version']})
assert plan['ready'],plan['issues']
q=api('/api/queues',{'token':plan['token'],'request_id':uuid4().hex})
print('Real cached evaluation queue:',q['id'],flush=True)
previous=''
while q['status'] not in {'completed','cancelled','paused'}:
    time.sleep(2);q=api('/api/queues/'+q['id'])
    if q.get('current_run'):
        run=api('/api/runs/'+q['current_run'])
        phase=run.get('phase','')
        if phase!=previous:print(phase,flush=True);previous=phase
item=q['items'][0]
result={'queue_id':q['id'],'status':q['status'],'stages':[a['stage'] for a in item['attempts']],
        'evaluation':item.get('evaluation'),'estimated_model_cost':q['estimated_cost'],
        'model_calls':0,'image_downloads':0,'new_instance_downloads':0,'isolated':True,
        'child_runs':[a['run_id'] for a in item['attempts']]}
result['evaluation']={k:(result['evaluation'] or {}).get(k) for k in ['category','resolved','completed','errors','unresolved']}
result['cleanup_pending']=any(api('/api/runs/'+a['run_id']).get('cleanup_pending') for a in item['attempts'])
(ROOT/'collaboration/artifacts/2026-09-16/instance-library/real-evaluation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False),flush=True)
assert q['status']=='completed' and item['evaluation']['resolved']==1 and not result['cleanup_pending']
