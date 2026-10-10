"""Persistent sequential batches using the workbench's single worker slot."""
import hashlib
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,ValidationError

from .bench import prediction,valid_instance
from .benchmarks import DatasetId,is_pinned,snapshot as benchmark_snapshot,check_snapshot as check_benchmark_snapshot
from .common import CONFIG_ROOT,ROOT,STATE,TERMINAL,job_dir,new_id,now,read_json,recover_json,save_json
from .library import check_metadata_space,image_inventory,readiness
from .settings import Settings,inspect_settings

DONE={'completed','cancelled'}
ITEM_DONE={'completed','cancelled','skipped'}


class BatchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    dataset: DatasetId='verified'
    instance_ids: list[str]=Field(min_length=1,max_length=50)
    operation: Literal['cache','download','solve','evaluate']
    predictions: dict[str,str]={}
    allow_data_download: bool=False
    allow_image_download: bool=False
    auto_evaluate: bool=False
    auto_approve: bool=False
    total_budget: float=Field(default=3,gt=0,le=50000,allow_inf_nan=False)
    settings_version: str
    profile_id: str | None=None
    diagnostics: dict[str,Literal['success','failure','test_failed','unknown_cost','slow']]={}


class BatchCommit(BaseModel):
    token: str
    request_id: str=Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')


class BatchControl(BaseModel):
    action: Literal['pause','stop','resume','retry','skip','cancel_item','finish']
    index: int | None=None
    accepted_settings_version: str | None=None
    accepted_profile_versions: dict[str,str]={}


class ExperimentRequest(BatchRequest):
    operation: Literal['solve']='solve'
    name: str=Field(default='',max_length=120)
    profile_ids: list[str]=Field(min_length=1,max_length=5)
    step_limit: int=Field(default=40,ge=1,le=1000)
    cost_limit: float=Field(default=3,gt=0,le=1000,allow_inf_nan=False)
    timeout: int=Field(default=120,ge=5,le=3600)
    auto_evaluate: bool=True


class SavedAttempt(BaseModel):
    model_config=ConfigDict(extra='allow',strict=True)
    stage: Literal['cache','download','solve','evaluate']
    request_id: str=Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    run_id: str | None
    status: Literal['starting','completed','failed','cancelled','interrupted','cleanup_required']
    cost: float=Field(ge=0,allow_inf_nan=False)


class SavedItem(BaseModel):
    model_config=ConfigDict(extra='allow',strict=True)
    instance_id: str
    stages: list[Literal['cache','download','solve','evaluate']]=Field(min_length=1,max_length=3)
    stage_index: int=Field(ge=0)
    status: Literal['pending','running','completed','failed','interrupted','skipped','cancelled']
    attempts: list[SavedAttempt]
    retry: int=Field(ge=0)
    prediction_path: str
    error: str
    profile_id: str | None=None
    evaluation: dict | None=None


class SavedQueue(BaseModel):
    model_config=ConfigDict(extra='allow',strict=True)
    id: str
    request_id: str=Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    dataset: DatasetId
    operation: Literal['cache','download','solve','evaluate']
    status: Literal['running','pause_pending','paused','completed','cancelled']
    created: str
    items: list[SavedItem]=Field(min_length=1,max_length=50)
    current_run: str | None
    current_index: int | None
    reason: str
    settings_version: str
    settings: dict
    allow_data_download: bool
    allow_image_download: bool
    auto_approve: bool
    auto_evaluate: bool
    total_budget: float=Field(gt=0,allow_inf_nan=False)
    diagnostics: dict[str,Literal['success','failure','test_failed','unknown_cost','slow']]
    billing_unknown: bool
    profile_snapshots: dict[str,dict]={}
    kind: Literal['batch','workflow','experiment']='batch'

    def check_links(self):
        job_dir(self.id)
        Settings.model_validate(self.settings)
        if self.current_index is not None and not 0<=self.current_index<len(self.items):raise ValueError('队列当前条目无效')
        if self.current_run:
            job_dir(self.current_run)
            if self.current_index is None:raise ValueError('队列缺少当前条目')
            attempts=self.items[self.current_index].attempts
            if not attempts or attempts[-1].run_id!=self.current_run:raise ValueError('队列缺少当前任务关联')
        for item in self.items:
            valid_instance(item.instance_id,self.dataset)
            if item.stage_index>len(item.stages) or (item.status not in ITEM_DONE and item.stage_index==len(item.stages)):
                raise ValueError('队列步骤索引无效')
            for attempt in item.attempts:
                if attempt.run_id:job_dir(attempt.run_id)
        for profile_id,snapshot in self.profile_snapshots.items():
            names=['profile_id','profile_name','profile_version','credential_version','model_name','api_base','protocol','configuration_fingerprint','fingerprint']
            if any(not isinstance(snapshot.get(name),str) for name in names) or snapshot['profile_id']!=profile_id:
                raise ValueError('模型快照标识无效')
            if any(not isinstance(snapshot.get(name),dict) for name in ['limits','parameters','pricing','runtime','configs']):
                raise ValueError('模型快照结构无效')


