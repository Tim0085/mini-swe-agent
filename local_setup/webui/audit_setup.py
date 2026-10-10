"""Capture a bounded installation baseline without reading credential contents."""
import hashlib
import importlib.metadata
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / '.local/webui/audit'


def hashes() -> dict:
    paths = [ROOT / '.local/.env', ROOT / '.local/api-key.dpapi', ROOT / 'local_setup/windows.yaml']
    paths += list((ROOT / '.local').glob('*.traj.json'))
    paths += list((ROOT / '.local').glob('*.predictions.jsonl'))
    paths += list((ROOT / '.local/evaluation').glob('**/report.json'))
    paths += list((ROOT / '.local/evaluation').glob('*/*.json'))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


if __name__ == '__main__':
    AUDIT.mkdir(parents=True, exist_ok=True)
    destination = AUDIT / 'baseline.json'
    if destination.exists():
        raise SystemExit('Baseline already exists; refusing to overwrite.')
    destination.write_text(json.dumps({'protected': hashes(), 'packages': {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}}, indent=2), encoding='utf-8')
    print('Baseline recorded; credential contents were not output.')
