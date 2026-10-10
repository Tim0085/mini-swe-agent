"""Run browser diagnostics against an already isolated backend and local HTTP fixture."""
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
location = json.loads((ROOT/'.local/webui/implementation-20260920/browser-instance.json').read_text())
state = Path(location['state']).resolve()
assert state.is_relative_to(ROOT/'.local/webui/implementation-20260920') and state.name.startswith('browser-')
os.environ.update(MINI_WEB_STATE=str(state), MINI_WEB_CONFIG_ROOT=location['config_root'], MINI_WEB_TESTING='1', MSWEA_GLOBAL_CONFIG_DIR=str(Path(location['config_root'])/'.local'), MSWEA_CONFIGURED='true')
from local_setup.webui.profiles import freeze_profile
from local_setup.webui.common import save_json
from local_setup.webui.tests.test_probe_recovery import diagnostic_service

request_id = uuid4().hex
save_json(state/'profiles/tests'/(request_id+'.json'), {'profile_id':'legacy', 'request_id':request_id,
    'status':'completed','created':'2026-09-20T23:00:00+00:00',
    'result':{'status':'passed','fingerprint':freeze_profile('legacy')['fingerprint'],'checked_at':'2026-09-20T23:00:00+00:00'}})
with diagnostic_service() as (base, service):
    result = subprocess.run(['node', str(Path(__file__).with_suffix('.cjs')), base], cwd=ROOT)
    if result.returncode: sys.exit(result.returncode)
    assert len(service['calls']) == 3, 'Expected one failed request and one two-request retry'
    assert all(body['model']=='grok-fixture' for body in service['calls'])
    sys.exit(result.returncode)
