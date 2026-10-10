import csv
import io
import json

import pytest

from local_setup.webui.benchmarks import PRO_ID, case_key, snapshot
from local_setup.webui.common import STATE, read_json
from local_setup.webui.results import Results
from local_setup.webui.server import manager
from test_experiments import experiment, model_profiles, start
from test_library_queues import IDS, queue_client, wait_queue
from test_results import evaluation, run

INSTANCE = 'instance_audit__pro-'+'1'*40+'-v'+'2'*40


def partial(number=1, **changes):
    return run(number, dataset=PRO_ID, instance_id=INSTANCE, case_key=case_key(PRO_ID, INSTANCE),
               benchmark_snapshot=snapshot(PRO_ID), prediction='saved-prediction.jsonl',
               result={'exit_status':'InternalServerError','patch_capture':{'source':'container_git_diff','partial':True,
                       'captured':True,'reason':'InternalServerError'}}, **changes)


@pytest.mark.parametrize(('status','expected'), [('failed','solve_failed'),('cancelled','cancelled'),
    ('interrupted','interrupted'),('running','solving'),('cleaning','solving'),('completed','submitted')])
def test_partial_patch_does_not_hide_failure_cancellation_or_unfinished_cleanup(tmp_path,status,expected):
    solve=partial(status=status,billing={'unknown':True},error='Provider unavailable')
    index=Results([solve],run_root=tmp_path)
    item=index.for_run(solve['id'])
    assert item['status']==expected and item['patch_available'] and item['solve_status']==status
    assert item['error']=='Provider unavailable' and item['cost']['estimated'] is None
    assert index.listing()['summary']['errors']==int(status in {'failed','interrupted'})
    assert index.listing()['summary']['evaluated']==0
    assessed=evaluation(2,solve,True,dataset=PRO_ID,benchmark_snapshot=snapshot(PRO_ID),case_key=case_key(PRO_ID,INSTANCE))
    result=Results([solve,assessed],run_root=tmp_path).for_run(solve['id'])
    assert result['status']=='passed' and result['solve_status']==status
    assert result['evaluation_run_ids']==[assessed['id']] and result['patch_capture']['partial']


@pytest.mark.parametrize(('origin',), [('recorded',),('request_settings',),('trajectory',)])
def test_effective_budget_separates_history_groups_without_changing_profile_or_files(tmp_path,origin):
    records=[]
    for number,budget in [(1,3),(2,.2)]:
        row=run(number);folder=tmp_path/row['id'];folder.mkdir()
        limits={'step_limit':40,'cost_limit':budget,'timeout':120}
        if origin=='recorded':row['effective_limits']=limits
        elif origin=='request_settings':(folder/'request.json').write_text(json.dumps({'settings':limits}),encoding='utf-8')
        else:(folder/'trajectory.traj.json').write_text(json.dumps({'info':{'config':{'agent':limits,'environment':{'timeout':120}}}}),encoding='utf-8')
        row['profile_snapshot']['limits']={'step_limit':40,'cost_limit':3,'timeout':120}
        records.append(row)
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    index=Results(records,run_root=tmp_path);page=index.listing()
    assert len(page['groups'])==2
    for item in page['items']:
        assert item['conditions']['configured_limits']['cost_limit']==3
        assert item['conditions']['limits_source']==origin
        assert item['conditions']['effective_limits']==item['conditions']['limits']
    rows=list(csv.DictReader(io.StringIO(index.export_csv())))
    assert sorted(json.loads(row['conditions_json'])['limits']['cost_limit'] for row in rows)==[.2,3]
    assert all(row['profile_snapshot']['limits']['cost_limit']==3 for row in records)
    assert before=={p:p.read_bytes() for p in before}


def test_experiment_keeps_one_column_per_model_and_exposes_varied_budgets(tmp_path):
    a,b=run(1,attempt_id='a'),run(2,attempt_id='b',instance_id='astropy__astropy-13034')
    a['effective_limits']={'step_limit':40,'cost_limit':3,'timeout':120}
    b['effective_limits']={'step_limit':40,'cost_limit':.2,'timeout':120}
    queue={'id':'experiment-budget','kind':'experiment','dataset':'verified','profile_snapshots':{'one':a['profile_snapshot']},
           'items':[{'instance_id':a['instance_id'],'profile_id':'one','first_attempt_id':'a'},
                    {'instance_id':b['instance_id'],'profile_id':'one','first_attempt_id':'b'}]}
    result=Results([a,b,evaluation(3,a),evaluation(4,b)],queues=[queue],run_root=tmp_path).experiment(queue['id'])
    assert len(result['groups'])==1 and result['summary']['denominator']==2
    assert result['groups'][0]['conditions']['conditions_varied']
    assert result['groups'][0]['conditions']['limits_source']=='experiment_varying'
    assert {r['cells'][0]['conditions']['limits']['cost_limit'] for r in result['rows']}=={3,.2}


def test_old_missing_execution_metadata_stays_explicitly_unconfirmed(tmp_path):
    solve=run(1);folder=tmp_path/solve['id'];folder.mkdir()
    (folder/'trajectory.traj.json').write_text('{"info":null}',encoding='utf-8')
    item=Results([solve],run_root=tmp_path).for_run(solve['id'])
    assert item['conditions']['limits_source']=='profile_snapshot'
    assert item['conditions']['effective_limits']['cost_limit'] is None
    assert item['conditions']['configured_limits']['cost_limit']==3


def test_orphan_reevaluation_does_not_use_current_cli_settings_as_solve_conditions(tmp_path):
    first=run(1,'evaluate',profile_snapshot={},result={},evaluation={'category':'test_failed'})
    second=run(2,'evaluate',profile_snapshot={},result={},evaluation={'category':'passed'})
    for row,budget in [(first,3),(second,20)]:
        folder=tmp_path/row['id'];folder.mkdir()
        (folder/'request.json').write_text(json.dumps({'settings':{'step_limit':40,'cost_limit':budget,'timeout':120}}),encoding='utf-8')
    index=Results([first,second],run_root=tmp_path).listing()
    assert len(index['groups'])==1 and index['summary']['denominator']==1
    assert all(row['conditions']['limits']['cost_limit'] is None for row in index['items'])
    assert all(row['conditions']['effective_limits']['cost_limit'] is None for row in index['items'])


def test_dispatch_records_actual_remaining_budget_in_request_and_metadata(queue_client):
    client=queue_client
    _,profiles=model_profiles(client,1)
    plan=experiment(client,profiles,instance_ids=IDS[:2],diagnostics={x:'success' for x in IDS[:2]},total_budget=.06)
    queue=start(client,plan)
    stopped=wait_queue(client,queue['id'],lambda q:q['status']=='paused' and not q['current_run'])
    solves=[manager.get(a['run_id']) for item in stopped['items'] for a in item['attempts'] if a['stage']=='solve']
    assert len(solves)==2 and solves[0]['status']=='completed' and solves[1]['status']=='failed'
    assert solves[0]['effective_limits']['cost_limit']==pytest.approx(.06)
    assert solves[1]['effective_limits']['cost_limit']==pytest.approx(.02)
    for solve in solves:
        request=read_json(STATE/'runs'/solve['id']/'request.json')
        assert request['effective_limits']==solve['effective_limits']
        assert request['effective_limits']['cost_limit']==request['settings']['cost_limit']
        assert request['profile_snapshot']['limits']['cost_limit']==1
