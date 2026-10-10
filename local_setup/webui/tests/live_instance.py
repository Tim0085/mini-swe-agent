"""Create/reuse an isolated workbench for real process and browser checks."""
import ctypes
import json
import os
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
LOCATOR=ROOT/'.local/webui/audit/fix-instance.json'


def get_json(url,nonce=None):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request=urllib.request.Request(url,headers={'Cookie':'mini_session='+nonce} if nonce else {})
    with opener.open(request,timeout=3) as response:
        return json.load(response)


def start():
    if LOCATOR.is_file():
        previous=json.loads(LOCATOR.read_text())
        state=Path(previous['state'])
        if (state/'service.json').is_file():
            record=json.loads((state/'service.json').read_text())
            try:
                if get_json(f"http://127.0.0.1:{record['port']}/health").get('identity')==record['identity']:
                    print('Reused live isolated instance on port',record['port'])
                    return
            except OSError:
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.OpenProcess.restype=ctypes.c_void_p
                handle=kernel.OpenProcess(0x1000,False,int(record['pid']))
                if handle:
                    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                    kernel.CloseHandle(handle)
                    raise RuntimeError('Previous process still exists; inspect it instead of restarting on an observation failure.')
    state=ROOT/'.local/webui/fix-instances'/uuid.uuid4().hex
    config=state/'config-root'
    (config/'.local').mkdir(parents=True)
    (config/'local_setup').mkdir()
    (config/'.local/.env').write_text("MSWEA_MODEL_NAME='openai/offline-test'\nOPENAI_API_BASE='https://example.invalid/v1'\n",encoding='utf-8')
    (config/'local_setup/windows.yaml').write_bytes((ROOT/'local_setup/webui/defaults.yaml').read_bytes())
    env=os.environ | {'MINI_WEB_STATE':str(state),'MINI_WEB_CONFIG_ROOT':str(config),'MINI_WEB_PORT':'19765','MINI_WEB_TESTING':'1','PYTHONUTF8':'1'}
    result=subprocess.run([str(ROOT/'.venv/Scripts/python.exe'),str(ROOT/'local_setup/webui/launch.py'),'--no-browser'],env=env,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    record=json.loads((state/'service.json').read_text())
    assert get_json(f"http://127.0.0.1:{record['port']}/health")['identity']==record['identity']
    LOCATOR.parent.mkdir(parents=True,exist_ok=True)
    LOCATOR.write_text(json.dumps({'state':str(state),'config_root':str(config),'port':record['port']},indent=2),encoding='utf-8')
    print('Isolated instance ready on port',record['port'],'; production service untouched.')


if __name__=='__main__':
    start()
