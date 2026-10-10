import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = Path(os.getenv('MINI_WEB_CONFIG_ROOT', str(ROOT))).resolve()
SETUP = ROOT / 'local_setup'
sys.path.insert(0, str(SETUP))
STATE = Path(os.getenv('MINI_WEB_STATE', str(ROOT / '.local/webui'))).resolve()
RUNS = STATE / 'runs'
STATIC = Path(__file__).parent / 'static'
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'cleanup_required'}
PRIVATE_VALUES: list[str] = []
APP_VERSION = '1.6.1'


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def new_id() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(4)


def redact(value):
    if isinstance(value, dict):
        def private_field(key):
            normalized=re.sub('[^a-z0-9]','',key.lower())
            return normalized in {'authorization','password','secret','token'} or normalized.endswith(('apikey','password','clientsecret','secretkey','accesstoken','refreshtoken','authtoken'))
        return {k: ('[REDACTED]' if private_field(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for key in PRIVATE_VALUES:
            if key:
                value = value.replace(key, '[REDACTED]')
        value = re.sub(r'(?i)\bsk-[A-Za-z0-9_-]{8,}', '[REDACTED]', value)
        value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', value)
    return value


def atomic(path: Path, content: str) -> None:
    atomic_bytes(path, content.encode('utf-8'))


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    try:
        temp.write_bytes(content)
        for attempt in range(6):
            try:
                os.replace(temp, path)
                break
            except PermissionError as exc:
                if os.name!='nt' or getattr(exc,'winerror',None) not in {5,32,33} or attempt==5:
                    raise
                time.sleep(.025*2**attempt)
    finally:
        temp.unlink(missing_ok=True)


def save_json(path: Path, value) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2)
    assert_no_credentials(content)
    atomic(path, content)


def assert_no_credentials(value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if any(secret and secret in text for secret in PRIVATE_VALUES):
        raise ValueError('内容包含当前已配置的密钥，操作已阻止；原文未被静默替换。请移除密钥后重试。')


def export_allowed(content: str) -> bool:
    if any(secret and secret in content for secret in PRIVATE_VALUES):
        return False
    return not re.search(r'(?i)\bsk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9_.-]{12,}', content)


def source_version() -> str:
    base = Path(__file__).parent
    files = sorted([*base.glob('*.py'), *base.glob('*.yaml'), *STATIC.glob('*'), *(SETUP/name for name in ['credentials.py','windows_env.py','run.py'])])
    files += sorted(path for path in (base/'vendor').rglob('*') if path.is_file() and (path.suffix in {'.py','.json'} or path.name=='LICENSE'))
    return hashlib.sha256(b''.join(p.read_bytes() for p in files if p.is_file())).hexdigest()[:16]


def recover_json(path: Path, default):
    try:
        return read_json(path)
    except (OSError, ValueError, TypeError):
        return default


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def file_version(*paths: Path) -> str:
    return hashlib.sha256(b'\0'.join(p.read_bytes() if p.exists() else b'' for p in paths)).hexdigest()


def job_dir(run_id: str) -> Path:
    if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}', run_id):
        raise ValueError('任务编号无效。')
    return RUNS / run_id
