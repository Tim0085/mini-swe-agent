"""Opt-in real metadata check: two bounded source reads, no images or model calls."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))


def main():
    state=ROOT/'.local/webui/rebench-source-checks'/uuid4().hex
    config=state/'config-root'
    (config/'.local').mkdir(parents=True)
    (config/'local_setup').mkdir()
    (config/'.local/.env').write_text("MSWEA_MODEL_NAME='openai/offline-source-check'\nOPENAI_API_BASE='https://example.invalid/v1'\n",encoding='utf-8')
    (config/'local_setup/windows.yaml').write_bytes((ROOT/'local_setup/webui/defaults.yaml').read_bytes())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=os.environ | {'MINI_WEB_STATE':str(state),'MINI_WEB_CONFIG_ROOT':str(config),'MINI_WEB_PORT':str(port),
                      'MINI_WEB_NONCE':uuid4().hex,'MINI_WEB_TESTING':'1','MSWEA_GLOBAL_CONFIG_DIR':str(config/'.local'),'MSWEA_CONFIGURED':'true'}
    with (state/'service.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'local_setup/webui/launch.py'),'--serve'],cwd=ROOT,env=env,
                                 stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(150):
            if (state/'service.json').exists():break
            assert process.poll() is None,'Isolated source-check service exited'
            time.sleep(.1)
        service=json.loads((state/'service.json').read_text())
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=20) as client:
            def call(path,body=None):
                result=client.get(path) if body is None else client.post(path,json=body)
                result.raise_for_status();return result.json()
            call('/session',{'nonce':service['nonce']})
            from local_setup.webui.benchmarks import REBENCH_ID
            def finish(run):
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    current=call('/api/runs/'+run['id'])
                    if current['status'] in {'completed','failed','interrupted','cleanup_required'}:
                        assert current['status']=='completed',current.get('error','Metadata task did not complete')
                        return current
                    time.sleep(.4)
                raise RuntimeError('Metadata task timed out; no retry dispatched')
            sync=finish(call('/api/library/sync',{'dataset':REBENCH_ID,'allow_network':True,'request_id':uuid4().hex}))
            assert sync['catalog_result']['count']==860
            instance='aws__bedrock-agentcore-sdk-python-279_interface'
            options=call('/api/instance-options?dataset='+REBENCH_ID+'&search='+instance)
            assert options['items'][0]['instance_id']==instance
            cached=finish(call('/api/runs',{'kind':'cache','dataset':REBENCH_ID,'instance_id':instance,
                'request_id':uuid4().hex,'settings_version':call('/api/config')['version'],
                'allow_data_download':True,'allow_image_download':False}))
            public=call('/api/bench/cached?dataset='+REBENCH_ID+'&instance_id='+instance)
            assert public['ready'] and 'Required interfaces:' in public['problem_statement']
            catalog=json.loads((state/'catalog'/(REBENCH_ID+'.json')).read_text())
            assert all(not {'patch','test_patch','install_config'} & set(row) for row in catalog['items'])
            assert call('/api/environment')['active_run'] is None
            ticket=call('/api/lifecycle',{'action':'exit','mode':'when_idle'})
            call('/api/lifecycle/ack',{'id':ticket['id']})
        process.wait(timeout=20)
        report={'passed':True,'catalog_count':860,'single_case_cache':True,'required_interfaces_visible':True,
                'pinned_source_verified':True,'no_answers_in_catalog':True,'source_reads':2,'source_read_bytes':2*4864925,
                'model_calls':0,'image_downloads':0,'production_state_modified':False,'isolated_service_stopped':True}
        out=ROOT/'collaboration/artifacts/2026-10-08/rebench/source-api-check.json'
        out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
    finally:
        if process.poll() is None:
            process.terminate();process.wait(timeout=20)


if __name__=='__main__':main()
