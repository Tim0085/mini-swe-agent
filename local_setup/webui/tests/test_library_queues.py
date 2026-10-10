import json
import os
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from local_setup.webui.common import STATE,save_json,read_json
from local_setup.webui.library import public_row,directory
from local_setup.webui.server import app,manager,queues,NONCE

IDS=['audit__fixture-'+str(1000+i) for i in range(30)]


@pytest.fixture
def queue_client():
    previous=os.environ.get('MINI_WEB_TESTING')
    os.environ['MINI_WEB_TESTING']='1'
    rows=[]
    for i,instance in enumerate(IDS):
        raw={'instance_id':instance,'repo':'audit/fixture','problem_statement':f'Offline library fixture number {i}','patch':'must not be in public directory'}
        rows.append(public_row(raw,i))
        save_json(STATE/'datasets/verified'/(instance+'.json'),[raw])
    save_json(STATE/'catalog/verified.json',{'dataset':'verified','items':rows,'synced_at':'2026-09-16T00:00:00+00:00','note':'offline fixtures'})
    with TestClient(app,base_url='http://127.0.0.1:8765') as client:
        assert client.post('/session',json={'nonce':NONCE}).status_code==200
        yield client
        q=queues.unfinished()
        if q:client.post('/api/queues/'+q['id']+'/control',json={'action':'finish'})
        deadline=time.monotonic()+20
        while manager.active and time.monotonic()<deadline:time.sleep(.1)
        assert not manager.active
    if previous is None:os.environ.pop('MINI_WEB_TESTING',None)
    else:os.environ['MINI_WEB_TESTING']=previous


def preview(client,ids=None,**changes):
    ids=ids or IDS[:3]
    body={'dataset':'verified','instance_ids':ids,'operation':'solve','diagnostics':{x:'success' for x in ids},
          'settings_version':client.get('/api/config').json()['version'],'total_budget':1,'auto_approve':True,'auto_evaluate':True} | changes
    response=client.post('/api/queues/preview',json=body)
    assert response.status_code==200,response.text
    return response.json()


def begin(client,plan):
    assert plan['ready'],plan['issues']
    body={'token':plan['token'],'request_id':uuid4().hex}
    response=client.post('/api/queues',json=body)
    assert response.status_code==200,response.text
    return response.json(),body


def wait_queue(client,queue_id,predicate,timeout=40):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        q=client.get('/api/queues/'+queue_id).json()
        if predicate(q):return q
        time.sleep(.1)
    pytest.fail('Queue timeout: '+json.dumps(q,ensure_ascii=False))


def test_library_pagination_search_and_no_reference_patch(queue_client):
    first=queue_client.get('/api/library',params={'dataset':'verified'}).json()
    second=queue_client.get('/api/library',params={'dataset':'verified','offset':8}).json()
    assert first['total']==30 and len(first['items'])==8 and len(second['items'])==8 and first['limit']==8
    assert not set(x['instance_id'] for x in first['items'])&set(x['instance_id'] for x in second['items'])
    filtered=queue_client.get('/api/library',params={'dataset':'verified','search':'number 29'}).json()
    assert filtered['total']==1
    for limit in (8,16,25,50):
        page=queue_client.get('/api/library',params={'dataset':'verified','limit':limit}).json()
        assert page['limit']==limit and len(page['items'])==min(30,limit)
    assert queue_client.get('/api/library',params={'limit':100000}).status_code==400
    last=queue_client.get('/api/library',params={'offset':1000,'limit':8}).json()
    assert last['offset']==24 and len(last['items'])==6
    assert all('patch' not in row and 'test_patch' not in row for row in directory('verified')['items'])
    queue_client.cookies.clear()
    assert queue_client.get('/api/library').status_code==401


