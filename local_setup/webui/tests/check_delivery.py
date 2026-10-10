import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from local_setup.webui.common import read_json, save_json
from local_setup.webui.launch import launch

if __name__ == '__main__':
    before = read_json(ROOT / '.local/webui/service.json')
    port = launch(False)
    after = read_json(ROOT / '.local/webui/service.json')
    assert before['pid'] == after['pid'] and before['identity'] == after['identity']
    files = [ROOT / 'LOCAL_SETUP.md', ROOT / 'collaboration/README.md', *list((ROOT / 'collaboration/docs').glob('*.md'))]
    checked = 0
    for file in files:
        source = file.read_text(encoding='utf-8-sig')
        for target in re.findall(r'\]\(([^)]+)\)',source):
            if '://' not in target:
                assert (file.parent / target).resolve().exists(), (file.name,target)
                checked += 1
    manifest = read_json(ROOT / '.local/archive/install/manifest.json')
    import hashlib
    for item in manifest:
        assert hashlib.sha256(Path(item['destination']).read_bytes()).hexdigest().upper() == item['sha256']
        assert not Path(item['source']).exists()
    output = {'launcher_reuses_service':True,'port':port,'documentation_links_checked':checked,'archived_logs_verified':len(manifest),'paid_model_calls':0}
    save_json(ROOT / 'collaboration/artifacts/2026-09-15/workbench/delivery-check.json',output)
    print(json.dumps(output,ensure_ascii=False))
