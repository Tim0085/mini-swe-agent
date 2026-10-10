import importlib.metadata
import re
import subprocess
from pathlib import Path

from .common import ROOT, STATE, read_json, save_json
from .benchmarks import PRO_ID,REGISTRY,case_key,check_snapshot,is_pinned,is_pro,is_rebench,snapshot,validate_dataset

DATASETS = {key:spec['source'] for key,spec in REGISTRY.items()}


def valid_instance(value: str, dataset: str = '') -> str:
    if dataset==PRO_ID or (not dataset and isinstance(value,str) and value.startswith('instance_')):
        if not isinstance(value,str) or len(value)>150 or '..' in value or not re.fullmatch(r'instance_[A-Za-z0-9_.-]+',value):
            raise ValueError('Pro 实例编号无效，请从实例库搜索并选择完整题号。')
        return value
    suffix = r'(?:_interface)?' if not dataset or is_rebench(dataset) else ''
    if not isinstance(value,str) or len(value)>150 or not re.fullmatch(r'[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+-\d+'+suffix, value):
        raise ValueError('实例编号格式无效，例如 django__django-10097。')
    return value


def dataset_file(dataset: str, instance: str) -> Path:
    validate_dataset(dataset)
    valid_instance(instance,dataset)
    cached = STATE / 'datasets' / dataset / (instance + '.json')
    legacy = ROOT / '.local/swebench-eval-data' / (instance + '-v5.json')
    return legacy if dataset == 'verified' and legacy.is_file() else cached


def load_instance(dataset: str, instance: str, download=False, emit=None) -> dict:
    path = dataset_file(dataset, instance)
    if is_pro(dataset):
        return _load_pro_instance(path,dataset,instance,download,emit)
    if not path.is_file():
        if not download:
            raise ValueError('本题尚未缓存。勾选允许下载后再加载题目。')
        if is_pinned(dataset):
            if is_pro(dataset):
                from .pro_data import load_case
            else:
                from .rebench_data import load_case
            found=load_case(instance,emit)
            found.update(dataset=dataset,benchmark_snapshot=snapshot(dataset),case_key=case_key(dataset,instance))
            save_json(path,[found])
            return found
        from .library import page_rows,directory
        found = None
        indexed=next((x for x in directory(dataset)['items'] if x['instance_id']==instance),None)
        if indexed:
            candidate=page_rows(dataset,indexed['row_index'],1)['rows']
            if candidate and candidate[0]['row']['instance_id']==instance and not candidate[0].get('truncated_cells'):
                found=candidate[0]['row']
        offset=0
        while found is None:
            if emit:
                emit('phase',f'检索题目数据：第 {offset//100+1} 页；可停止此加载任务')
            data=page_rows(dataset,offset)
            entry=next((x for x in data['rows'] if x['row']['instance_id']==instance),None)
            if entry and entry.get('truncated_cells'):
                raise ValueError('官方接口截断了题目内容，未保存不完整资料。')
            found=entry['row'] if entry else None
            if found:
                break
            offset+=len(data['rows'])
            if not data['rows'] or offset>=data.get('num_rows_total',float('inf')) or len(data['rows'])<100:
                break
        if found is None:
            raise ValueError('数据集中未找到该编号，请检查数据集和实例 ID。')
        save_json(path, [found])
    records = read_json(path)
    row = next((x for x in records if x['instance_id'] == instance), None)
    if row is None:
        raise ValueError('缓存与实例编号不匹配。')
    if is_pinned(dataset):
        check_snapshot(dataset,row.get('benchmark_snapshot'))
        if row.get('dataset')!=dataset or row.get('case_key')!=case_key(dataset,instance):
            raise ValueError('缓存题目的基准身份不匹配，请重新获取本题。')
    if is_pro(dataset):
        from .pro_data import ensure_asset_bundle,check_asset_bundle
        (ensure_asset_bundle if download else check_asset_bundle)(row)
    return row