def test_instance_options_search_scoping_and_public_fields(queue_client):
    first=queue_client.get('/api/instance-options').json()
    assert first['synced'] and first['total']==30 and len(first['items'])==8
    assert all(set(row)=={'instance_id','repo','title'} for row in first['items'])
    match=queue_client.get('/api/instance-options',params={'search':'AUDIT/fixture number 29'}).json()
    assert match['total']==1 and match['items'][0]['instance_id']==IDS[29]
    assert queue_client.get('/api/instance-options',params={'search':'no-such-issue'}).json()['total']==0
    assert queue_client.get('/api/instance-options',params={'dataset':'lite'}).json()=={'items':[],'total':0,'synced':False}
    queue_client.cookies.clear()
    assert queue_client.get('/api/instance-options').status_code==401


def test_three_items_serial_fresh_auto_evaluation_and_duplicate_commit(queue_client):
    q,body=begin(queue_client,preview(queue_client,diagnostics={IDS[0]:'success',IDS[1]:'test_failed',IDS[2]:'success'}))
    assert queue_client.post('/api/queues',json=body).json()['id']==q['id']
    done=wait_queue(queue_client,q['id'],lambda x:x['status']=='completed')
    assert [x['evaluation']['category'] for x in done['items']]==['passed','test_failed','passed']
    assert done['estimated_cost']==pytest.approx(.12)
    attempts=[a for item in done['items'] for a in item['attempts']]
    assert len(attempts)==9 and len({a['request_id'] for a in attempts})==9
    for item in done['items']:
        solve=next(a for a in item['attempts'] if a['stage']=='solve')
        evaluated=next(a for a in item['attempts'] if a['stage']=='evaluate')
        request=read_json(STATE/'runs'/evaluated['run_id']/'request.json')
        assert solve['run_id'] in request['prediction_path']
    times=[manager.get(a['run_id']) for a in attempts]
    assert all(a['finished']<=b['created'] for a,b in zip(times,times[1:]))


def test_failure_pauses_and_explicit_skip_resumes(queue_client):
    q,_=begin(queue_client,preview(queue_client,diagnostics={IDS[0]:'failure',IDS[1]:'success',IDS[2]:'success'}))
    stopped=wait_queue(queue_client,q['id'],lambda x:x['status']=='paused')
    assert stopped['items'][0]['status']=='failed' and not stopped['items'][1]['attempts']
    assert queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'resume'}).status_code==400
    assert queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'skip','index':0}).status_code==200
    queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'resume'})
    done=wait_queue(queue_client,q['id'],lambda x:x['status']=='completed')
    assert done['items'][0]['status']=='skipped' and done['items'][2]['status']=='completed'


def test_budget_stops_subsequent_calls_and_unknown_cost_pauses(queue_client):
    q,_=begin(queue_client,preview(queue_client,total_budget=.01))
    stopped=wait_queue(queue_client,q['id'],lambda x:x['status']=='paused')
    assert stopped['estimated_cost']==pytest.approx(.02)
    solved=[manager.get(a['run_id']) for a in stopped['items'][0]['attempts'] if a['stage']=='solve']
    assert solved[0]['stats']['calls']==1 and not stopped['items'][1]['attempts']
    queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'finish'})
    q,_=begin(queue_client,preview(queue_client,diagnostics={x:'unknown_cost' for x in IDS[:3]}))
    stopped=wait_queue(queue_client,q['id'],lambda x:x['status']=='paused')
    assert stopped['billing_unknown'] and not stopped['items'][1]['attempts']


