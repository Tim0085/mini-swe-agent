import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ['MINI_WEB_STATE'] = str(ROOT / '.local/webui/test-runs' / uuid4().hex)
os.environ['MINI_WEB_NONCE'] = 'offline-test-session-' + uuid4().hex
os.environ['MINI_WEB_PORT'] = '8765'
config_root=Path(os.environ['MINI_WEB_STATE'])/'config-root'
(config_root/'.local').mkdir(parents=True)
(config_root/'local_setup').mkdir()
(config_root/'.local/.env').write_text("MSWEA_MODEL_NAME='openai/offline-test'\nOPENAI_API_BASE='https://example.invalid/v1'\n",encoding='utf-8')
(config_root/'local_setup/windows.yaml').write_bytes((ROOT/'local_setup/webui/defaults.yaml').read_bytes())
os.environ['MINI_WEB_CONFIG_ROOT']=str(config_root)
