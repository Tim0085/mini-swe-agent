import importlib.metadata
import json
from datetime import date
from pathlib import Path

from .audit_setup import hashes
from .common import ROOT, read_json, save_json


if __name__ == '__main__':
    baseline = read_json(ROOT / '.local/webui/audit/baseline.json')
    current = hashes()
    changed = [p for p,h in baseline['protected'].items() if current.get(p)!=h]
    packages = {d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
    upgrades = {name: [version,packages.get(name)] for name,version in baseline['packages'].items() if packages.get(name)!=version}
    added = {name:version for name,version in packages.items() if name not in baseline['packages']}
    sizes = {}
    for name in added:
        distribution = importlib.metadata.distribution(name)
        sizes[name] = sum(distribution.locate_file(p).stat().st_size for p in distribution.files or [] if distribution.locate_file(p).is_file())
    old_report = next((ROOT / '.local/evaluation/django10097-lf-20260914-232849').glob('*.json'),None)
    report = read_json(old_report) if old_report else {}
    result = {'protected_files_checked':len(baseline['protected']),'protected_files_changed':changed,'existing_packages_changed':upgrades,
              'added_packages':added,'added_package_bytes':sizes,'total_added_package_bytes':sum(sizes.values()),
              'historical_result':{k:report.get(k) for k in ['completed_instances','resolved_instances','unresolved_instances','error_instances']}}
    save_json(ROOT / 'collaboration/artifacts' / date.today().isoformat() / 'workbench-fixes/release-audit.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    assert not changed and not upgrades