def test_pause_finishes_current_item_then_configuration_requires_confirmation(queue_client):
    q,_=begin(queue_client,preview(queue_client,auto_approve=False))
    active=wait_queue(queue_client,q['id'],lambda x:bool(x.get('current_run')) and manager.get(x['current_run'])['status']=='awaiting_confirmation')
    queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'pause'})
    for _ in range(2):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            meta=manager.get(active['current_run'])
            if meta.get('pending'):break
            time.sleep(.1)
        queue_client.post('/api/runs/'+meta['id']+'/approve',json={'ticket':meta['pending']['ticket'],'approve':True})
        time.sleep(.2)
    stopped=wait_queue(queue_client,q['id'],lambda x:x['status']=='paused' and not x.get('current_run'))
    assert stopped['items'][0]['evaluation']['category']=='passed' and not stopped['items'][1]['attempts']
    cfg=queue_client.get('/api/config').json()
    values={k:cfg[k] for k in ['model_name','api_base','model_class','step_limit','cost_limit','timeout']};values['timeout']+=1
    saved=queue_client.put('/api/config',json={'version':cfg['version'],'settings':values}).json()
    assert queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'resume'}).status_code==400
    assert queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'resume','accepted_settings_version':saved['version']}).status_code==200


def test_missing_authorization_diagnostic_gate_and_previews_expire(queue_client):
    missing=IDS[-1];path=STATE/'datasets/verified'/(missing+'.json');path.unlink()
    result=preview(queue_client,[missing],operation='cache',auto_approve=False,auto_evaluate=False)
    assert not result['ready'] and '未授权下载题目' in result['issues'][0]
    os.environ.pop('MINI_WEB_TESTING',None)
    cfg=queue_client.get('/api/config').json()
    denied=queue_client.post('/api/queues/preview',json={'instance_ids':[IDS[0]],'operation':'cache','settings_version':cfg['version'],'diagnostics':{IDS[0]:'success'}})
    assert denied.status_code==400
    os.environ['MINI_WEB_TESTING']='1'
    expired=queue_client.post('/api/queues',json={'token':'expired','request_id':uuid4().hex})
    assert expired.status_code==400


def test_cancel_pending_item_and_protect_unfinished_queue_artifacts(queue_client):
    q,_=begin(queue_client,preview(queue_client,auto_approve=False))
    active=wait_queue(queue_client,q['id'],lambda x:bool(x.get('current_run')) and manager.get(x['current_run'])['status']=='awaiting_confirmation')
    first_download=active['items'][0]['attempts'][0]['run_id']
    assert queue_client.post('/api/storage/preview',json={'ids':['run:'+first_download]}).status_code==400
    assert queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'cancel_item','index':2}).json()['items'][2]['status']=='cancelled'
    assert queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'cancel_item','index':0}).status_code==400
    queue_client.post('/api/queues/'+q['id']+'/control',json={'action':'stop'})
    paused=wait_queue(queue_client,q['id'],lambda x:x['status']=='paused' and not x.get('current_run'))
    assert not paused['items'][1]['attempts']


def test_partial_diagnostic_map_is_rejected_before_any_real_request(queue_client):
    body={'dataset':'verified','instance_ids':IDS[:2],'operation':'solve','diagnostics':{IDS[0]:'success'},
          'settings_version':queue_client.get('/api/config').json()['version']}
    response=queue_client.post('/api/queues/preview',json=body)
    assert response.status_code==400 and not manager.active


def test_resource_authorization_space_and_unknown_pricing_boundaries():
    from local_setup.webui.library import require_metadata_space
    from local_setup.webui.queues import BatchRequest,resource_issues,price_available
    cache=BatchRequest(instance_ids=[IDS[0]],operation='cache',settings_version='fixture',allow_data_download=True)
    assert resource_issues(cache,{'data_cached':False,'environment':'unknown'})==[]
    download=cache.model_copy(update={'operation':'download','allow_image_download':True})
    assert resource_issues(download,{'data_cached':True,'environment':'unknown'})==['Docker 无法检查']
    denied=download.model_copy(update={'allow_image_download':False,'allow_data_download':False})
    assert len(resource_issues(denied,{'data_cached':False,'environment':'missing'}))==2
    with pytest.raises(ValueError,match='空间不足'):require_metadata_space(10*1024**2-1)
    require_metadata_space(10*1024**2)
    assert not price_available('openai/not-a-real-model-offline-fixture')
