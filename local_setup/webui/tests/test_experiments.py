import time
from uuid import uuid4

import pytest

from local_setup.webui.common import STATE,read_json
from local_setup.webui.server import manager,queues
from test_library_queues import IDS,queue_client,wait_queue


def model_profiles(client,count=3):
    response=client.post('/api/connections',json={'name':'Offline experiment '+uuid4().hex[:8],'api_base':'https://example.invalid/v1'})
    assert response.status_code==200,response.text
    connection=response.json()
    profiles=[]
    for index in range(count):
        response=client.post('/api/profiles',json={'name':'Model '+str(index),'connection_id':connection['id'],
            'model_name':'fixture-'+str(index),'pricing':{'mode':'free'},'limits':{'step_limit':8,'cost_limit':1,'timeout':30}})
        assert response.status_code==200,response.text
        profiles.append(response.json())
    return connection,profiles


def experiment(client,profiles,**changes):
    body={'name':'Offline same-task experiment','dataset':'verified','instance_ids':IDS[:1],
          'profile_ids':[p['id'] for p in profiles],'settings_version':client.get('/api/config').json()['version'],
          'step_limit':8,'cost_limit':1,'timeout':30,'total_budget':2,'auto_evaluate':True,'auto_approve':True,
          'diagnostics':{IDS[0]:'success'}} | changes
    plan=client.post('/api/experiments/preview',json=body)
    assert plan.status_code==200,plan.text
    return plan.json()


def start(client,plan):
    assert plan['ready'],plan
    body={'token':plan['token'],'request_id':uuid4().hex}
    result=client.post('/api/experiments',json=body)
    assert result.status_code==200,result.text
    assert client.post('/api/experiments',json=body).json()['id']==result.json()['id']
    return result.json()


def test_three_models_sequence_frozen_limits_and_fresh_patch(queue_client):
    _,profiles=model_profiles(queue_client)
    plan=experiment(queue_client,profiles)
    assert [x['profile_id'] for x in plan['items']]==[p['id'] for p in profiles]
    q=start(queue_client,plan)
    done=wait_queue(queue_client,q['id'],lambda q:q['status']=='completed')
    assert len(done['items'])==3 and done['estimated_cost']==pytest.approx(.12)
    assert all(s['limits']['step_limit']==8 for s in done['profile_snapshots'].values())
    assert len({x['first_attempt_id'] for x in done['items']})==3
    records=[]
    for item in done['items']:
        solve=next(a for a in item['attempts'] if a['stage']=='solve')
        evaluation=next(a for a in item['attempts'] if a['stage']=='evaluate')
        meta=manager.get(evaluation['run_id'])
        assert meta['solve_run_id']==solve['run_id'] and meta['attempt_id']==item['first_attempt_id']
        assert meta['profile_snapshot']['profile_id']==item['profile_id']
        records.extend(manager.get(a['run_id']) for a in item['attempts'])
    assert all(a['finished']<=b['created'] for a,b in zip(records,records[1:]))
    exported=queue_client.get('/api/results/export').text
    assert all(x['id'] not in exported for x in records), 'Diagnostic results must not enter production statistics'


def test_failure_retry_preserves_first_round_and_restart_never_dispatches(queue_client):
    _,profiles=model_profiles(queue_client,2)
    q=start(queue_client,experiment(queue_client,profiles,auto_approve=False))
    wait_queue(queue_client,q['id'],lambda q:bool(q.get('current_task')) and q['current_task']['status']=='awaiting_confirmation')
    queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'stop'})
    stopped=wait_queue(queue_client,q['id'],lambda q:q['status']=='paused' and not q.get('current_run'))
    original=stopped['items'][0]['first_attempt_id']
    failed=stopped['items'][0]['attempts'][-1]['run_id']
    assert not stopped['items'][1]['attempts']
    result=queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'retry','index':0})
    assert result.status_code==200,result.text
    assert result.json()['items'][0]['attempt_id']!=original
    assert result.json()['items'][0]['first_attempt_id']==original
    with manager.lock:
        queues.records[q['id']]['auto_approve']=True
        queues.store(queues.records[q['id']])
        queues.recover()
        queues.tick()
    recovered=queue_client.get('/api/experiments/'+q['id']).json()
    assert recovered['status']=='paused' and not recovered['current_run']
    assert queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'resume'}).status_code==200
    done=wait_queue(queue_client,q['id'],lambda q:q['status']=='completed')
    assert manager.get(failed)['status']=='cancelled'
    assert done['items'][0]['attempt_number']==2 and done['items'][0]['first_attempt_id']==original