def _load_pro_instance(path: Path,dataset: str,instance: str,download: bool,emit=None) -> dict:
    from .pro_data import GH_REVISION,check_asset_bundle,ensure_asset_bundle,load_case,normalize_case,public_case,solver_input
    row=None
    if path.is_file():
        try:
            records=read_json(path)
            if not isinstance(records,list) or len(records)!=1 or not isinstance(records[0],dict):
                raise ValueError('Pro 缓存资料格式无效，请重新缓存题目。')
            row=records[0]
            check_snapshot(dataset,row.get('benchmark_snapshot'))
            if (row.get('instance_id')!=instance or row.get('dataset')!=dataset or
                    row.get('case_key')!=case_key(dataset,instance)):
                raise ValueError('Pro 缓存题目的身份不匹配，请重新缓存题目。')
            public_case(row)
            if row.get('asset_revision')!=GH_REVISION or row.get('docker_image')!='ghcr.io/scaleapi/swe-bench_pro-v2:'+instance:
                raise ValueError('Pro 缓存资料来源不匹配，请重新缓存题目。')
        except (OSError,ValueError,TypeError,KeyError):
            if not download:
                raise ValueError('Pro 缓存资料无效或版本不匹配，请允许下载后重新缓存题目。') from None
            row=None
    if row is None:
        if not download:
            raise ValueError('本题尚未缓存。勾选允许下载后再加载题目。')
        row=load_case(instance,emit)
        row.update(dataset=dataset,benchmark_snapshot=snapshot(dataset),case_key=case_key(dataset,instance))
        save_json(path,[row])
        return row
    paths=(ensure_asset_bundle(row,emit) if download else check_asset_bundle(row))
    normalized=normalize_case(row,paths)
    normalized.update(dataset=dataset,benchmark_snapshot=snapshot(dataset),case_key=case_key(dataset,instance))
    solver_input(normalized)
    if normalized!=row:
        if not download:
            raise ValueError('Pro 缓存说明或运行参数与官方资料不一致，请重新缓存题目。')
        save_json(path,[normalized])
    return normalized


def docker(*args: str, timeout=30) -> subprocess.CompletedProcess:
    from .docker_runtime import check_docker_result
    return check_docker_result(subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdin=subprocess.DEVNULL))


def image_name(row: dict, dataset: str = 'verified') -> str:
    if is_pro(dataset):
        expected='ghcr.io/scaleapi/swe-bench_pro-v2:'+valid_instance(row['instance_id'],dataset)
        if row.get('docker_image')!=expected:
            raise ValueError('Pro V2 镜像与题目不匹配；不能使用旧 V1 或其他来源镜像。')
        return expected
    namespace='swerebench' if is_rebench(dataset) else 'swebench'
    value = (row.get('image_name') or row.get('docker_image')) if is_rebench(dataset) else row.get('image')
    value = value or (namespace+'/sweb.eval.x86_64.' + row['instance_id'].replace('__', '_1776_').lower() + ':latest')
    if not isinstance(value,str) or not re.fullmatch(r'(?:docker.io/)?'+namespace+r'/[a-z0-9._/-]+(?::[a-zA-Z0-9._-]+)?(?:@sha256:[a-f0-9]{64})?', value):
        raise ValueError('镜像来源与所选基准不符；只允许该基准的官方镜像。')
    return value


def preview(dataset: str, instance: str, download=False) -> dict:
    row = load_instance(dataset, instance, download)
    image = image_name(row,dataset)
    try:
        available = docker('info', '--format', '{{.OSType}}',timeout=5).stdout.strip() == 'linux'
        image_info = docker('image', 'inspect', image, '--format', '{{.Id}}|{{.Size}}',timeout=5) if available else None
    except (OSError,subprocess.TimeoutExpired):
        available,image_info=False,None
    cached=image_info is not None and image_info.returncode==0
    return {k: row.get(k, '') for k in ['instance_id', 'problem_statement', 'repo', 'base_commit', 'difficulty']} | {
        'problem_statement': problem_description(row,dataset),
        'image': image, 'docker_ready': available, 'image_cached': cached,
        'image_bytes': int(image_info.stdout.strip().split('|')[-1]) if cached else None,
        'image_id':image_info.stdout.strip().split('|')[0] if cached else None,
        'dataset': dataset,
    }


def problem_description(row: dict, dataset: str) -> str:
    if is_pro(dataset):
        from .pro_data import solver_input
        return solver_input(row)
    if is_rebench(dataset):
        from .rebench_data import solver_input
        return solver_input(row)
    return row['problem_statement']


def ensure_image(row: dict, allow_download: bool, emit, dataset: str = 'verified', expected_image_id: str | None=None) -> str:
    if docker('info', '--format', '{{.OSType}}').stdout.strip() != 'linux':
        raise ValueError('请先启动 Docker Desktop，并切换到 Linux 容器。')
    image = image_name(row,dataset)
    if docker('image', 'inspect', image).returncode:
        if not allow_download:
            raise ValueError('镜像未缓存；可能占用数 GB。确认允许下载后重新开始。')
        emit('log', '正在下载官方题目镜像，实际体积由镜像决定。')
        from .runtime import stream_command
        result = stream_command(['docker','pull',image],emit=emit,timeout=1800)
        if result['returncode']:
            raise ValueError('镜像下载失败：' + result['output'][-2000:])
    if expected_image_id:
        actual=docker('image','inspect',image,'--format','{{.Id}}')
        if actual.returncode or actual.stdout.strip()!=expected_image_id:
            raise ValueError('本题镜像在准备后发生变化，已停止派发。请重新准备题目环境。')
        return expected_image_id
    return image


