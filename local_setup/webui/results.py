"""Read-only, provenance-aware result summaries for local runs and experiments."""
import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from .common import RUNS, redact
from .benchmarks import PRO_ID, revision_matches, snapshot as benchmark_snapshot

VALID = {'passed', 'test_failed'}
ERRORS = {'evaluation_error', 'solve_failed', 'preparation_failed', 'interrupted', 'unknown'}
LABELS = {'passed': '已解决', 'test_failed': '未解决', 'evaluation_error': '评测异常',
          'solve_failed': '求解失败', 'interrupted': '已中断', 'cancelled': '已取消',
          'preparation_failed': '准备失败',
          'pending': '尚未开始', 'evaluating': '评测中', 'solving': '求解中',
          'submitted': '已提交，待评测', 'unknown': '结果不完整', 'not_run': '尚无记录'}
DATASET_REQUIRED_NOTE = '包含不同基准或固定版本，请先选择单一题集再查看解决率或模型对比；各题集的成绩不会合并。'


def _requires_dataset(items: list[dict]) -> bool:
    datasets = {item.get('dataset', 'unknown') for item in items}
    return len(datasets) > 1 and any(str(dataset).startswith(('rebench-', 'pro-')) for dataset in datasets)


def _read(path: Path) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > 30_000_000:
            return {}
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _seconds(record: dict):
    try:
        return max(0, (datetime.fromisoformat(record['finished']) - datetime.fromisoformat(record['created'])).total_seconds())
    except (KeyError, ValueError, TypeError):
        return None


def _evaluation_time(record: dict) -> tuple[str | None, str]:
    for field in ('finished', 'created'):
        try:
            value = datetime.fromisoformat(record.get(field, ''))
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return value.astimezone(timezone.utc).isoformat(), field
        except (ValueError, TypeError):
            continue
    return None, 'unknown'


def _tests(detail: dict, instance: str) -> dict:
    instance_detail = detail.get(instance, {}) if isinstance(detail, dict) else {}
    groups = instance_detail.get('tests_status', {}) if isinstance(instance_detail, dict) else {}
    groups = groups if isinstance(groups, dict) else {}
    def count(name):
        group = groups.get(name, {})
        passed, failed = (group.get('success'), group.get('failure')) if isinstance(group, dict) else (None, None)
        if not isinstance(passed, list) or not isinstance(failed, list):
            return {'passed': None, 'failed': None, 'total': None, 'pass_rate': None}
        success, failure = len(set(map(str, passed))), len(set(map(str, failed)))
        return {'passed': success, 'failed': failure, 'total': success + failure,
                'failure_names': sorted(set(map(str, failed)))[:20],
                'pass_rate': success / (success + failure) if success + failure else None}
    repair, regression = count('FAIL_TO_PASS'), count('PASS_TO_PASS')
    total = None if repair['total'] is None or regression['total'] is None else repair['total'] + regression['total']
    passed = None if total is None else repair['passed'] + regression['passed']
    return {'repair': repair, 'regression': regression,
            'overall': {'passed': passed, 'failed': None if total is None else total - passed,
                        'total': total, 'pass_rate': passed / total if total else None}}


def _evaluation_status(record: dict) -> str:
    report = record.get('evaluation') or {}
    category = report.get('category')
    if category in {'infrastructure', 'ambiguous', 'incomplete'} or report.get('infrastructure') or report.get('ambiguous') or report.get('errors'):
        return 'evaluation_error'
    if category == 'passed' or report.get('resolved') == 1:
        return 'passed'
    if category == 'test_failed' or (report.get('completed') == 1 and report.get('resolved') == 0 and not report.get('errors') and not report.get('infrastructure') and not report.get('ambiguous')):
        return 'test_failed'
    if record.get('status') in {'cancelled', 'interrupted'}:
        return record['status']
    if record.get('status') in {'preparing', 'running', 'evaluating', 'cleaning', 'stopping'}:
        return 'evaluating'
    return 'evaluation_error'