def price_available(model_name: str) -> bool:
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
    import litellm
    info=litellm.model_cost.get(model_name) or litellm.model_cost.get(model_name.removeprefix('openai/')) or {}
    return all(isinstance(info.get(k),(int,float)) and math.isfinite(info[k]) and info[k]>0 for k in ['input_cost_per_token','output_cost_per_token'])


def resource_issues(request: BatchRequest,row: dict) -> list[str]:
    problems=[]
    if not row['data_cached'] and not request.allow_data_download:problems.append('未授权下载题目资料')
    if request.operation!='cache' and not request.diagnostics:
        if row['environment']=='unknown':problems.append('Docker 无法检查')
        elif row['environment']=='missing' and not request.allow_image_download:problems.append('未授权下载缺失镜像')
    return problems


class Queues:
    def __init__(self,manager):
        self.manager=manager
        self.records={}
        self.previews={}
        self.stop_event=threading.Event()
        self.thread=None
        self.recovery_issue=''
        self.invalid_records=[]

    def folder(self,queue_id):
        job_dir(queue_id)
        return STATE/'queues'/queue_id

    def store(self,q):
        q['updated']=now()
        save_json(self.folder(q['id'])/'queue.json',q)
        self.records[q['id']]=q
        self.refresh_recovery_issue()

    def refresh_recovery_issue(self):
        if self.invalid_records:
            self.recovery_issue='存在损坏批次或无法保存的记录，已停止派发并阻止创建新批次；请保留记录并检查：'+', '.join(self.invalid_records)
        elif sum(q['status'] not in DONE for q in self.records.values())>1:
            self.recovery_issue='存在多个未结束批次，请先结束多余批次；未自动执行。'
        else:
            self.recovery_issue=''

    def pause_record(self,q,reason):
        q.update(status='paused',reason=reason)
        self.records[q['id']]=q
        try:
            self.store(q)
        except OSError:
            if q['id'] not in self.invalid_records:self.invalid_records.append(q['id'])
            self.refresh_recovery_issue()

    def unfinished(self):
        return next((q for q in self.records.values() if q['status'] not in DONE),None)

    def owns_slot(self):
        q=self.unfinished()
        return bool(q and (q['status'] in {'running','pause_pending'} or q.get('current_run')))

    def protected_runs(self):
        return {a['run_id'] for q in self.records.values() for item in q['items'] for a in item['attempts'] if a.get('run_id') and
                (q['status'] not in DONE or (q.get('kind')=='experiment' and item.get('first_attempt_id') and a.get('attempt_id')==item['first_attempt_id']))}

    def get(self,queue_id):
        if queue_id not in self.records:raise ValueError('未找到批次。')
        return self.records[queue_id]

    def view(self,q):
        value=json.loads(json.dumps(q))
        value.pop('diagnostics',None)
        value['estimated_cost']=self.spent(q)
        value['count']=len(q['items'])
        value['completed']=sum(item['status']=='completed' for item in q['items'])
        current=inspect_settings()
        value['configuration_changed']=q['settings_version']!=current['version']
        value['current_settings_version']=current['version']
        value['current_settings']={k:current[k] for k in Settings.model_fields}
        if q.get('profile_snapshots'):
            from .profiles import check_snapshot,freeze_profile
            changes={key:check_snapshot(snap) for key,snap in q['profile_snapshots'].items()}
            value['profile_changes']=changes
            value['configuration_changed']=any(x['changed'] for x in changes.values())
            value['current_profile_versions']={}
            for key,change in changes.items():
                if change['changed'] and change.get('credentials_only'):
                    value['current_profile_versions'][key]=freeze_profile(key)['credential_version']
        child=self.manager.records.get(q.get('current_run'))
        value['current_task']={k:child.get(k) for k in ['id','status','phase']} if child else None
        return value

    def spent(self,q):
        total=0.0
        for item in q['items']:
            for attempt in item['attempts']:
                if attempt.get('run_id'):
                    meta=self.manager.records.get(attempt['run_id'])
                    amounts=[(meta or {}).get('stats',{}).get('cost',0),(meta or {}).get('billing',{}).get('cost_floor',0),attempt.get('cost',0)]
                    total+=max([0,*[x for x in amounts if isinstance(x,(int,float)) and math.isfinite(x)]])
        return total

    def recover(self):
        self.records={}
        self.recovery_issue=''
        self.invalid_records=[]
        root=STATE/'queues'
        root.mkdir(parents=True,exist_ok=True)
        for path in root.glob('*/queue.json'):
            q=recover_json(path,None)
            try:
                SavedQueue.model_validate(q).check_links()
                if q['id']!=path.parent.name:raise ValueError('队列编号与目录不符')
            except (ValidationError,ValueError,TypeError):
                self.invalid_records.append(path.parent.name)
                continue
            self.records[q['id']]=q
            if q['status'] in DONE:continue
            for index,item in enumerate(q['items']):
                for attempt in item['attempts']:
                    if not attempt.get('run_id'):
                        attempt['run_id']=self.manager.request_ids.get(attempt['request_id'])
                if item['status']=='running' and item['attempts'] and item['attempts'][-1].get('run_id'):
                    q['current_run']=item['attempts'][-1]['run_id'];q['current_index']=index
            q['status']='paused';q['reason']='后台已重新启动，请核对后手动继续；中断题不会自动重跑。'
            if q.get('current_run'):
                child=self.manager.records.get(q['current_run'])
                if child and child['status'] in TERMINAL:
                    try:
                        self.consume(q,child)
                    except (OSError,ValueError,KeyError,TypeError):
                        q['items'][q['current_index']].update(status='failed',error='已保留任务记录，但无法恢复本次产物；请检查后明确重试或跳过。')
                        q['current_run']=None
                else:
                    item=q['items'][q['current_index']]
                    item['status']='interrupted';q['current_run']=None
            for index,item in enumerate(q['items']):
                if item['status']=='running' and not (q.get('current_run') and index==q['current_index']):item['status']='interrupted'
            if q['status'] not in DONE:self.pause_record(q,q['reason'])
            else:
                try:self.store(q)
                except OSError:
                    self.invalid_records.append(q['id'])
        self.refresh_recovery_issue()

    def start(self):
        self.stop_event.clear()
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.thread.start()

    def suspend(self):
        with self.manager.lock:
            q=self.unfinished()
            if q:
                self.pause_record(q,'后台退出或重启已请求，停止派发后续步骤。')

    def close(self):
        self.suspend();self.stop_event.set()
        if self.thread:self.thread.join(timeout=2)

    def preview(self,request: BatchRequest):
        if len(set(request.instance_ids))!=len(request.instance_ids):raise ValueError('同一批次不能重复选择题目。')
        if request.diagnostics and (os.getenv('MINI_WEB_TESTING')!='1' or CONFIG_ROOT==ROOT or STATE==ROOT/'.local/webui'):raise ValueError('离线队列诊断只允许隔离测试实例。')
        if request.diagnostics and set(request.diagnostics)!=set(request.instance_ids):raise ValueError('诊断批次必须为每一题指定离线场景，禁止混入真实模型任务。')
        settings=inspect_settings()
        profile_snapshots={}
        if request.operation=='solve' and (request.profile_id or isinstance(request,ExperimentRequest)):
            from .profiles import freeze_profile,snapshot_settings
            profile_ids=request.profile_ids if isinstance(request,ExperimentRequest) else [request.profile_id]
            if len(set(profile_ids))!=len(profile_ids):raise ValueError('不能重复选择相同模型档案。')
            if len(profile_ids)*len(request.instance_ids)>50:raise ValueError('模型数 × 题目数不能超过 50。')
            limits={key:getattr(request,key) for key in ('step_limit','cost_limit','timeout')} if isinstance(request,ExperimentRequest) else None
            profile_snapshots={key:freeze_profile(key,limits) for key in profile_ids}
            settings=settings | snapshot_settings(next(iter(profile_snapshots.values())))
        elif settings['version']!=request.settings_version:raise ValueError('设置已变化，请重新检查批次。')
        if request.operation!='solve' and (request.auto_evaluate or request.auto_approve):raise ValueError('只有求解批次可以授权自动执行或自动评测。')
        images=image_inventory()
        items=[];issues=[];snapshots={};total_bytes=0
        for instance in request.instance_ids:
            valid_instance(instance,request.dataset)
            row=readiness(request.dataset,instance,list(self.manager.records.values()),images)
            problems=resource_issues(request,row)
            selected=request.predictions.get(instance) or row['patches']['recommended']
            if request.operation=='evaluate':
                if not selected:problems.append('请选择一份本题补丁')
                else:
                    pred=prediction(Path(selected),instance)
                    from .patches import inspect_patch
                    if not inspect_patch(Path(selected),instance,dataset=request.dataset)['usable']:raise ValueError('选择的补丁不可评测。')
                    digest=hashlib.sha256(json.dumps(pred,sort_keys=True).encode()).hexdigest()
                    snapshots[instance]={'path':selected,'hash':digest}
                    total_bytes+=len(json.dumps(pred).encode())
            items.append({'instance_id':instance,'title':row['title'],'data_cached':row['data_cached'],'environment':row['environment'],
                          'image_size':row['image_size'],'prediction_path':selected if request.operation=='evaluate' else '',
                          'patches':row['patches']['items'],'issues':problems})
            issues.extend(f'{instance}：{message}' for message in problems)
        if request.operation=='solve' and not request.diagnostics:
            if profile_snapshots:
                from .profiles import profile_price_available,load_snapshot_secret
                for snap in profile_snapshots.values():
                    if not load_snapshot_secret(snap):issues.append(snap['profile_name']+'：密钥尚未就绪')
                    if not profile_price_available(snap):issues.append(snap['profile_name']+'：请填写可用模型价格')
            else:
                if not settings['valid'] or not settings['key_configured']:issues.append('模型配置或密钥尚未就绪')
                if not price_available(settings['model_name']):issues.append('本机没有可用模型计价，批量求解暂不可执行')
        if isinstance(request,ExperimentRequest):
            items=[row | {'profile_id':key,'profile_name':snap['profile_name']} for row in items for key,snap in profile_snapshots.items()]
        if total_bytes>20_000_000:issues.append('本批补丁快照超过 20 MB，请分批评测')
        check_metadata_space()
        token=secrets.token_hex(24)
        self.previews={k:v for k,v in self.previews.items() if v['expires']>time.monotonic()}
        self.previews[token]={'request':request.model_dump(),'snapshots':snapshots,'profile_snapshots':profile_snapshots,
                              'benchmark_snapshot':benchmark_snapshot(request.dataset) if is_pinned(request.dataset) else None,
                              'kind':'experiment' if isinstance(request,ExperimentRequest) else 'batch','expires':time.monotonic()+600,'issues':issues}
        return {'token':token,'items':items,'issues':issues,'ready':not issues,'model':settings['model_name'],
                'step_limit':settings['step_limit'],'per_item_budget':settings['cost_limit'],'total_budget':request.total_budget,
                'profiles':list(profile_snapshots.values()),'settings':{k:settings[k] for k in Settings.model_fields},
                'storage_note':images['storage_note'],'note':'费用是估算，单次请求可能超限；停止未知计价的后续调用。每题顺序执行。'}

    def commit(self,body: BatchCommit):
        with self.manager.lock:
            previous=next((q for q in self.records.values() if q['request_id']==body.request_id),None)
            if previous:return self.view(previous)
            if self.recovery_issue:raise ValueError(self.recovery_issue)
            if self.unfinished():raise ValueError('已有未完成批次，请先继续或结束它。')
            plan=self.previews.get(body.token)
            if not plan or plan['expires']<time.monotonic():raise ValueError('批次预览已过期，请重新预览。')
            if plan['issues']:raise ValueError('请先处理预览中的问题。')
            request=plan['request'];settings=inspect_settings()
            check_benchmark_snapshot(request['dataset'],plan.get('benchmark_snapshot'))
            profiles=plan.get('profile_snapshots',{})
            if profiles:
                from .profiles import check_snapshot,snapshot_settings
                if any(check_snapshot(s)['changed'] for s in profiles.values()):raise ValueError('模型配置已变化，请重新预览。')
                settings=settings | snapshot_settings(next(iter(profiles.values())))
            elif request['settings_version']!=settings['version']:raise ValueError('配置已变化，请重新预览。')
            check_metadata_space()
            snapshots={}
            for instance,entry in plan['snapshots'].items():
                pred=prediction(Path(entry['path']),instance)
                if hashlib.sha256(json.dumps(pred,sort_keys=True).encode()).hexdigest()!=entry['hash']:raise ValueError('补丁在预览后变化，请重新预览。')
                snapshots[instance]=pred
            queue_id=new_id();folder=self.folder(queue_id);folder.mkdir(parents=True)
            items=[]
            cells=[(instance,key) for instance in request['instance_ids'] for key in (list(profiles) or [None])]
            for instance,profile_id in cells:
                stages={'cache':['cache'],'download':['download'],'solve':['download','solve']+(['evaluate'] if request['auto_evaluate'] else []),'evaluate':['download','evaluate']}[request['operation']]
                item={'instance_id':instance,'stages':stages,'stage_index':0,'status':'pending','attempts':[],'retry':0,'prediction_path':'','error':''}
                attempt_id=new_id()
                item.update(profile_id=profile_id,profile_name=profiles.get(profile_id,{}).get('profile_name',settings['model_name']),
                            attempt_id=attempt_id,first_attempt_id=attempt_id,attempt_number=1,solve_run_id=None)
                if instance in snapshots:
                    path=folder/(instance+'.predictions.jsonl')
                    path.write_text(json.dumps(snapshots[instance],ensure_ascii=False)+'\n',encoding='utf-8',newline='\n')
                    item['prediction_path']=str(path)
                    item['prediction_hash']=hashlib.sha256(json.dumps(snapshots[instance],sort_keys=True).encode()).hexdigest()
                items.append(item)
            q={'id':queue_id,'request_id':body.request_id,'dataset':request['dataset'],'operation':request['operation'],
               'status':'running','created':now(),'items':items,'current_run':None,'current_index':None,'reason':'',
               'settings_version':settings['version'],'settings':{k:settings[k] for k in Settings.model_fields},
               'allow_data_download':request['allow_data_download'],'allow_image_download':request['allow_image_download'],
               'auto_approve':request['auto_approve'],'auto_evaluate':request['auto_evaluate'],'total_budget':request['total_budget'],
               'diagnostics':request['diagnostics'],'billing_unknown':False}
            if plan.get('benchmark_snapshot'):q['benchmark_snapshot']=plan['benchmark_snapshot']
            q.update(kind=plan.get('kind','batch'),name=request.get('name',''),profile_snapshots=profiles)
            self.store(q);self.previews.pop(body.token,None)
            return self.view(q)

    def consume(self,q,child):
        if child['status']=='cleanup_required' or child.get('recovery_pending'):
            reason='当前任务等待资源清理或状态恢复；处理后将复用已有结果，请勿重新求解。'
            if q['status']!='paused' or q.get('reason')!=reason:
                q.update(status='paused',reason=reason)
                self.store(q)
            return
        item=q['items'][q['current_index']]
        attempt=item['attempts'][-1]
        attempt.update(status=child['status'],cost=max(child.get('stats',{}).get('cost',0),child.get('billing',{}).get('cost_floor',0)))
        q['billing_unknown']=q.get('billing_unknown',False) or bool(child.get('billing',{}).get('pending') or child.get('billing',{}).get('unknown'))
        stage=item['stages'][item['stage_index']]
        if stage=='download' and child.get('prepared',{}).get('image_id'):
            item['image_id']=child['prepared']['image_id']
        success=child['status']=='completed'
        if stage=='evaluate' and child.get('evaluation',{}).get('category') not in {'passed','test_failed'}:success=False
        if stage=='solve' and success:
            item['solve_run_id']=child['id']
            produced=child.get('prediction')
            if not produced or Path(produced).resolve()!=(job_dir(child['id'])/'predictions.jsonl').resolve():success=False
            else:
                item['prediction_path']=produced
                submitted=prediction(Path(produced),item['instance_id'])
                if not submitted.get('model_patch','').strip():success=False
                item['prediction_hash']=hashlib.sha256(json.dumps(submitted,sort_keys=True).encode()).hexdigest()
                item['patch_hash']=hashlib.sha256(submitted.get('model_patch','').encode()).hexdigest()
                child['patch_hash']=item['patch_hash'];self.manager.store(child)
        q['current_run']=None
        if not success:
            item.update(status='interrupted' if child['status']=='interrupted' else 'failed',error=child.get('error') or '当前步骤未正常完成，请查看任务记录。')
            q.update(status='paused',reason=item['error'])
        else:
            item['stage_index']+=1
            item['status']='completed' if item['stage_index']==len(item['stages']) else 'pending'
            if child.get('evaluation'):item['evaluation']=child['evaluation']
            if q['status']=='pause_pending' and item['status']=='completed':q.update(status='paused',reason='当前题目已结束，队列已暂停。')
        if q.get('finishing'):
            for row in q['items']:
                if row['status'] not in ITEM_DONE:row['status']='cancelled'
            q['status']='cancelled'
        elif all(row['status'] in ITEM_DONE for row in q['items']):q['status']='completed'
        self.store(q)

    def tick(self):
        from .server import RunRequest
        with self.manager.lock:
            for candidate in list(self.records.values()):
                if not candidate.get('current_run') or self.manager.active==candidate['current_run']:continue
                child=self.manager.get(candidate['current_run'])
                if child['status'] in TERMINAL:self.consume(candidate,child)
            q=self.unfinished()
            if not q or self.recovery_issue or q.get('current_run'):return
            if q['status'] not in {'running','pause_pending'} or self.manager.active or not self.manager.accepting or getattr(self.manager,'tool_testing',False):return
            if any(r['status'] in {'failed','interrupted'} for r in q['items']):
                q.update(status='paused',reason='请明确重试或跳过失败／中断题，再继续队列。');self.store(q);return
            index=next((i for i,r in enumerate(q['items']) if r['status']=='pending'),None)
            if index is None:q['status']='completed';self.store(q);return
            item=q['items'][index]
            check_benchmark_snapshot(q['dataset'],q.get('benchmark_snapshot'))
            if q['status']=='pause_pending' and item['stage_index']==0:q['status']='paused';self.store(q);return
            stage=item['stages'][item['stage_index']]
            if stage=='evaluate':
                current_hash=hashlib.sha256(json.dumps(prediction(Path(item['prediction_path']),item['instance_id']),sort_keys=True).encode()).hexdigest()
                if current_hash!=item.get('prediction_hash'):raise ValueError('本批补丁快照已变化，未执行评测。请保留记录并重新选择补丁创建批次。')
            settings=inspect_settings()
            profile=q.get('profile_snapshots',{}).get(item.get('profile_id'))
            if profile:
                from .profiles import check_snapshot,snapshot_settings
                if any(check_snapshot(s)['changed'] for s in q['profile_snapshots'].values()):raise ValueError('引用的模型配置或密钥已变化，请重新确认；改变实验条件需要新建实验。')
                settings=settings | snapshot_settings(profile)
            elif settings['version']!=q['settings_version']:raise ValueError('配置或密钥已变化，请重新确认剩余任务参数。')
            if any(r.get('cleanup_pending') or r.get('recovery_pending') for r in self.manager.records.values()):raise ValueError('存在待清理资源或状态恢复，请处理后继续队列。')
            check_metadata_space()
            remaining=q['total_budget']-self.spent(q)
            if stage=='solve':
                if q['billing_unknown']:raise ValueError('存在无法确认的调用费用，禁止继续批量求解；请核对账单后结束此批次。')
                if remaining<=0:raise ValueError('本批估算费用上限已达到，后续求解已暂停。')
                from .profiles import profile_price_available
                if not q['diagnostics'] and not (profile_price_available(profile) if profile else price_available(settings['model_name'])):raise ValueError('模型计价不可用，后续求解已暂停。')
            request_id=hashlib.sha256(f"{q['id']}:{index}:{item['retry']}:{stage}".encode()).hexdigest()[:32]
            attempt={'stage':stage,'request_id':request_id,'run_id':None,'status':'starting','cost':0,
                     'attempt_id':item.get('attempt_id'),'attempt_number':item.get('attempt_number',1)}
            item['attempts'].append(attempt);item['status']='running';q['current_index']=index;self.store(q)
            context={'queue_id':q['id'],'index':index,'stage':stage,'remaining_budget':remaining,
                     'diagnostic':q['diagnostics'].get(item['instance_id']), 'profile_snapshot':profile,
                     'benchmark_snapshot':q.get('benchmark_snapshot'),'image_id':item.get('image_id'),
                     'attempt_id':item.get('attempt_id'),'attempt_number':item.get('attempt_number',1),
                     'solve_run_id':item.get('solve_run_id') if stage=='evaluate' else None,
                     'experiment_id':q['id'] if q.get('kind')=='experiment' else None,
                     'experiment_item_id':str(index) if q.get('kind')=='experiment' else None}
            request=RunRequest(kind={'solve':'swebench'}.get(stage,stage),dataset=q['dataset'],instance_id=item['instance_id'],
                               settings_version=q['settings_version'],request_id=request_id,prediction_path=item['prediction_path'],
                               profile_id=item.get('profile_id'),
                               allow_data_download=q['allow_data_download'],allow_image_download=q['allow_image_download'],auto_approve=q['auto_approve'])
            try:
                child=self.manager.create(request,queue_context=context)
            except Exception as exc:
                attempt.update(run_id=self.manager.request_ids.get(request_id),status='failed')
                item.update(status='failed',error=str(exc))
                q.update(status='paused',reason=str(exc),current_run=None)
                self.store(q)
                return
            attempt['run_id']=child['id'];q['current_run']=child['id'];self.store(q)

    def loop(self):
        while not self.stop_event.wait(.4):
            try:self.tick()
            except Exception as exc:
                with self.manager.lock:
                    q=self.unfinished()
                    if q:
                        for item in q['items']:
                            if item['status']=='running' and not q.get('current_run'):
                                item.update(status='failed',error=str(exc))
                        self.pause_record(q,str(exc))

    def control(self,queue_id,body: BatchControl):
        with self.manager.lock:
            q=self.get(queue_id)
            action=body.action
            reopen=q['status']=='completed' and q.get('kind')=='experiment' and action=='retry'
            if q['status'] in DONE and not reopen:raise ValueError('该批次已经结束。')
            if reopen:
                if self.unfinished() or self.manager.active or getattr(self.manager,'tool_testing',False):raise ValueError('请先结束当前任务或未完成批次，再重试实验。')
                if body.index is None or not 0<=body.index<len(q['items']) or q['items'][body.index]['status']!='completed':raise ValueError('请选择已经完成的求解条目。')
                q.update(status='paused',reason='已创建新的求解尝试，首轮成绩保留；请核对剩余预算后明确继续。')
            if action=='pause':q['status']='pause_pending' if q.get('current_run') or any(x['status']=='pending' and x['stage_index']>0 for x in q['items']) else 'paused'
            elif action=='stop':
                q['status']='paused'
                if q.get('current_run'):self.manager.cancel(q['current_run'])
            elif action=='finish':
                q['finishing']=True;q['status']='paused'
                if q.get('current_run'):self.manager.cancel(q['current_run'])
                else:
                    for item in q['items']:
                        if item['status'] not in ITEM_DONE:item['status']='cancelled'
                    q['status']='cancelled'
            elif action in {'retry','skip','cancel_item'}:
                if body.index is None or not 0<=body.index<len(q['items']):raise ValueError('题目序号无效。')
                item=q['items'][body.index]
                if item['status']=='running':raise ValueError('请先停止当前步骤。')
                if action=='cancel_item' and (item['status']!='pending' or item['stage_index']!=0):raise ValueError('只能取消未开始的题目。')
                rerun=action=='retry' and q.get('kind')=='experiment' and item['status']=='completed'
                if action in {'retry','skip'} and item['status'] not in {'failed','interrupted'} and not rerun:raise ValueError('只能重试或跳过失败／中断题。')
                if action=='retry':
                    if rerun:
                        if q.get('current_run') or q['status'] not in {'paused'}:raise ValueError('请先暂停实验，再重新求解。')
                        item['stage_index']=item['stages'].index('solve')
                        item.pop('evaluation',None)
                    if item['stages'][item['stage_index']]=='solve':
                        item.update(attempt_id=new_id(),attempt_number=item.get('attempt_number',1)+1,solve_run_id=None,prediction_path='')
                    item.update(status='pending',error='',retry=item['retry']+1)
                else:item['status']='cancelled' if action=='cancel_item' else 'skipped'
            elif action=='resume':
                if q.get('billing_unknown'):raise ValueError('上次调用费用无法确认，不能继续此批次。请核对服务商记录后结束批次；新的求解需另建批次并确认预算。')
                if q.get('current_run') or self.manager.active:raise ValueError('请等待当前任务和清理结束。')
                if any(r['status'] in {'failed','interrupted'} for r in q['items']):raise ValueError('请先明确重试或跳过失败／中断题。')
                settings=inspect_settings()
                if q.get('profile_snapshots'):
                    from .profiles import check_snapshot,freeze_profile,refresh_credentials
                    replacements={}
                    for key,snapshot in q['profile_snapshots'].items():
                        change=check_snapshot(snapshot)
                        if not change['changed']:continue
                        if not change.get('credentials_only'):raise ValueError('执行配置已变化，请结束此批次并建立新实验版本，避免混合不同条件。')
                        if body.accepted_profile_versions.get(key)!=freeze_profile(key)['credential_version']:raise ValueError('密钥已变化，请核对后重新确认。')
                        replacements[key]=refresh_credentials(snapshot)
                    q['profile_snapshots'].update(replacements)
                elif settings['version']!=q['settings_version']:
                    if body.accepted_settings_version!=settings['version']:raise ValueError('请确认当前配置后再继续。')
                    q['settings_version']=settings['version'];q['settings']={k:settings[k] for k in Settings.model_fields}
                q.update(status='running',reason='')
            self.store(q)
            return self.view(q)
