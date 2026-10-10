"""Read-only patch selection and links to matching saved conversations."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .bench import captured_pro_patch, prediction, valid_instance
from .benchmarks import REGISTRY,revision_matches,snapshot as current_snapshot
from .common import CONFIG_ROOT, job_dir, read_json, redact


class PatchIdentityError(ValueError):
    pass


def fingerprint(patch: str) -> str:
    return hashlib.sha256(patch.replace('\r\n', '\n').encode()).hexdigest()


def _patch_identity(path: Path, instance: str, recorded_instance: str | dict, dataset: str) -> dict:
    recorded = recorded_instance if isinstance(recorded_instance, dict) else {'instance_id': recorded_instance}
    if path.name.endswith('.traj.json'):
        value = read_json(path)
        sources = [recorded, value, value.get('info', {})]
    else:
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        sources = [recorded, *[row for row in rows if row.get('instance_id') == instance]]
    datasets, instances, cases, revisions = set(), set(), set(), set()
    snapshot = {}
    snapshots = []
    for source in sources:
        if not isinstance(source, dict):
            raise PatchIdentityError('补丁身份记录不完整。')
        for key, target in [('dataset', datasets), ('instance_id', instances), ('case_key', cases)]:
            if source.get(key):
                if not isinstance(source[key], str):
                    raise PatchIdentityError('补丁身份记录格式无效。')
                target.add(source[key])
        if isinstance(source.get('benchmark_snapshot'), dict):
            snapshot = source['benchmark_snapshot']
            snapshots.append(snapshot)
            for key in ('dataset', 'id'):
                if snapshot.get(key):
                    datasets.add(snapshot[key])
            if snapshot.get('source_revision'):
                revisions.add(snapshot['source_revision'])
    for case in cases:
        source_dataset, separator, source_instance = case.partition(':')
        if not separator or not source_dataset or not source_instance:
            raise PatchIdentityError('补丁题目身份无效。')
        datasets.add(source_dataset)
        instances.add(source_instance)
    if instances and instances != {instance}:
        raise PatchIdentityError('补丁所属题目与当前实例不一致。')
    if len(datasets) > 1 or (dataset and datasets and datasets != {dataset}):
        raise PatchIdentityError('补丁所属基准或固定版本与当前题目不一致。')
    if len(revisions) > 1 or any(not revision_matches(dataset,revision) for revision in revisions):
        raise PatchIdentityError('补丁记录的数据快照版本与当前基准不一致。')
    if dataset and dataset not in {'verified', 'lite'} and datasets != {dataset}:
        raise PatchIdentityError('这份补丁未注明基准版本；请使用本题求解记录，或在文件中明确填写 dataset 与 instance_id。')
    if not dataset and any(value not in {'verified', 'lite'} for value in datasets):
        raise PatchIdentityError('此补丁属于其他基准版本，请先选择相同基准。')
    identified_dataset = next(iter(datasets), dataset)
    if identified_dataset in REGISTRY and REGISTRY[identified_dataset].get('source_revision'):
        expected=current_snapshot(identified_dataset)
        if any(key in expected and value not in (None,'') and value!=expected[key]
               for saved in snapshots for key,value in saved.items()):
            raise PatchIdentityError('补丁记录的数据或评测器快照与当前基准不一致。')
    return {'dataset': identified_dataset, 'case_key': f'{identified_dataset}:{instance}' if identified_dataset else '',
            'benchmark_snapshot': snapshot, 'recorded_instance': instance if instances else ''}


def inspect_patch(path: Path, instance: str, recorded_instance: str | dict = '', *, dataset: str = '') -> dict:
    valid_instance(instance)
    result = {'path': str(path), 'name': path.name, 'usable': False, 'reason': '', 'instance_id': '', 'association': 'unknown'}
    try:
        if not path.name.endswith(('.traj.json', '.jsonl')):
            raise ValueError('请选择轨迹 .traj.json 或预测 .jsonl 文件。')
        if path.name.endswith('.traj.json') and path.is_file() and path.stat().st_size <= 20_000_000:
            record=read_json(path)
            info = record.get('info', {})
            if info.get('exit_status') != 'Submitted' and not captured_pro_patch(record):
                return result | {'reason': '任务没有成功提交补丁。'}
            if not isinstance(info.get('submission'), str) or not info['submission'].strip():
                return result | {'reason': '这是一份检查记录，没有代码补丁。'}
        pred = prediction(path, instance)
        identity = _patch_identity(path, instance, recorded_instance, dataset)
        patch = pred['model_patch']
        if not re.search(r'^diff --git ', patch, re.M) or not re.search(r'^(@@ |GIT binary patch$|new file mode |deleted file mode |old mode |rename from |Binary files )', patch, re.M):
            raise ValueError('提交内容不是可识别的代码补丁，不能作为本题评测输入。')
        association = 'record'
        if path.name.endswith('.traj.json'):
            data = read_json(path)
            identified = identity['recorded_instance']
            if identified and identified != instance:
                raise ValueError('轨迹所属题目与当前实例不一致。')
            if not identified:
                association = 'filename' if path.name == instance + '.traj.json' else 'unknown'
        result.update(usable=True, instance_id=instance if association != 'unknown' else '', association=association,
                      dataset=identity['dataset'], case_key=identity['case_key'], benchmark_snapshot=identity['benchmark_snapshot'],
                      model=pred['model_name_or_path'], patch_hash=fingerprint(patch),
                      files=re.findall(r'^diff --git a/(.*?) b/.*$', patch, re.M),
                      modified=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                      format='trajectory' if path.name.endswith('.traj.json') else 'prediction',
                      note={'record': '题目编号已核对', 'filename': '旧轨迹无内置题号，按文件名关联', 'unknown': '轨迹没有题号，需自行确认属于本题'}[association])
    except PatchIdentityError as exc:
        result['reason'] = str(exc)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Do not expose JSON fragments or arbitrary private file content in errors.
        result['reason'] = '无已提交代码补丁、题号不匹配，或文件不完整；不会自动选用。'
    return result


def source_paths(records: list[dict], legacy_root: Path | None = None, *, dataset: str = '') -> list[tuple[Path, str | dict]]:
    root = legacy_root or CONFIG_ROOT / '.local'
    paths = [(p, '') for p in sorted([*root.glob('*.traj.json'), *root.glob('*.predictions.jsonl')])[:100]]
    for run in sorted(records, key=lambda r: r['created'], reverse=True)[:100]:
        if run['kind'] != 'swebench' or (dataset and run.get('dataset') and run['dataset'] != dataset):
            continue
        folder = job_dir(run['id'])
        identity = {key: run[key] for key in ('id', 'kind', 'solve_run_id', 'attempt_id', 'instance_id', 'dataset', 'case_key', 'benchmark_snapshot') if key in run}
        paths.extend((folder / name, identity) for name in ['predictions.jsonl', 'trajectory.traj.json'] if (folder / name).is_file())
    return paths


def catalog(instance: str, paths: list[tuple[Path, str | dict]], *, dataset: str = '') -> dict:
    inspected = [inspect_patch(path, instance, identity, dataset=dataset) for path, identity in paths]
    groups = {}
    rejected = []
    for item in inspected:
        if not item['usable'] or item['association'] == 'unknown':
            rejected.append({'name': item['name'], 'reason': item.get('note') or item['reason']})
            continue
        groups.setdefault((item['patch_hash'], item['model']), []).append(item)
    items = []
    for matches in groups.values():
        matches.sort(key=lambda x: (x['association'] == 'record', x['format'] == 'prediction', x['modified']), reverse=True)
        items.append(matches[0] | {'copies': len(matches), 'has_conversation': any(x['format'] == 'trajectory' for x in matches),
                                  'alternatives': [{'name': x['name'], 'path': x['path']} for x in matches[1:]]})
    items.sort(key=lambda x: x['modified'], reverse=True)
    return {'items': items, 'rejected': rejected, 'recommended': items[0]['path'] if len(items) == 1 else None,
            'note': '同内容且同来源模型的补丁已合并；不同模型保留独立选项。推荐不代表测试通过。检索旧目录前 100 个文件及最近 100 条工作台记录。'}


def conversation_for(path: Path, instance: str, paths: list[tuple[Path, str | dict]], before: int | None = None, limit: int = 50, *, dataset: str = '', source: dict | None = None) -> dict:
    from .timeline import message_preview
    selected_identity = next((identity for candidate, identity in paths if candidate == path), '')
    selected = inspect_patch(path, instance, selected_identity, dataset=dataset)
    if not selected['usable']:
        return {'messages': [], 'note': '评测输入无法读取，未猜测或关联其他对话。'}
    origin = (selected_identity if isinstance(selected_identity, dict) else {}) | (source or {})
    explicit = origin.get('solve_run_id') or (origin.get('id') if origin.get('kind') == 'swebench' else '')
    attempt = origin.get('attempt_id')
    original = Path(origin['patch_source']['path']) if origin.get('patch_source', {}).get('path') else path
    case = {key: origin[key] for key in ('instance_id', 'dataset', 'case_key', 'benchmark_snapshot') if key in origin}
    candidates = [(path, selected_identity)] if path.name.endswith('.traj.json') else []
    candidates += [(p, identity) for p, identity in paths if p.name.endswith('.traj.json') and p != path]
    if explicit and re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}', explicit):
        candidates.append((job_dir(explicit) / 'trajectory.traj.json', case))
    if original.name.endswith('.traj.json'):
        candidates.append((original, case))
    elif origin.get('patch_source', {}).get('path'):
        candidates += [(original.with_name('trajectory.traj.json'), case),
                       (original.with_name(original.name.removesuffix('.predictions.jsonl') + '.traj.json'), case)]
    matches, seen = [], set()
    for candidate, identity in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        identity = identity if isinstance(identity, dict) else {'instance_id': identity}
        if explicit and explicit not in {identity.get('solve_run_id'), identity.get('id'), candidate.parent.name}:
            continue
        if attempt and identity.get('attempt_id') and attempt != identity['attempt_id']:
            continue
        inspected = inspect_patch(candidate, instance, identity, dataset=dataset)
        if not inspected['usable'] or inspected['association'] == 'unknown' or inspected['patch_hash'] != selected['patch_hash'] or inspected['model'] != selected['model']:
            continue
        matches.append((candidate, identity))
    if path.name.endswith('.traj.json'):
        matches = [item for item in matches if item[0] == path]
    elif not explicit:
        if attempt:
            matches = [item for item in matches if item[1].get('attempt_id') == attempt]
        elif original.name.endswith('.traj.json'):
            matches = [item for item in matches if item[0] == original]
        elif origin.get('patch_source', {}).get('path'):
            matches = [item for item in matches if item[0].parent == original.parent]
        else:
            local = [item for item in matches if item[0].parent == path.parent]
            matches = local or matches
    if len(matches) != 1:
        return {'messages': [], 'association': 'ambiguous' if len(matches) > 1 else 'missing',
                'note': ('来源待确认：多轮求解生成了相同补丁，现有记录无法确定原始对话；不会选取最新一份。'
                         if len(matches) > 1 else '来源待确认：未找到与已记录来源、题目、补丁及模型一致的轨迹；不会改用其他求解对话。可在“评测进度”查看本次测试输出。')}
    candidate, identity = matches[0]
    try:
        data = read_json(candidate)
    except (OSError, ValueError):
        return {'messages': [], 'association': 'missing', 'note': '原始对话暂时无法读取；未改用其他求解对话。'}
    messages = data.get('messages', []) if isinstance(data, dict) else []
    messages = [m for m in messages if isinstance(m, dict) and m.get('role', m.get('type')) in {'user', 'assistant', 'tool', 'exit', 'message', 'function_call', 'function_call_output'}] if isinstance(messages, list) else []
    end = len(messages) if before is None else max(0, min(before, len(messages)))
    start = max(0, end - max(1, min(limit, 50)))
    page = [message_preview(m) for m in messages[start:end]]
    truncated = any(m['display_truncated'] for m in page)
    return {'messages': page, 'source': candidate.name, 'association': 'matched',
            'source_run_id': identity.get('solve_run_id') or identity.get('id') or explicit or None,
            'source_attempt_id': identity.get('attempt_id'),
            'truncated': truncated, 'has_older': start > 0, 'before': start, 'total': len(messages),
            'note': '以下为与本次补丁及来源模型一致的已保存对话，不是本次评测的新回复。' + ('此轨迹没有可展示消息。' if not messages else '') + ('长消息显示节选，完整内容保留在原轨迹文件。' if truncated else '')}