def _summary(items: list[dict], fixed: bool = False) -> dict:
    resolved = sum(x['status'] == 'passed' for x in items)
    evaluated = sum(x['status'] in VALID for x in items)
    estimates = [x.get('cost', {}).get('estimated') for x in items]
    known = [value for value in estimates if value is not None]
    measured = [x for x in items if x.get('solve_run_id')]
    solve_times = [x['timing']['solve_seconds'] for x in measured if x['timing']['solve_seconds'] is not None]
    evaluation_times = [x['timing']['evaluation_seconds'] for x in items if x['timing']['evaluation_seconds'] is not None]
    preparation_times = [x['timing']['preparation_seconds'] for x in items if x['timing'].get('preparation_seconds') is not None]
    denominator = len(items) if fixed else evaluated
    requires_dataset = _requires_dataset(items)
    return {'instances': len(items), 'evaluated': evaluated, 'resolved': resolved, 'unresolved': evaluated - resolved,
            'resolution_rate': resolved / denominator if denominator and not requires_dataset else None, 'denominator': denominator,
            'requires_dataset': requires_dataset,
            'denominator_kind': 'planned_first_attempts' if fixed else 'valid_evaluations',
            'unevaluated': len(items) - evaluated, 'errors': sum(x['status'] in ERRORS for x in items),
            'cancelled': sum(x['status'] == 'cancelled' for x in items),
            'coverage_rate': evaluated / len(items) if items else None,
            'estimated_cost': sum(known) if known else None, 'cost_known': len(known),
            'cost_unknown': len(items) - len(known),
            'mean_solve_seconds': sum(solve_times) / len(solve_times) if solve_times else None,
            'timed_solves': len(solve_times),
            'mean_evaluation_seconds': sum(evaluation_times) / len(evaluation_times) if evaluation_times else None,
            'timed_evaluations': len(evaluation_times),
            'mean_preparation_seconds': sum(preparation_times) / len(preparation_times) if preparation_times else None,
            'timed_preparations': len(preparation_times)}


def _costs(items: list[dict]) -> dict:
    solves = {x['solve_run_id']: x for x in items if x.get('solve_run_id')}
    amounts = [x['cost']['estimated'] for x in solves.values() if x['cost']['estimated'] is not None]
    return {'estimated_cost': sum(amounts) if amounts else None, 'known_solves': len(amounts),
            'unknown_solves': len(solves) - len(amounts), 'solve_attempts': len(solves),
            'note': '包含所选范围内的失败与重试求解；缺失计价不按零计算。'}