def check_harness(row: dict, dataset: str = 'verified') -> None:
    if is_pro(dataset):
        from .pro_harness import check_harness as check_pro
        return check_pro(row)
    if is_rebench(dataset):
        from .rebench_harness import check_harness as check_rebench
        return check_rebench(row)
    if importlib.metadata.version('swebench') != '5.0.2':
        raise ValueError('当前评测接入已验证 SWE-bench 5.0.2；版本变化后请先检查兼容性。')
    if not all(k in row for k in ['eval_script', 'image', 'log_parser', 'eval_type', 'FAIL_TO_PASS', 'PASS_TO_PASS']):
        raise ValueError('题目缺少 SWE-bench 5 评测字段，请重新获取官方数据。')
    location = Path(importlib.metadata.distribution('swebench').locate_file('swebench/harness/run_evaluation.py'))
    source = location.read_text(encoding='utf-8')
    for prefix in ['patch_file.write_text(', 'eval_file.write_text(']:
        line = next((line for line in source.splitlines() if prefix in line), '')
        if 'newline="\\n"' not in line:
            raise ValueError('Windows LF 修复缺失，请按操作说明运行已有修复脚本后重试。')


def captured_pro_patch(record: dict) -> bool:
    info=record.get('info',{})
    capture=info.get('patch_capture',{})
    return ((record.get('dataset') or info.get('dataset'))==PRO_ID and capture.get('source')=='container_git_diff'
            and capture.get('captured') is True and isinstance(info.get('submission'),str) and bool(info['submission'].strip()))


def prediction(path: Path, instance: str) -> dict:
    valid_instance(instance)
    if not path.is_file() or path.stat().st_size > 20_000_000:
        raise ValueError('请选择不超过 20 MB 的轨迹或预测文件。')
    if path.name.endswith('.traj.json'):
        record = read_json(path)
        if not isinstance(record,dict) or not isinstance(record.get('info'),dict):
            raise ValueError('轨迹缺少有效 info 对象，请选择完整的 mini 轨迹。')
        info = record.get('info', {})
        if info.get('exit_status') != 'Submitted' and not captured_pro_patch(record):
            raise ValueError('轨迹没有成功提交，不能直接导出预测。')
        recorded_id = record.get('instance_id') or info.get('instance_id')
        if recorded_id and recorded_id != instance:
            raise ValueError('轨迹中的实例编号与所选题目不一致。')
        config=info.get('config',{})
        model=config.get('model',{}) if isinstance(config,dict) else {}
        if not isinstance(model,dict) or not isinstance(model.get('model_name'),str):
            raise ValueError('轨迹没有有效模型名称，请选择完整轨迹或标准预测文件。')
        result = {'instance_id': instance, 'model_name_or_path': model['model_name'], 'model_patch': info.get('submission', '')}
        identity=record | info
    else:
        import json
        records = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if any(not isinstance(x,dict) for x in records):
            raise ValueError('预测文件每一行必须是 JSON 对象。')
        matched = [x for x in records if x.get('instance_id') == instance]
        if len(matched) != 1:
            raise ValueError('预测文件必须包含且只能包含一条匹配该题目的记录。')
        if not all(k in matched[0] for k in ['instance_id','model_name_or_path','model_patch']):
            raise ValueError('预测文件缺少实例、模型名称或补丁字段。')
        result = {k: matched[0][k] for k in ['instance_id', 'model_name_or_path', 'model_patch']}
        identity=matched[0]
    for key in ('dataset','benchmark_snapshot','case_key'):
        if key in identity:result[key]=identity[key]
    if not isinstance(result['model_patch'], str) or not result['model_patch'].strip():
        raise ValueError('提交的补丁为空。')
    if not isinstance(result['model_name_or_path'], str) or not re.fullmatch(r'[A-Za-z0-9_./:@+-]+', result['model_name_or_path']) or '..' in result['model_name_or_path']:
        raise ValueError('预测文件中的模型名不适合用作评测输出标识。')
    result['model_patch'] = result['model_patch'].replace('\r\n', '\n')
    return result


def summarize_report(report: dict) -> dict:
    resolved = report.get('resolved_instances', 0)
    errors = report.get('error_instances', 0)
    completed = report.get('completed_instances', 0)
    infrastructure = report.get('infra_failure_instances', 0)
    ambiguous = report.get('ambiguous_failure_instances', 0)
    category = 'passed' if resolved == 1 else 'infrastructure' if infrastructure else 'ambiguous' if ambiguous else 'incomplete' if errors or not completed else 'test_failed'
    verdicts = {'passed': '通过', 'infrastructure': '疑似环境失败，不能直接归因于补丁',
                'ambiguous': '失败原因不明确，需要查看日志', 'incomplete': '评测异常或未完成', 'test_failed': '测试未通过'}
    return {'resolved': resolved, 'errors': errors, 'completed': completed,
            'unresolved': report.get('unresolved_instances', 0), 'infrastructure': infrastructure, 'ambiguous': ambiguous,
            'failure_reasons': report.get('failure_reasons', {}), 'category': category, 'verdict': verdicts[category]}
