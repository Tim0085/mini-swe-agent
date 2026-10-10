"""Registered benchmark identities; fixed snapshots never silently move to a new release."""
from typing import Annotated

from pydantic import AfterValidator

REBENCH_ID = 'rebench-34d5a588'
REBENCH_REVISION = '34d5a58864acf91613740a09ec5d205228dcfa39'
REBENCH_EVALUATOR = 'e4907b7a90eafaa1f0a6428fd04fe31cdd8b4284'
PRO_ID = 'pro-v2-2d52cb3d'
PRO_REVISION = '2d52cb3df914a3fcf80c7f66738b3a88ae37fc50'
PRO_EVALUATOR = '66f92766bba642462d4bbe5479e83f91f9211862'
PRO_INSTANCE_TEMPLATE = '''{{task}}

You are working in {{cwd}} inside the task's offline Linux container. Use bash tool calls
to inspect, modify, and test the repository. Commands run in separate shells. Network
access is disabled. The evaluator runs separately and is not available in this container.
Follow the task instructions above. Do not commit your changes or create a patch file in
the repository: the workbench captures all tracked and unignored new files from Git.
Remove temporary debugging files before finishing. When finished, execute:
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
Issue this as a separate command. You cannot continue working after submitting.
'''
REGISTRY = {
    'verified': {'id':'verified','label':'SWE-bench Verified','benchmark':'swebench',
                 'source':'SWE-bench/SWE-bench_Verified','source_revision':None,'config':'default','split':'test',
                 'description':'现有 Verified 题集，原有缓存与历史保持兼容。','license':'MIT / upstream repository licenses'},
    'lite': {'id':'lite','label':'SWE-bench Lite','benchmark':'swebench',
             'source':'SWE-bench/SWE-bench_Lite','source_revision':None,'config':'default','split':'test',
             'description':'现有 Lite 题集，原有缓存与历史保持兼容。','license':'MIT / upstream repository licenses'},
    REBENCH_ID: {'id':REBENCH_ID,'label':'SWE-rebench 经典榜单','benchmark':'swe-rebench-classic',
                 'source':'nebius/SWE-rebench-leaderboard','source_revision':REBENCH_REVISION,'config':'default','split':'test',
                 'description':'860 题固定快照。同步或获取未缓存题目会读取约 4.86 MB 的官方数据文件；镜像另行按需下载。不是新版 Harbor 多语言题集。',
                 'license':'CC BY 4.0 / upstream repository licenses','evaluator':'swe-rebench-fork',
                 'evaluator_revision':REBENCH_EVALUATOR,'adapter_version':1,'expected_count':860},
    PRO_ID: {'id':PRO_ID,'label':'SWE-bench Pro V2（公开题集）','benchmark':'swe-bench-pro-v2',
             'source':'ScaleAI/SWE-bench_Pro','source_revision':PRO_REVISION,'config':'default','split':'test',
             'description':'642 题固定公开快照。题目数据约 6.51 MB，所选题目的官方说明及评测资料另行按需缓存。求解容器断网，评测使用全新容器。',
             'license':'MIT verifier / upstream repository licenses','evaluator':'swe-bench-pro-v2-verifier',
             'evaluator_revision':PRO_EVALUATOR,'adapter_version':1,'expected_count':642,
             'capture_partial':True,'agent_network':'none','fresh_evaluation':True,'task_time_limit':3000},
}


def validate_dataset(value: str) -> str:
    if value not in REGISTRY:
        raise ValueError('未知的基准或版本，请从题集列表重新选择。')
    return value


DatasetId = Annotated[str, AfterValidator(validate_dataset)]


def is_rebench(dataset: str) -> bool:
    return validate_dataset(dataset) == REBENCH_ID


def is_pro(dataset: str) -> bool:
    return validate_dataset(dataset) == PRO_ID


def is_pinned(dataset: str) -> bool:
    return bool(REGISTRY[validate_dataset(dataset)].get('source_revision'))


def revision_matches(dataset: str, revision: str) -> bool:
    expected=REGISTRY.get(dataset,{}).get('source_revision')
    return not expected or revision==expected


def snapshot(dataset: str) -> dict:
    spec = REGISTRY[validate_dataset(dataset)]
    value = {key:spec[key] for key in ('id','benchmark','source','source_revision','config','split','evaluator','evaluator_revision','adapter_version') if key in spec} | {'dataset':dataset}
    if dataset == PRO_ID:
        value['execution_config'] = {'instance_template': PRO_INSTANCE_TEMPLATE, 'wall_time_limit_seconds': 3000,
                                     'agent_network': 'none', 'fresh_evaluation': True, 'patch_source': 'container_git_diff',
                                     'cpus': 1, 'memory_mb': 4096, 'storage_quota_enforced': False}
    return value


def check_snapshot(dataset: str, frozen: dict | None) -> None:
    if is_pinned(dataset) and frozen != snapshot(dataset):
        raise ValueError('基准数据或评测器版本已变化或未冻结，请重新建立任务；未自动继续求解。')


def case_key(dataset: str, instance: str) -> str:
    return validate_dataset(dataset) + ':' + instance