class Results:
    def __init__(self, records: list[dict], queues=(), run_root: Path = RUNS):
        self.root = Path(run_root)
        self.queues = list(queues)
        self.all_records = {r['id']: dict(r) for r in records if r.get('id')}
        self.records = {r['id']: dict(r) for r in records if r.get('id') and r.get('kind') in {'swebench', 'evaluate'}}
        self.requests = {}
        self.patch_hashes = {}
        self.stage_info = {}
        for queue in self.queues:
            for item in queue.get('items', []):
                for attempt in item.get('attempts', []):
                    if attempt.get('run_id'):
                        self.stage_info[attempt['run_id']] = dict(attempt) | {
                            'experiment_id': queue['id'] if queue.get('kind') == 'experiment' else '',
                            'experiment_item_id': item.get('id', item.get('experiment_item_id', '')),
                            'profile_id': item.get('profile_id', '')}
        self.items = self._build()

    def _folder(self, run_id: str) -> Path | None:
        return self.root / run_id if re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}', run_id) else None

    def _request(self, record: dict) -> dict:
        if record['id'] not in self.requests:
            folder = self._folder(record['id'])
            self.requests[record['id']] = _read(folder / 'request.json') if folder else {}
        return self.requests[record['id']]

    def _case(self, record: dict) -> dict:
        request = self._request(record)
        dataset = record.get('dataset') or request.get('dataset') or 'unknown'
        instance = record.get('instance_id') or request.get('instance_id') or ''
        case = f'{dataset}:{instance}'
        snapshot = record.get('benchmark_snapshot') or request.get('benchmark_snapshot') or {}
        valid = isinstance(snapshot, dict)
        snapshot = snapshot if valid else {}
        for source in (record, request):
            valid = valid and all(not source.get(key) or source[key] == value
                                  for key, value in [('dataset', dataset), ('instance_id', instance), ('case_key', case)])
            saved = source.get('benchmark_snapshot') or {}
            if not isinstance(saved, dict):
                valid = False
                continue
            valid = valid and all(not saved.get(key) or saved[key] == dataset for key in ('dataset', 'id'))
            revision = saved.get('source_revision')
            if revision:
                valid = valid and isinstance(revision, str) and revision_matches(dataset, revision)
                valid = valid and revision == snapshot.get('source_revision')
            if dataset == PRO_ID:
                expected = benchmark_snapshot(dataset)
                valid = valid and all(key not in expected or expected[key] == value for key, value in saved.items())
        return {'dataset': dataset, 'instance_id': instance, 'case_key': case,
                'benchmark_snapshot': snapshot if isinstance(snapshot, dict) else {},
                'identity_valid': bool(valid),
                'identity_warning': '' if valid else '基准版本或题目身份记录冲突，未关联其他求解记录。'}

    def _same_case(self, left: dict, right: dict) -> bool:
        a, b = self._case(left), self._case(right)
        revisions = [value['benchmark_snapshot'].get('source_revision') for value in (a, b)]
        return (a['identity_valid'] and b['identity_valid'] and a['case_key'] == b['case_key']
                and (not all(revisions) or revisions[0] == revisions[1]))

    @staticmethod
    def _case_token(item: dict) -> tuple:
        return (item['dataset'], item['instance_id'], '' if item.get('identity_valid', True) else item['attempt_id'])

    def _excluded(self, record: dict) -> bool:
        request = self._request(record)
        return bool(record.get('diagnostic') or record.get('model') == 'deterministic' or request.get('queue_context', {}).get('diagnostic') or request.get('diagnostic'))

    def _patch_hash(self, record: dict) -> str:
        if record['id'] not in self.patch_hashes:
            self.patch_hashes[record['id']] = self._read_patch_hash(record)
        return self.patch_hashes[record['id']]

    def _read_patch_hash(self, record: dict) -> str:
        recorded = record.get('patch_hash') or record.get('patch_source', {}).get('patch_hash')
        if recorded:
            return str(recorded)
        folder = self._folder(record['id'])
        if not folder:
            return ''
        for name in ('input.predictions.jsonl', 'predictions.jsonl'):
            path = folder / name
            try:
                if not path.is_file() or path.stat().st_size > 20_000_000:
                    continue
                for line in path.read_text(encoding='utf-8-sig').splitlines():
                    row = json.loads(line)
                    if row.get('instance_id') == record.get('instance_id') and isinstance(row.get('model_patch'), str):
                        return hashlib.sha256(row['model_patch'].replace('\r\n', '\n').encode()).hexdigest()
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return ''

    def _limits(self, record: dict, request: dict, snapshot: dict) -> dict:
        keys = ('step_limit', 'cost_limit', 'timeout')
        configured = snapshot.get('limits') or (request.get('settings') if record.get('kind') != 'evaluate' else {}) or {}
        configured = {key: _number(configured.get(key)) for key in keys}
        candidates = [(record.get('effective_limits') or request.get('effective_limits') or {}, 'recorded')]
        if record.get('kind') != 'evaluate':
            candidates.append((request.get('settings') or {}, 'request_settings'))
        effective, source = {key: None for key in keys}, 'profile_snapshot'
        for limits, origin in candidates:
            if isinstance(limits, dict) and any(_number(limits.get(key)) is not None for key in keys):
                effective = {key: _number(limits.get(key)) for key in keys}
                source = origin
                break
        if source == 'profile_snapshot' and record.get('kind') == 'swebench':
            folder = self._folder(record['id'])
            info = _read(folder / 'trajectory.traj.json').get('info') if folder else {}
            config = info.get('config', {}) if isinstance(info, dict) else {}
            config = config if isinstance(config, dict) else {}
            agent, environment = config.get('agent', {}), config.get('environment', {})
            agent = agent if isinstance(agent, dict) else {}
            environment = environment if isinstance(environment, dict) else {}
            values = {key: agent.get(key) for key in ('step_limit', 'cost_limit')}
            values['timeout'] = environment.get('timeout')
            if any(_number(values.get(key)) is not None for key in keys):
                effective = {key: _number(values.get(key)) for key in keys}
                source = 'trajectory'
        return {'limits': configured if source == 'profile_snapshot' else effective,
                'configured_limits': configured, 'effective_limits': effective, 'limits_source': source}

    def _profile(self, record: dict) -> dict:
        request = self._request(record)
        snapshot = record.get('profile_snapshot') or request.get('profile_snapshot') or {}
        settings = (request.get('settings') or {}) if record.get('kind') != 'evaluate' else {}
        limits = self._limits(record, request, snapshot)
        identity = {'model_name': snapshot.get('model_name', settings.get('model_name', record.get('model', '未知模型'))),
                    'connection_id': snapshot.get('connection_id', 'legacy' if settings else None),
                    'pricing_fingerprint': _hash(snapshot['pricing']) if snapshot.get('pricing') else None,
                    'api_base': snapshot.get('api_base', settings.get('api_base')),
                    'protocol': snapshot.get('protocol', settings.get('model_class')),
                    'parameters': snapshot.get('parameters', {}),
                    'limits': limits['limits'],
                    'runtime': snapshot.get('runtime', {}), 'configs_fingerprint': _hash(snapshot['configs']) if snapshot.get('configs') else None,
                    'auto_approve': request.get('auto_approve', record.get('auto_approve'))}
        # Missing legacy execution conditions must not masquerade as a known configuration.
        complete = bool(snapshot)
        if not snapshot:
            identity['legacy_unknown_conditions'] = True
        if limits['limits_source'] == 'profile_snapshot':
            identity['effective_limits_unconfirmed'] = True
        return {'group_id': _hash(identity)[:24], 'configuration_fingerprint': _hash(identity), 'model': identity['model_name'],
                'profile_id': snapshot.get('profile_id', record.get('profile_id', '')),
                'profile_name': snapshot.get('profile_name', record.get('model', '未知模型')),
                'profile_version': snapshot.get('profile_version'), 'conditions_known': complete,
                'pricing_source': snapshot.get('pricing', {}).get('source', snapshot.get('pricing', {}).get('mode', 'unknown')),
                'conditions': {**limits, 'parameters': identity['parameters'],
                               'connection_id': identity['connection_id'], 'connection_name': snapshot.get('connection_name'),
                               'pricing_fingerprint': identity['pricing_fingerprint'],
                               'pricing_source': snapshot.get('pricing', {}).get('source', snapshot.get('pricing', {}).get('mode', 'unknown')),
                               'pricing': {key: value for key, value in snapshot.get('pricing', {}).items()
                                           if key in {'mode', 'source', 'currency', 'available', 'input_per_million',
                                                      'output_per_million', 'cached_input_per_million'}
                                           and isinstance(value, (str, int, float, bool, type(None)))},
                               'protocol': identity['protocol'], 'runtime_fingerprint': _hash(identity['runtime']) if identity['runtime'] else None,
                               'configs_fingerprint': identity['configs_fingerprint'],
                               'auto_approve': identity['auto_approve']}}

    def _report(self, record: dict) -> dict:
        value = dict(record)
        evaluation = dict(value.get('evaluation') or {})
        folder = self._folder(record['id'])
        if folder:
            reports = list(folder.glob('*.' + record['id'] + '.json'))
            if reports:
                report = _read(reports[0])
                for public, raw in [('resolved', 'resolved_instances'), ('completed', 'completed_instances'),
                                    ('errors', 'error_instances'), ('infrastructure', 'infra_failure_instances'),
                                    ('ambiguous', 'ambiguous_failure_instances')]:
                    if public not in evaluation and raw in report:
                        evaluation[public] = report[raw]
            if not evaluation.get('detail'):
                details = list(folder.glob('logs/run_evaluation/**/report.json'))
                if details:
                    evaluation['detail'] = _read(details[0])
        value['evaluation'] = evaluation
        return value

    def _cost(self, record: dict | None) -> dict:
        if not record:
            return {'estimated': None, 'known': False, 'source': 'missing_solve_record'}
        billing, stats = record.get('billing') or {}, record.get('stats') or {}
        snapshot = record.get('profile_snapshot') or self._request(record).get('profile_snapshot') or {}
        explicit_free = snapshot.get('pricing', {}).get('mode') == 'free'
        values = [_number(stats.get('cost')), _number(billing.get('cost_floor'))]
        values = [x for x in values if x is not None]
        unknown = billing.get('pending') or billing.get('unknown') or not values or (max(values) == 0 and bool(stats.get('calls')) and not explicit_free)
        return {'estimated': None if unknown else max(values), 'known': not unknown,
                'source': 'unknown' if unknown else 'explicit_free' if explicit_free else 'local_estimate', 'recorded_lower_bound': max(values) if values else None}

    def _item(self, solve: dict | None, evaluations: list[dict]) -> dict:
        ordered = sorted(evaluations, key=lambda r: (_evaluation_time(r)[0] or '', r['id']))
        valid = [r for r in ordered if _evaluation_status(r) in VALID]
        evaluation = valid[-1] if valid else ordered[-1] if ordered else None
        record = solve or evaluation
        profile = self._profile(record)
        stage = self.stage_info.get(record['id'], {})
        request = self._request(record)
        source_status = (solve or {}).get('status')
        if evaluation:
            status = _evaluation_status(evaluation)
        elif source_status in {'failed','cancelled','interrupted','cleanup_required'}:
            status = {'failed':'solve_failed','cancelled':'cancelled','interrupted':'interrupted','cleanup_required':'interrupted'}[source_status]
        elif source_status == 'completed':
            status = 'submitted' if solve.get('prediction') or solve.get('result', {}).get('exit_status') == 'Submitted' else 'unknown'
        else:
            status = 'solving'
        case = self._case(record)
        if not case['identity_valid']:
            status = 'unknown'
        solve_time, evaluation_time = _seconds(solve or {}), _seconds(evaluation or {})
        attempt_id = record.get('attempt_id') or stage.get('attempt_id') or (solve['id'] if solve else 'evaluation-' + record['id'])
        preparation_time = self._preparation_seconds(attempt_id, record)
        capture = (solve or {}).get('result', {}).get('patch_capture', {}) if case['dataset'] == PRO_ID else {}
        return profile | case | {'attempt_id': attempt_id, 'attempt_number': record.get('attempt_number', stage.get('attempt_number', 1)),
                          'created': record.get('created', ''), 'status': status, 'verdict': '部分补丁，待评测' if status == 'submitted' and capture.get('partial') else LABELS[status],
                          'patch_capture': capture, 'patch_available': bool((solve or {}).get('prediction')),
                          'solve_status': source_status,
                          'solve_run_id': solve['id'] if solve else None, 'evaluation_run_id': evaluation['id'] if evaluation else None,
                          'evaluation_run_ids': [r['id'] for r in ordered],
                          'evaluated_at': _evaluation_time(evaluation or {})[0],
                          'evaluation_time_source': _evaluation_time(evaluation or {})[1],
                          'latest_evaluation_status': _evaluation_status(ordered[-1]) if ordered else None,
                          'has_later_evaluation_error': bool(valid and ordered[-1]['id'] != valid[-1]['id']),
                          'patch_hash': self._patch_hash(record),
                          'association': 'solve_record' if solve else 'evaluation_only',
                          'tests': _tests((evaluation or {}).get('evaluation', {}).get('detail', {}), record.get('instance_id', '')),
                          'cost': self._cost(solve), 'calls': (solve or {}).get('stats', {}).get('calls'),
                          'error': str(record.get('error') or (record.get('result') or {}).get('exception_str') or '')[:1200],
                          'timing': {'preparation_seconds': preparation_time, 'solve_seconds': solve_time, 'evaluation_seconds': evaluation_time,
                                     'total_seconds': preparation_time + solve_time + evaluation_time if all(x is not None for x in (preparation_time, solve_time, evaluation_time)) else None},
                          'experiment_id': record.get('experiment_id') or stage.get('experiment_id') or request.get('experiment_id', ''),
                          'experiment_item_id': record.get('experiment_item_id') or stage.get('experiment_item_id') or request.get('experiment_item_id', '')}

    def _preparation_seconds(self, attempt_id: str, record: dict):
        if not attempt_id:
            return None
        matched = [r for r in self.all_records.values() if r.get('kind') in {'download', 'cache'} and r.get('status') == 'completed'
                   and (r.get('attempt_id') or self.stage_info.get(r['id'], {}).get('attempt_id')) == attempt_id
                   and r.get('instance_id') == record.get('instance_id') and r.get('dataset') == record.get('dataset')]
        times = [_seconds(r) for r in matched]
        return sum(times) if times and all(x is not None for x in times) else None

    def _same_configuration(self, left: dict, right: dict) -> bool:
        snapshots = [r.get('profile_snapshot') or self._request(r).get('profile_snapshot') for r in (left, right)]
        if not all(snapshots):
            return True
        keys = ('model_name', 'connection_id', 'api_base', 'protocol', 'parameters', 'limits', 'runtime', 'configs', 'pricing')
        return _hash({k: snapshots[0].get(k) for k in keys}) == _hash({k: snapshots[1].get(k) for k in keys})

    def _build(self) -> list[dict]:
        records = [r for r in self.records.values() if not self._excluded(r)]
        solves = {r['id']: r for r in records if r['kind'] == 'swebench'}
        associated = {key: [] for key in solves}
        orphaned = {}
        for raw in records:
            if raw['kind'] != 'evaluate':
                continue
            record = self._report(raw)
            requested = self._request(record)
            explicit = record.get('solve_run_id') or requested.get('solve_run_id')
            stage = self.stage_info.get(record['id'], {})
            identity = record.get('attempt_id') or stage.get('attempt_id')
            patch = self._patch_hash(record)
            source = record.get('patch_source', {}).get('path', '')
            source_id = Path(source.replace('\\', '/')).parent.name if source else ''
            source_id = source_id if self._folder(source_id) else ''
            candidate = explicit or source_id
            candidates = [solves[candidate]] if candidate in solves else [] if candidate else list(solves.values())
            matches = []
            for solve in candidates:
                solve_attempt = solve.get('attempt_id') or self.stage_info.get(solve['id'], {}).get('attempt_id')
                same_attempt = identity and identity == solve_attempt
                solve_hash = self._patch_hash(solve)
                same_patch = patch and patch == solve_hash
                same_context = self._same_case(record, solve) and record.get('model') == solve.get('model')
                if identity and not same_attempt and (solve_attempt or not candidate):
                    continue
                if same_context and self._same_configuration(record, solve) and (same_attempt or same_patch or solve['id'] == explicit) and not (patch and solve_hash and patch != solve_hash):
                    matches.append(solve)
            if len(matches) == 1:
                associated[matches[0]['id']].append(record)
            else:
                origin = ('solve', candidate) if candidate else ('attempt', identity) if identity else ('evaluation', record['id'])
                case = self._case(record)
                key = (case['case_key'], case['benchmark_snapshot'].get('source_revision'),
                       '' if case['identity_valid'] else record['id'], self._profile(record)['group_id'], origin, patch or record['id'])
                orphaned.setdefault(key, []).append(record)
        items = [self._item(solve, associated[run_id]) for run_id, solve in solves.items()]
        items.extend(self._item(None, runs) for runs in orphaned.values())
        return sorted(items, key=lambda x: (x['created'], x['attempt_id']), reverse=True)

    def _latest(self, items: list[dict]) -> list[dict]:
        def recency(row):
            valid = row['status'] in VALID
            return (valid, (row.get('evaluated_at') if valid else row['created']) or '',
                    (row.get('evaluation_run_id') if valid else row['attempt_id']) or '')

        groups = {}
        for item in sorted(items, key=lambda x: (x['created'], x['attempt_id'])):
            key = (*self._case_token(item), item['group_id'])
            previous = groups.get(key)
            if not previous or recency(item) > recency(previous):
                groups[key] = item
        return list(groups.values())

    def _groups(self, items: list[dict], fixed=False) -> list[dict]:
        groups = {}
        for item in items:
            groups.setdefault(item['group_id'], []).append(item)
        result = []
        for key, rows in groups.items():
            conditions = dict(rows[0]['conditions'])
            variants = list({json.dumps(row['conditions']['limits'],sort_keys=True):row['conditions']['limits'] for row in rows}.values())
            if fixed and len(variants) > 1:
                conditions.update(conditions_varied=True,effective_limits_variants=variants,
                                  effective_limits={key:None for key in ('step_limit','cost_limit','timeout')},
                                  limits=conditions['configured_limits'],limits_source='experiment_varying')
            result.append({'group_id': key, 'model': rows[0]['model'], 'profile_id': rows[0]['profile_id'],
                 'profile_name': rows[0]['profile_name'], 'conditions_known': rows[0]['conditions_known'],
                 'configuration_fingerprint': rows[0]['configuration_fingerprint'], 'pricing_source': rows[0]['pricing_source'],
                 'conditions': conditions, 'summary': _summary(rows, fixed)})
        return result

    def listing(self, dataset='all', search='', limit=50, offset=0) -> dict:
        query = search.strip().lower()
        rows = [x for x in self.items if (dataset == 'all' or x['dataset'] == dataset) and
                (not query or query in ' '.join(str(x[k]) for k in ['instance_id', 'model', 'profile_name']).lower())]
        latest = self._latest(rows)
        coverage = {}
        for item in latest:
            if item['status'] in VALID:
                coverage.setdefault(item['group_id'], set()).add((item['dataset'], item['instance_id']))
        pairs = [(len(a_cases & coverage[b]), a, b) for a, a_cases in coverage.items() for b in coverage if a < b]
        models = {item['group_id']: item['model'] for item in latest}
        different_models = [pair for pair in pairs if pair[0] and models[pair[1]] != models[pair[2]]]
        best = max(different_models or pairs, default=(0, '', ''))
        defaults = list(best[1:]) if best[0] else list(coverage)[:2]
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        return redact({'items': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit,
                       'summary': _summary(latest), 'all_attempts_cost': _costs(rows), 'groups': self._groups(latest), 'comparison_defaults': defaults,
                       'note': DATASET_REQUIRED_NOTE if _requires_dataset(latest) else '汇总按题目与执行配置去重，采用最近一次有效评测；重试和异常记录保留。解决率仅代表当前已评测样本。'})

    def detail(self, attempt_id: str) -> dict:
        item = next((x for x in self.items if x['attempt_id'] == attempt_id or x['solve_run_id'] == attempt_id or attempt_id in x['evaluation_run_ids']), None)
        if not item:
            raise ValueError('未找到这次求解或评测记录。')
        return redact(item | {'related_attempts': [x for x in self.items if x['instance_id'] == item['instance_id'] and x['dataset'] == item['dataset'] and x['attempt_id'] != item['attempt_id']],
                              'note': '测试通过比例不等于题目解决率；题目结论以正式评测 resolved 标记为准。'})

    def for_run(self, run_id: str) -> dict | None:
        item = next((x for x in self.items if x['solve_run_id'] == run_id or run_id in x['evaluation_run_ids']), None)
        return redact(item) if item else None

    def compare(self, group_ids: list[str] | None = None, dataset='all', experiment_id: str | None = None) -> dict:
        if experiment_id:
            return self.experiment(experiment_id)
        items = self._latest([x for x in self.items if (dataset == 'all' or x['dataset'] == dataset) and (group_ids is None or x['group_id'] in group_ids)])
        if dataset == 'all' and _requires_dataset(items):
            cases = {(item['dataset'], item['instance_id']) for item in items}
            return {'mode': 'history_common_cases', 'groups': [], 'instance_ids': [], 'rows': [],
                    'summary': {'common_instances': 0, 'union_instances': len(cases), 'groups': 0},
                    'requires_dataset': True, 'note': DATASET_REQUIRED_NOTE}
        groups = self._groups(items)
        ids = [g['group_id'] for g in groups]
        cases = sorted({(x['dataset'], x['instance_id']) for x in items})
        lookup = {(x['dataset'], x['instance_id'], x['group_id']): x for x in items}
        common = [case for case in cases if all(lookup.get((*case, group), {}).get('status') in VALID for group in ids)] if len(ids) >= 2 else []
        for group in groups:
            group['coverage_summary'] = group['summary']
            group['summary'] = _summary([lookup[(*case, group['group_id'])] for case in common])
            group['all_attempts_cost'] = _costs([x for x in self.items if x['group_id'] == group['group_id'] and (dataset == 'all' or x['dataset'] == dataset)])
        return redact({'mode': 'history_common_cases', 'groups': groups, 'instance_ids': [case[1] for case in cases],
                       'rows': [{'dataset': case[0], 'instance_id': case[1], 'included': case in common,
                                 'cells': [lookup.get((*case, group), {'group_id': group, 'status': 'not_run', 'verdict': LABELS['not_run']}) for group in ids]} for case in cases],
                       'summary': {'common_instances': len(common), 'union_instances': len(cases), 'groups': len(groups)},
                       'note': '主比较只统计各配置共同有有效评测的题目；缺测与异常仍显示。没有共同题集时不生成排名。历史结果不是标准 pass@1。'})

    def experiment(self, experiment_id: str) -> dict:
        queue = next((q for q in self.queues if q['id'] == experiment_id and q.get('kind') == 'experiment'), None)
        if not queue:
            raise ValueError('未找到对比实验。')
        slots = []
        snapshots = queue.get('profile_snapshots', {})
        for item in queue.get('items', []):
            first = item.get('first_attempt_id')
            row = next((x for x in self.items if first and x['attempt_id'] == first
                        and x['dataset'] == queue.get('dataset', 'verified') and x['instance_id'] == item['instance_id']), None)
            if not row:
                run = {'id': '', 'model': snapshots.get(item.get('profile_id'), {}).get('model_name', '未知模型'),
                       'profile_snapshot': snapshots.get(item.get('profile_id'), {}), 'auto_approve': queue.get('auto_approve', False)}
                profile = self._profile(run)
                status = 'cancelled' if item.get('status') in {'cancelled', 'skipped'} else 'pending'
                first_stages = [a for a in item.get('attempts', []) if a.get('attempt_id') == first]
                failed_stage = next((a for a in reversed(first_stages) if a.get('status') in {'failed', 'interrupted', 'cancelled', 'cleanup_required'}), None)
                if failed_stage:
                    status = failed_stage['status'] if failed_stage['status'] in {'interrupted', 'cancelled'} else {'download': 'preparation_failed', 'cache': 'preparation_failed', 'evaluate': 'evaluation_error'}.get(failed_stage.get('stage'), 'solve_failed')
                elif first_stages:
                    status = 'unknown' if any(a.get('status') == 'completed' and a.get('stage') == 'solve' for a in first_stages) else 'pending'
                row = profile | {'attempt_id': first, 'instance_id': item['instance_id'], 'dataset': queue.get('dataset', 'verified'),
                                 'case_key': f"{queue.get('dataset', 'verified')}:{item['instance_id']}",
                                 'benchmark_snapshot': queue.get('benchmark_snapshot', {}),
                                 'status': status, 'verdict': LABELS[status], 'cost': self._cost(None), 'solve_run_id': None,
                                 'timing': {'preparation_seconds': self._preparation_seconds(first, {'instance_id': item['instance_id'], 'dataset': queue.get('dataset', 'verified')}),
                                            'solve_seconds': None, 'evaluation_seconds': None, 'total_seconds': None}}
            # An experiment keeps one column per selected profile, even for equivalent configurations.
            row = row | {'group_id': _hash([queue['id'], item.get('profile_id')])[:24]}
            slots.append(row)
        groups = self._groups(slots, fixed=True)
        cases = sorted({(x['dataset'], x['instance_id']) for x in slots})
        lookup = {(x['dataset'], x['instance_id'], x['group_id']): x for x in slots}
        retries = [x for x in self.items if x.get('experiment_id') == experiment_id and x.get('attempt_number', 1) > 1]
        return redact({'mode': 'experiment_first_attempt', 'experiment_id': experiment_id, 'groups': groups,
                       'instance_ids': [case[1] for case in cases],
                       'rows': [{'dataset': case[0], 'instance_id': case[1], 'included': True,
                                 'cells': [lookup.get((*case, group['group_id']), {'group_id': group['group_id'], 'status': 'not_run'}) for group in groups]} for case in cases],
                       'summary': _summary(slots, fixed=True), 'retry_results': retries,
                       'all_attempts_cost': _costs([x for x in self.items if x.get('experiment_id') == experiment_id]),
                       'note': '首轮解决率使用固定计划题目数作分母；未执行、异常及未评测不算解决。后续重试单独展示，不替换首轮成绩。'})

    def _export_items(self, dataset: str, group_ids: list[str] | None) -> list[dict]:
        return [x for x in self.items if (dataset == 'all' or x['dataset'] == dataset) and
                (group_ids is None or x['group_id'] in group_ids)]

    def export_json(self, experiment_id: str | None = None, dataset='all', group_ids: list[str] | None = None) -> str:
        items = self._export_items(dataset, group_ids)
        value = self.experiment(experiment_id) if experiment_id else {'items': items, 'summary': _summary(self._latest(items)),
                                                                    'all_attempts_cost': _costs(items),
                                                                    'selection_policy': 'latest_valid_per_instance_configuration',
                                                                    'note': DATASET_REQUIRED_NOTE if _requires_dataset(items) else 'items保留筛选范围内全部尝试；汇总每题每配置取最近有效评测，不择优。'}
        if not experiment_id and group_ids is not None:
            value['comparison'] = self.compare(group_ids=group_ids, dataset=dataset)
        value['export_columns'] = self._export_metadata(items, experiment_id, dataset, group_ids)
        return json.dumps(redact(value), ensure_ascii=False, indent=2)

    def _export_metadata(self, items: list[dict], experiment_id, dataset, group_ids) -> dict:
        if experiment_id:
            data = self.experiment(experiment_id)
            selected = {cell.get('attempt_id') for row in data['rows'] for cell in row['cells']}
            return {'selection_policy': 'fixed_first_attempt', 'denominator_scope': 'planned_first_attempts',
                    'denominators': {g['group_id']: g['summary']['denominator'] for g in data['groups']},
                    'included_attempt_ids': sorted(x for x in selected if x)}
        latest = self._latest(items)
        if group_ids is not None:
            compared = self.compare(group_ids=group_ids, dataset=dataset)
            selected = {cell.get('attempt_id') for row in compared['rows'] if row['included'] for cell in row['cells']}
            denominators = {group['group_id']: group['summary']['denominator'] for group in compared['groups']}
            scope = 'common_valid_evaluations'
        else:
            selected = {x['attempt_id'] for x in latest if x['status'] in VALID}
            denominators = {g['group_id']: g['summary']['denominator'] for g in self._groups(latest)}
            scope = 'valid_evaluations_per_configuration'
        return {'selection_policy': 'latest_valid_per_instance_configuration', 'denominator_scope': scope,
                'denominators': denominators, 'included_attempt_ids': sorted(x for x in selected if x),
                'requires_dataset': _requires_dataset(latest)}

    def export_csv(self, experiment_id: str | None = None, dataset='all', group_ids: list[str] | None = None) -> str:
        rows = [cell for row in self.experiment(experiment_id)['rows'] for cell in row['cells']] if experiment_id else self._export_items(dataset, group_ids)
        metadata = self._export_metadata(rows, experiment_id, dataset, group_ids)
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(['attempt_id', 'dataset', 'instance_id', 'model', 'profile_name', 'status', 'solve_run_id', 'evaluation_run_id',
                         'estimated_cost_usd', 'solve_seconds', 'evaluation_seconds', 'repair_passed', 'repair_total', 'regression_passed', 'regression_total',
                         'group_id', 'configuration_fingerprint', 'conditions_known', 'conditions_json', 'pricing_source',
                         'selection_policy', 'denominator_scope', 'denominator_count', 'included_in_summary', 'preparation_seconds', 'total_seconds',
                         'case_key', 'benchmark_snapshot_json', 'identity_warning', 'requires_dataset', 'evaluated_at', 'evaluation_time_source'])
        for row in rows:
            tests, timing = row.get('tests', {}), row.get('timing', {})
            values = [row.get(k, '') for k in ['attempt_id', 'dataset', 'instance_id', 'model', 'profile_name', 'status', 'solve_run_id', 'evaluation_run_id']]
            values += [row.get('cost', {}).get('estimated'), timing.get('solve_seconds'), timing.get('evaluation_seconds'),
                       tests.get('repair', {}).get('passed'), tests.get('repair', {}).get('total'),
                       tests.get('regression', {}).get('passed'), tests.get('regression', {}).get('total')]
            values += [row.get('group_id'), row.get('configuration_fingerprint'), row.get('conditions_known'),
                       json.dumps(redact(row.get('conditions', {})), ensure_ascii=False, sort_keys=True), row.get('pricing_source', 'unknown'),
                       metadata['selection_policy'], metadata['denominator_scope'], metadata['denominators'].get(row.get('group_id'), 0),
                       row.get('attempt_id') in metadata['included_attempt_ids'], timing.get('preparation_seconds'), timing.get('total_seconds'),
                       row.get('case_key'), json.dumps(redact(row.get('benchmark_snapshot', {})), ensure_ascii=False, sort_keys=True), row.get('identity_warning', ''),
                       metadata.get('requires_dataset', False), row.get('evaluated_at'), row.get('evaluation_time_source')]
            safe = [redact(str(value)) if value is not None else '' for value in values]
            writer.writerow(["'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in safe])
        return stream.getvalue()
