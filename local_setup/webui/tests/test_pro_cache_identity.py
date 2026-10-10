import json

import pytest

from local_setup.webui.bench import dataset_file,load_instance
from local_setup.webui.benchmarks import PRO_ID,REBENCH_ID,case_key,snapshot
from local_setup.webui.common import STATE,atomic_bytes,save_json
from local_setup.webui.library import data_cache_state,public_row,readiness
from local_setup.webui.patches import inspect_patch
from local_setup.webui.pro_data import ASSET_FILES,asset_dir,normalize_case
from local_setup.webui.tests.test_pro_data import INSTANCE,fixture_assets,row


def cached_case():
    public=row()
    paths={name:asset_dir(public)/name for name in ASSET_FILES}
    for name,raw in fixture_assets().items():atomic_bytes(paths[name],raw)
    record=normalize_case(public,paths)
    record.update(dataset=PRO_ID,case_key=case_key(PRO_ID,INSTANCE),benchmark_snapshot=snapshot(PRO_ID))
    save_json(dataset_file(PRO_ID,INSTANCE),[record])
    save_json(STATE/'catalog'/(PRO_ID+'.json'),{'dataset':PRO_ID,'benchmark_snapshot':snapshot(PRO_ID),
              'items':[public_row(record,0,PRO_ID)],'synced_at':'fixture','note':''})
    return record,paths


def test_pro_cache_readiness_tracks_missing_corrupt_and_repaired_assets():
    record,paths=cached_case()
    assert data_cache_state(PRO_ID,INSTANCE)['data_cached']
    assert data_cache_state(PRO_ID,INSTANCE)['data_cache_state']=='ready'
    original=paths['tests/test.sh'].read_bytes()
    paths['tests/test.sh'].unlink()
    missing=readiness(PRO_ID,INSTANCE,[],{'available':False,'images':{}})
    assert not missing['data_cached'] and missing['data_cache_state']=='incomplete'
    assert '重新缓存' in missing['data_cache_note']
    with pytest.raises(ValueError,match='尚未完整缓存'):load_instance(PRO_ID,INSTANCE)
    atomic_bytes(paths['tests/test.sh'],original+b'bad')
    assert not data_cache_state(PRO_ID,INSTANCE)['data_cached']
    atomic_bytes(paths['tests/test.sh'],original)
    assert data_cache_state(PRO_ID,INSTANCE)['data_cached']
    save_json(dataset_file(PRO_ID,INSTANCE),[record|{'instruction':'changed instruction'}])
    assert not data_cache_state(PRO_ID,INSTANCE)['data_cached']
    save_json(dataset_file(PRO_ID,INSTANCE),[record])
    assert data_cache_state(PRO_ID,INSTANCE)['data_cached']


def test_authorized_cache_repair_restores_official_derived_fields_without_redownloading():
    record,_=cached_case()
    path=dataset_file(PRO_ID,INSTANCE)
    damaged=record|{'instruction':'wrong prompt','pro_task':{'agent_timeout':1},'working_dir':'/wrong','image_name':'wrong-image'}
    save_json(path,[damaged])
    original=path.read_bytes()
    with pytest.raises(ValueError,match='官方资料不一致'):
        load_instance(PRO_ID,INSTANCE,download=False)
    assert path.read_bytes()==original
    assert load_instance(PRO_ID,INSTANCE,download=True)==record
    assert json.loads(path.read_text(encoding='utf-8'))==[record]
    assert data_cache_state(PRO_ID,INSTANCE)['data_cached']


@pytest.mark.parametrize(('content',),[('not-json',),(json.dumps([{'instance_id':INSTANCE}]),)])
def test_read_only_bad_cache_preserves_original_and_refuses_implicit_download(content):
    path=dataset_file(PRO_ID,INSTANCE)
    atomic_bytes(path,content.encode())
    with pytest.raises(ValueError,match='允许下载'):
        load_instance(PRO_ID,INSTANCE,download=False)
    assert path.read_bytes()==content.encode()
    assert data_cache_state(PRO_ID,INSTANCE)['data_cache_state']=='incomplete'


@pytest.mark.parametrize(('dataset','instance'),[(PRO_ID,INSTANCE),(REBENCH_ID,'review__snapshot-901')])
def test_patch_snapshot_rejects_evaluator_conflicts_and_preserves_partial_legacy_metadata(dataset,instance):
    path=STATE/('snapshot-'+dataset+'.predictions.jsonl')
    patch={'instance_id':instance,'dataset':dataset,'model_name_or_path':'offline',
           'model_patch':'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-before\n+after\n'}
    for changed in [{'evaluator_revision':'wrong-commit'},{'config':'wrong-config'},{'adapter_version':100}]:
        atomic_bytes(path,(json.dumps(patch|{'benchmark_snapshot':snapshot(dataset)|changed})+'\n').encode())
        result=inspect_patch(path,instance,dataset=dataset)
        assert not result['usable'] and '快照' in result['reason']
    atomic_bytes(path,(json.dumps(patch|{'benchmark_snapshot':{'source_revision':snapshot(dataset)['source_revision']}})+'\n').encode())
    assert inspect_patch(path,instance,dataset=dataset)['usable']
    atomic_bytes(path,(json.dumps(patch|{'benchmark_snapshot':snapshot(dataset)})+'\n').encode())
    assert not inspect_patch(path,instance,{'dataset':dataset,'benchmark_snapshot':{'evaluator_revision':'conflict'}},dataset=dataset)['usable']
    assert inspect_patch(path,instance,dataset=dataset)['usable']
