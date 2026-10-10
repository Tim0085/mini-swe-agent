import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_setup.webui.bench import summarize_report
from local_setup.webui.changes import snapshot,finish
from local_setup.webui.common import STATE,save_json
from local_setup.webui.evaluation_progress import Progress
from local_setup.webui.patches import catalog,conversation_for
from local_setup.webui.storage import summary,inventory
from local_setup.webui.timeline import event_page


@pytest.mark.parametrize(('extra','expected'),[
    ({'resolved_instances':1},'passed'),({},'test_failed'),({'infra_failure_instances':1},'infrastructure'),
    ({'ambiguous_failure_instances':1},'ambiguous'),({'completed_instances':0,'error_instances':1},'incomplete'),
])
def test_verdict_preserves_original_counts_and_reasons(extra,expected):
    data={'completed_instances':1,'resolved_instances':0,'unresolved_instances':1,'error_instances':0,'failure_reasons':{'fixture':'missing_module'}} | extra
    result=summarize_report(data)
    assert result['category']==expected and result['resolved']==data['resolved_instances']
    assert result['unresolved']==data['unresolved_instances'] and result['failure_reasons']==data['failure_reasons']


@pytest.mark.parametrize(('use_git',),[(False,),(True,)])
def test_new_modified_deleted_text_preview_without_touching_git_index(tmp_path,use_git):
    project=tmp_path/'project';project.mkdir()
    if use_git:
        subprocess.run(['git','init','--quiet',str(project)],check=True)
    (project/'modified.py').write_text('before\n',encoding='utf-8')
    (project/'deleted.txt').write_text('deleted content',encoding='utf-8')
    output=tmp_path/'result';output.mkdir()
    save_json(output/'before-files.json',snapshot(project))
    (project/'modified.py').write_text('after\n',encoding='utf-8')
    (project/'deleted.txt').unlink()
    (project/'new.py').write_text('new content',encoding='utf-8')
    result=finish(project,output)
    assert {r['change'] for r in result['files']}=={'新增','删除','修改'}
    diff=(output/'changes.diff').read_text(encoding='utf-8')
    assert '+new content' in diff and '-deleted content' in diff and '-before' in diff and '+after' in diff
    assert not (project/'.git/index').exists()


def test_private_binary_large_files_do_not_leak_into_previews(tmp_path):
    output=tmp_path/'out';output.mkdir()
    project=tmp_path/'project';project.mkdir()
    save_json(output/'before-files.json',snapshot(project))
    (project/'example.txt').write_text('sk-offline-example-not-real',encoding='utf-8')
    (project/'binary.dat').write_bytes(b'\0bin')
    (project/'large.txt').write_text('x'*200001,encoding='utf-8')
    result=finish(project,output)
    assert len(result['files'])==3 and all('preview' not in r for r in result['files'])
    assert not (output/'changes.diff').exists()


def test_cross_model_duplicates_and_oversized_first_message_remain_distinct(tmp_path):
    instance='django__django-10097'
    patch='diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'
    traj=tmp_path/(instance+'.traj.json');pred=tmp_path/(instance+'.predictions.jsonl')
    traj.write_text(json.dumps({'info':{'exit_status':'Submitted','submission':patch,'config':{'model':{'model_name':'openai/a'}}},'messages':[{'role':'user','content':'x'*200001}]}),encoding='utf-8')
    pred.write_text(json.dumps({'instance_id':instance,'model_name_or_path':'openai/b','model_patch':patch})+'\n',encoding='utf-8')
    result=catalog(instance,[(traj,''),(pred,'')])
    assert len(result['items'])==2 and result['recommended'] is None
    assert not next(x for x in result['items'] if x['model']=='openai/b')['has_conversation']
    messages=conversation_for(traj,instance,[])
    assert messages['source']==traj.name and messages['truncated'] and len(messages['messages'])==1
    assert messages['messages'][0]['role']=='user' and len(messages['messages'][0]['content'])<5000


def test_timeline_page_cursor_partial_write_and_message_limits(tmp_path):
    path=tmp_path/'events.jsonl'
    events=[{'id':i,'kind':'message','data':{'role':'assistant','content':str(i)}} for i in range(1,161)]
    path.write_text('\n'.join(json.dumps(x) for x in events)+'\n{"id":161',encoding='utf-8')
    newest=event_page(path)
    older=event_page(path,newest['before'])
    assert len(newest['items'])==50 and newest['items'][0]['id']==111 and newest['last_event_id']==160
    assert older['items'][-1]['id']==110 and not set(x['id'] for x in newest['items'])&set(x['id'] for x in older['items'])
    assert newest['has_older']


def test_progress_reads_incremental_owned_log_and_does_not_repeat(tmp_path):
    log=tmp_path/'logs/run_evaluation/run/model/issue/run_instance.log';log.parent.mkdir(parents=True)
    log.write_text('Creating container for fixture\n',encoding='utf-8')
    events=[];watcher=Progress(tmp_path,lambda kind,data:events.append((kind,data)))
    watcher.scan();watcher.scan()
    assert len([x for x in events if x[0]=='log'])==1
    with log.open('a',encoding='utf-8') as handle:handle.write('Grading answer for fixture\n')
    watcher.scan()
    assert watcher.state['stage']=='汇总测试结果' and watcher.state['last_output_at']
    assert len([x for x in events if x[0]=='log'])==2


def test_storage_categories_cover_other_and_audit_files():
    STATE.mkdir(parents=True,exist_ok=True)
    (STATE/'other-audit-fixture.txt').write_text('123',encoding='utf-8')
    (STATE/'audit').mkdir(exist_ok=True)
    (STATE/'audit/storage-fixture.txt').write_text('12345',encoding='utf-8')
    result=summary(SimpleNamespace(records={},active=None))
    local=[x for x in result['categories'] if x['name']!='隔离配置目录备份']
    assert sum(x['bytes'] for x in local)==sum(x[1] for x in inventory(STATE))
    assert next(x for x in local if x['name']=='其他工作台资料')['bytes']>=3
