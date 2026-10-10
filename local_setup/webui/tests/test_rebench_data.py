import json

import pytest

from local_setup.webui.bench import dataset_file,load_instance,valid_instance,image_name
from local_setup.webui.benchmarks import REBENCH_ID,case_key,snapshot
from local_setup.webui.common import STATE,save_json
from local_setup.webui.library import public_row
from local_setup.webui.rebench_data import PUBLIC_COLUMNS,read_source,solver_input


def test_corrupt_source_and_unversioned_cache_are_rejected_without_overwrite():
    with pytest.raises(ValueError,match='校验'):
        read_source(b'not-the-pinned-official-source')
    instance='audit__source-880_interface'
    path=dataset_file(REBENCH_ID,instance)
    original=[{'instance_id':instance,'problem_statement':'fixture','patch':'not for solver'}]
    save_json(path,original)
    with pytest.raises(ValueError,match='未冻结'):
        load_instance(REBENCH_ID,instance)
    assert json.loads(path.read_text())==original
    assert path.is_relative_to(STATE/'datasets'/REBENCH_ID)
    with pytest.raises(ValueError):valid_instance(instance,'verified')
    with pytest.raises(ValueError):dataset_file(REBENCH_ID,'../../bad')
    with pytest.raises(ValueError):dataset_file('rebench-unregistered','audit__source-880')
    missing=dataset_file(REBENCH_ID,'audit__source-881')
    with pytest.raises(ValueError,match='尚未缓存'):
        load_instance(REBENCH_ID,'audit__source-881',False)
    assert not missing.exists()


def test_catalog_and_solver_exclude_answer_fields_and_preserve_required_interface():
    instance='audit__source-882_interface'
    row={'instance_id':instance,'repo':'audit/source','problem_statement':'Repair a method.',
         'interface':'def required_method(x): ...','base_commit':'a'*40,'patch':'hidden-answer-sentinel',
         'test_patch':'hidden-test-sentinel','install_config':{'install':'never show this'},
         'image_name':'swerebench/sweb.eval.x86_64.audit_1776_source-882:latest',
         'dataset':REBENCH_ID,'benchmark_snapshot':snapshot(REBENCH_ID),'case_key':case_key(REBENCH_ID,instance)}
    public=public_row(row,0,REBENCH_ID)
    assert 'required_method' in public['problem_statement'] and 'required_method' in solver_input(row)
    assert not {'patch','test_patch','install_config'} & set(public)
    assert not {'patch','test_patch','install_config'} & set(PUBLIC_COLUMNS)
    assert 'hidden-answer-sentinel' not in json.dumps(public)+solver_input(row)
    save_json(dataset_file(REBENCH_ID,instance),[row])
    assert load_instance(REBENCH_ID,instance)['benchmark_snapshot']==snapshot(REBENCH_ID)
    assert image_name(row,REBENCH_ID)==row['image_name']
    with pytest.raises(ValueError,match='来源'):
        image_name(row|{'image_name':'swebench/other:latest'},REBENCH_ID)