def test_used_profile_change_blocks_resume_unrelated_profile_does_not(queue_client):
    _,profiles=model_profiles(queue_client,2)
    q=start(queue_client,experiment(queue_client,profiles[:1],auto_approve=False))
    active=wait_queue(queue_client,q['id'],lambda q:q.get('current_task',{}).get('status')=='awaiting_confirmation' if q.get('current_task') else False)
    other=profiles[1] | {'name':'Unrelated edit'}
    assert queue_client.put('/api/profiles/'+other['id'],json=other).status_code==200
    assert not queue_client.get('/api/experiments/'+q['id']).json()['configuration_changed']
    queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'stop'})
    wait_queue(queue_client,q['id'],lambda q:not q.get('current_run'))
    changed=profiles[0] | {'parameters':{'temperature':.2}}
    assert queue_client.put('/api/profiles/'+changed['id'],json=changed).status_code==200
    queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'retry','index':0})
    denied=queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'resume'})
    assert denied.status_code==400 and '新实验' in denied.text


def test_experiment_limits_authorization_and_legacy_unchanged(queue_client):
    from local_setup.webui.common import CONFIG_ROOT
    files=[CONFIG_ROOT/'.local/.env',CONFIG_ROOT/'local_setup/windows.yaml']
    before=[f.read_bytes() for f in files]
    _,profiles=model_profiles(queue_client,2)
    assert queue_client.put('/api/profiles/default',json={'profile_id':profiles[0]['id']}).status_code==200
    assert [f.read_bytes() for f in files]==before
    duplicate=queue_client.post('/api/experiments/preview',json={'instance_ids':IDS[:1],
        'profile_ids':[profiles[0]['id']]*2,'settings_version':'unused'})
    assert duplicate.status_code==400
    oversized=queue_client.post('/api/experiments/preview',json={'instance_ids':IDS[:26],
        'profile_ids':[p['id'] for p in profiles],'settings_version':'unused'})
    assert oversized.status_code==400
    (STATE/'datasets/verified'/(IDS[-1]+'.json')).unlink()
    missing=experiment(queue_client,profiles,instance_ids=[IDS[-1]],operation='solve',
                       diagnostics={IDS[-1]:'success'})
    assert not missing['ready'] and any('未授权下载' in x for x in missing['issues'])


def test_completed_experiment_explicit_resolve_retry_keeps_first_round_and_budget(queue_client):
    _,profiles=model_profiles(queue_client,2)
    q=start(queue_client,experiment(queue_client,profiles,diagnostics={IDS[0]:'test_failed'}))
    completed=wait_queue(queue_client,q['id'],lambda q:q['status']=='completed')
    first=completed['items'][0]['first_attempt_id']
    first_runs=[a['run_id'] for a in completed['items'][0]['attempts']]
    response=queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'retry','index':0})
    assert response.status_code==200,response.text
    pending=response.json()
    assert pending['status']=='paused' and not pending['current_run']
    assert pending['items'][0]['first_attempt_id']==first and pending['items'][0]['attempt_id']!=first
    assert pending['items'][0]['attempt_number']==2 and pending['items'][0]['stage_index']==1
    assert pending['total_budget']==completed['total_budget'] and pending['estimated_cost']==completed['estimated_cost']
    assert queue_client.post('/api/experiments/'+q['id']+'/control',json={'action':'resume'}).status_code==200
    done=wait_queue(queue_client,q['id'],lambda q:q['status']=='completed')
    assert len(done['items'][0]['attempts'])==5 and len(done['items'][1]['attempts'])==3
    assert all(manager.get(run)['status']=='completed' for run in first_runs)
    assert done['estimated_cost']==pytest.approx(.12)
