"""Control only the isolated instance tracked by live_instance.py."""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
locator=json.loads((ROOT/'.local/webui/audit/fix-instance.json').read_text())
state=Path(locator['state'])
record=json.loads((state/'service.json').read_text())
base=f"http://127.0.0.1:{record['port']}"
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path,body=None):
    headers={'Cookie':'mini_session='+record['nonce']}
    if body is not None:
        headers['Content-Type']='application/json'
    request=urllib.request.Request(base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with opener.open(request,timeout=5) as response:
        return json.load(response)


if __name__=='__main__':
    before=api('/health')
    assert before['identity']==record['identity']
    env=api('/api/environment')
    if env.get('active_run'):
        raise SystemExit('Isolated task is active. Inspect or explicitly stop it before restarting.')
    action=sys.argv[1] if len(sys.argv)>1 else 'restart'
    assert action in {'restart','exit'}
    api('/api/lifecycle',{'action':action,'mode':'stop'})
    acknowledged=False
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        try:
            health=api('/health')
            if action=='restart' and health['boot_id']!=before['boot_id']:
                print('Isolated service restarted; production untouched.')
                break
            status=api('/api/environment')['lifecycle']
            if status['status']=='ready':
                api('/api/lifecycle/ack',{'id':status['id']})
                acknowledged=True
            if status['status']=='blocked':
                raise RuntimeError(status['error'])
        except (OSError,urllib.error.URLError):
            if action=='exit' and acknowledged:
                print('Isolated service exit confirmed.')
                break
        time.sleep(.25)
    else:
        raise SystemExit('Observation timed out; do not restart blindly. Recheck this same instance.')
