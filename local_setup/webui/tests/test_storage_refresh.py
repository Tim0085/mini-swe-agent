import time
from types import SimpleNamespace

import pytest

from local_setup.webui.common import STATE, job_dir, new_id
from local_setup.webui.storage import SummaryCache, delete, preview


def test_background_storage_single_scan_cache_and_live_cleanup_validation():
    run_id=new_id()
    folder=job_dir(run_id)
    folder.mkdir(parents=True)
    (folder/'payload.txt').write_text('original',encoding='utf-8')
    data=STATE/'test-storage-inventory'
    data.mkdir(exist_ok=True)
    for number in range(300):
        (data/f'{number}.txt').write_text('fixture',encoding='utf-8')
    manager=SimpleNamespace(records={run_id:{'id':run_id,'status':'completed','title':'storage fixture'}},
                            active=None,protected_run_ids=set())
    cache=SummaryCache()
    start=time.monotonic()
    initial=cache.get(manager)
    assert time.monotonic()-start<1 and initial['scanning'] and not initial['scanned_at']
    first=cache.thread
    assert cache.get(manager)['scanning'] and cache.thread is first
    first.join(30)
    completed=cache.get(manager)
    assert not completed['scanning'] and completed['scanned_at'] and not completed['stale']
    assert next(x for x in completed['items'] if x['id']=='run:'+run_id)['bytes']==8
    assert cache.thread is first

    manager.active=run_id
    with pytest.raises(ValueError,match='受保护'):
        preview(['run:'+run_id],manager)
    assert next(x for x in cache.get(manager)['items'] if x['id']=='run:'+run_id)
    manager.active=None
    plan=preview(['run:'+run_id],manager)
    (folder/'payload.txt').write_text('changed after preview',encoding='utf-8')
    with pytest.raises(ValueError,match='文件在预览后发生变化'):
        delete(plan['token'],manager)
    assert folder.exists()

    assert cache.get(manager,refresh=True)['stale']
    assert cache.thread is not first
    cache.thread.join(30)
    assert next(x for x in cache.get(manager)['items'] if x['id']=='run:'+run_id)['bytes']==21
    cache.invalidate()
    assert cache.get(manager)['scanned_at'] is None
    cache.thread.join(30)
    assert not cache.get(manager)['scanning']


def test_failed_storage_scan_stops_until_explicit_retry():
    cache=SummaryCache()
    broken=SimpleNamespace(records={'invalid':None},active=None,protected_run_ids=set())
    cache.get(broken)
    cache.thread.join(30)
    failed=cache.get(broken)
    assert failed['error'] and not failed['scanning'] and failed['scanned_at'] is None
    failed_thread=cache.thread
    good=SimpleNamespace(records={},active=None,protected_run_ids=set())
    assert cache.get(good)['error'] and cache.thread is failed_thread
    assert cache.get(good,refresh=True)['scanning']
    cache.thread.join(30)
    assert cache.get(good)['error'] is None and cache.get(good)['scanned_at']
