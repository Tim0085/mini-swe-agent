import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .common import APP_VERSION, CONFIG_ROOT, ROOT, RUNS, STATE, STATIC, TERMINAL, atomic, assert_no_credentials, export_allowed, job_dir, new_id, now, read_json, recover_json, redact, save_json, source_version
from .settings import Settings, read_settings, inspect_settings, save_settings, replace_key, recover_transaction, backup_list, restore_backup
from .processes import Job
from .benchmarks import DatasetId,REGISTRY,case_key,is_pinned,snapshot as benchmark_snapshot,check_snapshot as check_benchmark_snapshot

NONCE = os.environ.get('MINI_WEB_NONCE') or secrets.token_urlsafe(32)
PORT = int(os.environ.get('MINI_WEB_PORT', '8765'))
IDENTITY = hashlib.sha256(NONCE.encode()).hexdigest()[:24]
RUNNING_VERSION = source_version()
BOOT_ID=new_id()


class RunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['local', 'offline', 'swebench', 'evaluate', 'prepare','cache','download','catalog_sync']
    task: str = Field(default='', max_length=50000)
    cwd: str = Field(default='', max_length=1000)
    dataset: DatasetId = 'verified'
    instance_id: str = Field(default='', max_length=150)
    prediction_path: str = Field(default='', max_length=1000)
    auto_approve: bool = False
    allow_download: bool = False
    allow_data_download: bool | None = None
    allow_image_download: bool | None = None
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    settings_version: str
    parent_id: str | None = None
    profile_id: str | None = None
    profile_fingerprint: str | None = None
    auto_evaluate: bool = False
    offline_scenario: Literal['basic','slow_shell','slow_model','edit'] = 'basic'


class Approval(BaseModel):
    ticket: str = Field(pattern=r'^[0-9a-f]{32}$')
    approve: bool
    feedback: str = Field(default='', max_length=5000)


class SettingsUpdate(BaseModel):
    settings: Settings
    version: str
    repair: bool = False


class KeyUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=4096)
    version: str


class BenchRequest(BaseModel):
    dataset: DatasetId = 'verified'
    instance_id: str
    allow_download: bool = False


class LifecycleRequest(BaseModel):
    action: Literal['exit', 'restart']
    mode: Literal['stop', 'when_idle'] = 'stop'
    allow_pending_cleanup: bool = False


class ArchiveRequest(BaseModel):
    archived: bool


class RestoreRequest(BaseModel):
    backup_id: str
    version: str


class CleanupSelection(BaseModel):
    ids: list[str]


class CleanupCommit(BaseModel):
    token: str


class LifecycleTicket(BaseModel):
    id: str


class PatchRequest(BaseModel):
    dataset: DatasetId | None = None
    path: str = Field(max_length=1000)
    instance_id: str = Field(max_length=150)


class CatalogSync(BaseModel):
    dataset: DatasetId='verified'
    request_id: str=Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')
    allow_network: bool=False


def cleanup_containers(names: list[str],run_id: str) -> list[str]:
    from .bench import docker
    failures = []
    for prefix in names:
        if not isinstance(prefix,str) or not (prefix=='mini-web-'+run_id or (prefix.startswith('sweb.eval.') and prefix.endswith('.'+run_id))):
            failures.append(str(prefix))
            continue
        try:
            found = docker('ps', '-a', '--format', '{{.Names}}', '--filter', 'name=' + prefix)
            if found.returncode:
                failures.append(prefix)
                continue
            for name in found.stdout.splitlines():
                if name == prefix or name.startswith(prefix + '.'):
                    if docker('rm', '-f', name, timeout=60).returncode:
                        failures.append(name)
        except (OSError, subprocess.TimeoutExpired):
            failures.append(prefix)
    return failures


class Manager:
    def __init__(self):
        RUNS.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.active = None
        self.process = None
        self.job = None
        self.records = {}
        self.request_ids = {}
        self.deleted_requests=set()
        self.issues = []
        self.idle = threading.Event()
        self.idle.set()
        self.lifecycle = {'status':'idle'}
        self.accepting = True

    def store(self, meta):
        save_json(job_dir(meta['id']) / 'meta.json', meta)
        self.records[meta['id']] = meta
        if meta.get('request_id'):
            self.request_ids[meta['request_id']] = meta['id']

    def finish_record(self, meta):
        meta.pop('recovery_pending',None)
        meta.pop('recovery_error',None)
        recovery=job_dir(meta['id'])/'recovery.json'
        if meta.get('request_id'):
            self.request_ids[meta['request_id']]=meta['id']
        try:
            self.store(meta)
        except OSError as exc:
            message='任务已停止，但最终状态保存失败；请解除文件占用或检查磁盘后重试清理／恢复。'
            meta.update(status='cleanup_required',recovery_pending=True,phase=message,recovery_error=type(exc).__name__)
            self.records[meta['id']]=meta
            self.issues.append(meta['id']+'：'+message)
            try:
                save_json(recovery,meta)
            except OSError:
                self.issues.append(meta['id']+'：恢复记录也无法保存，原任务记录已保留，重启后需重新核对。')
        else:
            self.issues=[issue for issue in self.issues if not issue.startswith(meta['id']+'：')]
            try:
                recovery.unlink(missing_ok=True)
            except OSError:
                self.issues.append(meta['id']+'：旧恢复记录暂时无法移除；已保存的新状态优先。')
        return meta

    def commit_lifecycle_exit(self, ticket):
        with self.lock:
            if self.lifecycle.get('id')!=ticket or self.lifecycle.get('status')!='ready':
                return False
            self.lifecycle=self.lifecycle | {'status':'exiting'}
            return True

    def cancel_lifecycle(self, ticket):
        with self.lock:
            if self.lifecycle.get('id')!=ticket or self.lifecycle.get('status')=='exiting':
                raise HTTPException(409,'操作已结束或正在退出，无法取消。')
            self.lifecycle={'status':'idle'}
            self.lifecycle_ack.set()
            self.accepting=True
            return self.lifecycle

    def recover(self):
        if self.active:
            return
        self.records = {}
        self.request_ids = {}
        tombstones=recover_json(STATE/'request-tombstones.json',[])
        self.deleted_requests=set(tombstones) if isinstance(tombstones,list) and all(isinstance(x,str) for x in tombstones) else set()
        self.issues = recover_transaction()
        for folder in RUNS.iterdir():
            path = folder / 'meta.json'
            if path.is_file():
                recovery=folder/'recovery.json'
                source=recovery if recovery.is_file() and recovery.stat().st_mtime_ns>path.stat().st_mtime_ns else path
                meta = recover_json(source,None)
                if not isinstance(meta,dict) or meta.get('id') != folder.name or not all(isinstance(meta.get(key),str) for key in ['status','kind','title','created','model']) or meta.get('status') not in TERMINAL|{'preparing','running','evaluating','awaiting_confirmation','cleaning','stopping'}:
                    self.issues.append('记录损坏，已隔离显示：'+folder.name)
                    continue
                try:
                    job_dir(meta['id'])
                    datetime.fromisoformat(meta['created'])
                except ValueError:
                    self.issues.append('记录编号或时间无效，已跳过：'+folder.name)
                    continue
                dirty=False
                if meta['status'] not in TERMINAL:
                    meta.update(status='interrupted', pending=None, error='服务已重启，任务没有自动恢复。',outcome='interrupted')
                    meta['cleanup_pending'] = meta.get('containers',[])
                    dirty=True
                if meta.get('cleanup_pending') and meta['status']!='cleanup_required':
                    meta['status'] = 'cleanup_required'
                    dirty=True
                if dirty:
                    self.finish_record(meta)
                else:
                    self.records[meta['id']]=meta
                    if meta.get('request_id'):
                        self.request_ids[meta['request_id']]=meta['id']

    def get(self, run_id):
        if run_id in self.records:
            return json.loads(json.dumps(self.records[run_id]))
        path = job_dir(run_id) / 'meta.json'
        if not path.is_file():
            raise HTTPException(404, '未找到该任务。')
        meta = recover_json(path,None)
        if not isinstance(meta,dict) or meta.get('id') != run_id:
            raise HTTPException(409,'这条记录损坏，已保留原文件；其他任务仍可使用。')
        return meta

    def list(self, search='', archived=False):
        return sorted([m for m in self.records.values() if bool(m.get('archived',False))==archived and search.lower() in (m.get('title','')+' '+m.get('instance_id','')+' '+m['id']).lower()], key=lambda x:x['id'], reverse=True)

    def event(self, run_id, kind, data):
        with self.lock:
            folder = job_dir(run_id)
            meta = self.get(run_id)
            sequence = meta.get('sequence', 0) + 1
            event = redact({'id': sequence, 'time': now(), 'kind': kind, 'data': data})
            with (folder / 'events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
            meta.update(sequence=sequence, updated=now())
            if kind == 'state' and meta['status'] not in TERMINAL and meta['status'] != 'stopping':
                if data in TERMINAL:
                    meta.update(status='cleaning',outcome=data,phase='正在确认进程结束并清理所属资源')
                else:
                    meta['status'] = data
                if data != 'awaiting_confirmation':
                    meta['pending'] = None
            elif kind == 'approval' and meta['status'] not in TERMINAL and meta['status'] != 'stopping':
                meta.update(status='awaiting_confirmation', pending=data)
            elif kind == 'approval_expired' and (meta.get('pending') or {}).get('ticket') == data.get('ticket'):
                meta.update(pending=None)
                if meta['status'] not in TERMINAL and meta['status'] != 'stopping':
                    meta.update(status='running',phase='等待批准已超时，正在保存本次改动')
            elif kind in {'stats', 'result', 'error', 'evaluation', 'prediction', 'phase', 'prepared', 'changes', 'evaluation_progress','billing','catalog_result'}:
                meta[kind] = data
            elif kind == 'benchmark_runtime':
                meta[kind] = data
                meta['cwd'] = data['cwd']
            elif kind == 'container':
                meta.setdefault('containers', []).append(data)
            self.store(meta)

    def create(self, request: RunRequest, *, queue_context=None):
        from .bench import load_instance, prediction
        with self.lock:
            if request.request_id in self.deleted_requests:
                raise HTTPException(410,'该请求的记录已由用户删除。需要重新执行时，请明确新建任务。')
            if request.request_id in self.request_ids:
                return self.get(self.request_ids[request.request_id])
            if not self.accepting:
                raise HTTPException(409,'工作台正在退出或重启，请等待。')
            if getattr(self,'tool_testing',False):raise HTTPException(409,'正在验证模型工具调用，请等待完成。')
            if self.active:
                raise HTTPException(409, '已有任务正在运行，请先完成或停止它。')
            if any(m.get('cleanup_pending') or m.get('recovery_pending') for m in self.records.values()):
                raise HTTPException(409,'存在待清理资源或状态恢复，请先在对应任务中重试清理／恢复。')
            if queues.owns_slot() and queue_context is None:
                raise HTTPException(409,'顺序队列正在运行，请先暂停队列再开始独立任务。')
            diagnostic=bool(queue_context and queue_context.get('diagnostic'))
            if diagnostic and os.getenv('MINI_WEB_TESTING')!='1':raise ValueError('离线队列诊断仅允许隔离实例。')
            if request.offline_scenario!='basic' and (request.kind!='offline' or os.getenv('MINI_WEB_TESTING')!='1'):
                raise ValueError('专用离线诊断只允许在隔离测试实例运行。')
            settings = inspect_settings()
            profile_snapshot=(queue_context or {}).get('profile_snapshot')
            if request.kind in {'local','swebench'} and not diagnostic and not profile_snapshot:
                from .profiles import freeze_profile
                profile_snapshot=freeze_profile(request.profile_id or 'legacy')
                if (request.profile_id or 'legacy')=='legacy' and request.settings_version!=settings['version']:
                    raise HTTPException(409,'原配置已变化，请重新加载后确认。')
                if request.profile_fingerprint and request.profile_fingerprint!=profile_snapshot['fingerprint']:
                    raise HTTPException(409,'所选模型参数或密钥已变化，请重新加载模型档案后确认。')
            if profile_snapshot:
                from .profiles import snapshot_settings,check_snapshot,load_snapshot_secret
                if check_snapshot(profile_snapshot)['changed']:raise ValueError('模型档案或密钥已变化，请重新确认。')
                settings=settings | snapshot_settings(profile_snapshot) | {'valid':True}
                if request.kind in {'local','swebench'} and not diagnostic:
                    settings['key_configured']=bool(load_snapshot_secret(profile_snapshot))
            if not settings['valid'] and request.kind not in {'offline','prepare','evaluate','cache','download','catalog_sync'}:
                raise ValueError('模型配置需要修复，请先打开设置。离线检查和已有补丁评测仍可使用。')
            if not profile_snapshot and request.settings_version != settings['version']:
                raise HTTPException(409, '设置已变化，请重新加载任务页面后开始。')
            if request.kind == 'local' and not request.task.strip():
                raise ValueError('请填写任务内容。')
            benchmark_row = None
            if request.kind in {'local', 'offline'}:
                cwd = Path(request.cwd).expanduser().resolve()
                if not request.cwd.strip() or not cwd.is_dir():
                    raise ValueError('项目目录不存在。')
                request.cwd = str(cwd)
            elif request.kind=='catalog_sync':
                if not (request.allow_data_download if request.allow_data_download is not None else request.allow_download):raise ValueError('请明确允许同步官方目录。')
            elif request.kind not in {'prepare','cache','download'}:
                benchmark_row = load_instance(request.dataset, request.instance_id, False)
            else:
                from .bench import valid_instance
                valid_instance(request.instance_id,request.dataset)
            if request.kind in {'local', 'swebench'} and not settings['key_configured'] and not diagnostic:
                raise ValueError('请先在设置中配置密钥。')
            if request.kind == 'evaluate' and not diagnostic:
                selected_prediction = prediction(Path(request.prediction_path), request.instance_id)
                from .patches import inspect_patch
                selected_source = inspect_patch(Path(request.prediction_path), request.instance_id,dataset=request.dataset)
                if not selected_source['usable']:
                    raise ValueError(selected_source['reason'])
            else:
                selected_prediction = None
            assert_no_credentials(request.model_dump())
            run_id = new_id()
            folder = job_dir(run_id)
            folder.mkdir()
            payload = request.model_dump() | {'settings': {k: settings[k] for k in Settings.model_fields}}
            if profile_snapshot:payload['profile_snapshot']=profile_snapshot
            if request.kind not in {'local','offline'} and is_pinned(request.dataset):
                frozen=(queue_context or {}).get('benchmark_snapshot') or benchmark_snapshot(request.dataset)
                check_benchmark_snapshot(request.dataset,frozen)
                payload.update(benchmark_snapshot=frozen,case_key=case_key(request.dataset,request.instance_id))
            attempt_id=(queue_context or {}).get('attempt_id') or (new_id() if request.kind in {'local','swebench'} else None)
            payload['attempt_id']=attempt_id
            payload['allow_data_download']=request.allow_data_download if request.allow_data_download is not None else request.allow_download
            payload['allow_image_download']=request.allow_image_download if request.allow_image_download is not None else request.allow_download
            if queue_context:
                payload['queue_context']=queue_context
                if request.kind=='swebench':payload['settings']['cost_limit']=min(settings['cost_limit'],queue_context['remaining_budget'])
            if request.kind in {'local','swebench','offline'}:
                payload['effective_limits']={key:payload['settings'][key] for key in ('step_limit','cost_limit','timeout')}
            if request.kind == 'evaluate' and benchmark_row:
                from .evaluation_progress import EVALUATION_TIMEOUT
                payload['evaluation_timeout_seconds']=int(benchmark_row.get('pro_task',{}).get('verifier_timeout',EVALUATION_TIMEOUT))
            if request.kind in {'local','offline'}:
                payload.update(dataset='verified',instance_id='',prediction_path='',allow_download=False)
                title = request.task.strip()[:80] or '离线连接检查'
                cwd = request.cwd
            else:
                payload.update(task='',cwd='/testbed' if request.kind=='swebench' else '')
                title = {'swebench':'求解 · ','evaluate':'评测 · ','prepare':'加载题目 · ','cache':'缓存题目 · ','download':'下载环境 · ','catalog_sync':'同步目录 · '}[request.kind]+(request.dataset if request.kind=='catalog_sync' else request.instance_id)
                cwd = payload['cwd']
            if request.kind in {'evaluate','prepare','cache','download','catalog_sync'}:
                payload['auto_approve'] = False
            if request.kind == 'offline':
                payload['auto_approve'] = False
            # Snapshot evaluation input so later file edits cannot change the chosen patch.
            if request.kind == 'evaluate' and not diagnostic:
                atomic(folder / 'input.predictions.jsonl', json.dumps(selected_prediction, ensure_ascii=False) + '\n')
                payload['prediction_path'] = str(folder / 'input.predictions.jsonl')
            save_json(folder / 'request.json', payload)
            meta = {'id': run_id, 'request_id': request.request_id, 'title': title,
                    'kind': request.kind, 'status': 'preparing', 'created': now(), 'updated': now(), 'sequence': 0, 'pending': None,
                    'cwd': cwd, 'instance_id': payload['instance_id'], 'dataset': payload['dataset'],
                    'model': ('deterministic' if request.kind == 'offline' or diagnostic else selected_prediction['model_name_or_path'] if selected_prediction else '无需模型' if request.kind in {'prepare','cache','download','catalog_sync'} else settings['model_name']),
                    'containers': [],'auto_approve':payload['auto_approve'],'parent_id':request.parent_id}
            if request.kind == 'evaluate' and not diagnostic:
                meta['patch_source'] = selected_source
            if queue_context:meta['queue_id']=queue_context['queue_id']
            if diagnostic:meta['diagnostic']=True
            for identity in ('benchmark_snapshot','case_key','effective_limits','evaluation_timeout_seconds'):
                if identity in payload:meta[identity]=payload[identity]
            if profile_snapshot:meta['profile_snapshot']=profile_snapshot
            if attempt_id:meta['attempt_id']=attempt_id
            if queue_context:
                for key in ('attempt_number','solve_run_id','experiment_id','experiment_item_id'):
                    if queue_context.get(key) is not None:meta[key]=queue_context[key]
            if request.kind=='swebench':meta['solve_run_id']=run_id
            if request.kind=='evaluate':
                meta['evaluation_run_id']=run_id
                if selected_prediction:meta['patch_hash']=hashlib.sha256(selected_prediction['model_patch'].encode()).hexdigest()
            self.store(meta)
            env = os.environ.copy()
            for key in list(env):
                if key.startswith('MINI_WEB_') or key in {'OPENAI_API_KEY', 'MINI_API_KEY', 'MINI_API_BASE'}:
                    env.pop(key, None)
            env.update(MINI_WEB_STATE=str(STATE), PYTHONUTF8='1', PYTHONUNBUFFERED='1', PYTHONPATH=str(ROOT))
            env['MINI_WEB_CONFIG_ROOT'] = str(CONFIG_ROOT)
            if os.getenv('MINI_WEB_TESTING')=='1':
                env['MINI_WEB_TESTING']='1'
            try:
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'local_setup.webui.worker', run_id], cwd=ROOT,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                           env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError:
                meta.update(status='failed',error='任务进程无法启动，请检查 Python 环境。')
                self.store(meta)
                raise ValueError(meta['error'])
            try:
                job = Job(process)
            except OSError:
                process.kill()
                process.wait()
                meta.update(status='failed', error='无法建立任务进程隔离。')
                self.store(meta)
                raise
            self.active, self.process, self.job = run_id, process, job
            self.idle.clear()
            threading.Thread(target=self.pump, args=(run_id, process, job), daemon=True).start()
            return meta

    def pump(self, run_id, process, job):
        try:
            for line in process.stdout:
                if line.startswith('@@MINI_WEB@@'):
                    event = json.loads(line[len('@@MINI_WEB@@'):])
                    self.event(run_id, event['kind'], event['data'])
                elif line.strip():
                    self.event(run_id, 'log', line.rstrip())
            process.wait()
        except OSError as exc:
            with self.lock:
                meta=self.get(run_id)
                meta.update(error='任务输出或状态写入失败，已停止任务并保留恢复记录：'+type(exc).__name__,outcome='interrupted')
                if meta.get('kind') in {'local','swebench'}:
                    meta['billing']=meta.get('billing',{}) | {'pending':False,'unknown':True}
                self.records[run_id]=meta
        finally:
            try:
                with self.lock:
                    job.close()
                failures = cleanup_containers(self.get(run_id).get('containers', []),run_id)
                request=recover_json(job_dir(run_id)/'request.json',{})
                changes=None
                if request.get('kind')=='local' or (request.get('kind')=='offline' and request.get('offline_scenario')=='edit'):
                    try:
                        from .changes import finish
                        changes=finish(Path(request['cwd']),job_dir(run_id))
                    except Exception:
                        changes={'files':[],'note':'改动检查未完成，不能据此认定没有修改。'}
                with self.lock:
                    meta = self.get(run_id)
                    outcome = meta.get('outcome','failed')
                    if 'outcome' not in meta and not meta.get('error'):
                        meta['error'] = '任务进程意外退出，请查看输出。'
                    meta.update(status='cleanup_required' if failures else outcome,outcome=outcome,pending=None, cleanup_pending=failures,phase='',finished=now())
                    if changes:
                        meta['changes']=changes
                    self.finish_record(meta)
            finally:
                with self.lock:
                    self.active = self.process = self.job = None
                    self.idle.set()

    def approve(self, run_id, answer):
        with self.lock:
            meta = self.get(run_id)
            if self.active != run_id or not meta.get('pending') or meta['pending']['ticket'] != answer.ticket:
                raise HTTPException(409, '此确认已处理或已过期，请刷新任务状态。')
            expires = meta['pending'].get('expires_at')
            if expires is not None and time.time() >= expires:
                meta.update(pending=None,status='running',phase='等待批准已超时，正在保存本次改动')
                self.store(meta)
                raise HTTPException(409, '本题批准时限已到，未执行该命令；正在保存本次改动，请等待结果。')
            self.process.stdin.write(json.dumps(answer.model_dump(), ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            meta.update(pending=None, status='running')
            self.store(meta)
            return meta

    def cancel(self, run_id):
        with self.lock:
            meta = self.get(run_id)
            if self.active != run_id or meta['status'] in TERMINAL:
                return meta
            if self.job is None:
                return meta
            meta.update(status='stopping', outcome='cancelled',pending=None,phase='正在停止本任务进程，随后清理所属资源')
            self.store(meta)
            self.job.close()
            return meta

    def retry_cleanup(self, run_id):
        with self.lock:
            if self.active:
                raise HTTPException(409,'请等待当前任务结束后再清理。')
            meta = self.get(run_id)
            if not meta.get('cleanup_pending') and not meta.get('recovery_pending'):
                return meta
            self.active = run_id
            self.idle.clear()
            meta.update(status='cleaning',phase='重试清理所属容器')
            self.records[run_id]=meta
        try:
            failures = cleanup_containers(meta.get('cleanup_pending',[]),run_id)
            meta.update(cleanup_pending=failures,status='cleanup_required' if failures else meta.get('outcome','interrupted'),phase='')
            with self.lock:
                return self.finish_record(meta)
        finally:
            with self.lock:
                self.active = None
                self.idle.set()


manager = Manager()
from .queues import Queues,BatchRequest,BatchCommit,BatchControl,ExperimentRequest
queues=Queues(manager)
manager.queues=queues


@asynccontextmanager
async def lifespan(app):
    manager.recover()
    manager.accepting = True
    manager.lifecycle = {'status':'idle'}
    queues.recover()
    queues.start()
    yield
    queues.close()
    if manager.active:
        manager.cancel(manager.active)
        await asyncio.to_thread(manager.idle.wait,120)


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
from .profile_api import profile_router
app.include_router(profile_router(manager))


@app.middleware('http')
async def security(request: Request, call_next):
    allowed = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
    if request.headers.get('host') not in allowed:
        return JSONResponse({'detail': '非法主机。'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and origin not in {'http://' + host for host in allowed}:
        return JSONResponse({'detail': '不允许跨站请求。'}, status_code=403)
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse({'detail': '不允许跨站请求。'}, status_code=403)
    if request.url.path.startswith('/api/') and not secrets.compare_digest(request.cookies.get('mini_session', ''), NONCE):
        return JSONResponse({'detail': '请通过桌面快捷方式重新打开工作台。'}, status_code=401)
    if request.method in {'POST', 'PUT', 'DELETE'} and 'application/json' not in request.headers.get('content-type', ''):
        return JSONResponse({'detail': '请求格式无效。'}, status_code=415)
    if int(request.headers.get('content-length', '0')) > 200000:
        return JSONResponse({'detail': '请求内容过大。'}, status_code=413)
    response = await call_next(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                             'Referrer-Policy': 'no-referrer',
                             'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    names = {'model_name':'模型名','api_base':'接口地址','step_limit':'最大步数','cost_limit':'费用上限','timeout':'超时','key':'密钥','cwd':'项目目录','instance_id':'实例编号'}
    errors = [{'field':str(e['loc'][-1]),'message':names.get(str(e['loc'][-1]),'该字段')+'格式无效，请检查。'} for e in exc.errors()]
    return JSONResponse({'detail':'请检查标记的字段。','fields':errors},status_code=422)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'detail': redact(str(exc))}, status_code=400)


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    return JSONResponse({'detail': '操作失败，请检查本地服务状态。', 'type': type(exc).__name__}, status_code=500)


@app.get('/health')
def health():
    return {'app':'mini-workbench','identity':IDENTITY,'boot_id':BOOT_ID,'state_id':hashlib.sha256(str(STATE).encode()).hexdigest()[:16],
            'version':APP_VERSION,'code_version':RUNNING_VERSION,'update_available':source_version()!=RUNNING_VERSION}


@app.post('/session')
async def session(request: Request):
    body = await request.json()
    if not isinstance(body.get('nonce'), str) or not secrets.compare_digest(body['nonce'], NONCE):
        raise HTTPException(403, '启动凭据已失效，请重新打开快捷方式。')
    response = JSONResponse({'ok': True})
    response.set_cookie('mini_session', NONCE, httponly=True, samesite='strict')
    return response


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/api/config')
def config():
    return inspect_settings()


@app.put('/api/config')
def update_config(body: SettingsUpdate):
    with manager.lock:
        return save_settings(body.settings, body.version,repair=body.repair)


@app.put('/api/key')
def update_key(body: KeyUpdate):
    with manager.lock:
        if body.version != inspect_settings()['version']:
            raise HTTPException(409, '设置已变化，请重新加载。')
        replace_key(body.key)
        return inspect_settings()


@app.get('/api/config/backups')
def config_backups():
    return backup_list()


@app.post('/api/config/restore')
def config_restore(body: RestoreRequest):
    with manager.lock:
        return restore_backup(body.backup_id,body.version)


@app.get('/api/environment')
def environment():
    import shutil
    from windows_env import find_git_bash
    try:
        bash = str(find_git_bash())
        bash_issue = ''
    except FileNotFoundError:
        bash, bash_issue = '', 'Git Bash 未找到，编程命令暂不可用；设置和历史仍可使用。'
    return {'root':str(ROOT),'python':sys.executable,'bash':bash,'bash_issue':bash_issue,
            'docker_installed':bool(shutil.which('docker')),'active_run':manager.active,'tool_testing':getattr(manager,'tool_testing',False),
            'worker_pid':manager.process.pid if manager.process is not None and manager.process.poll() is None else None,
            'version':APP_VERSION,'boot_id':BOOT_ID,'code_version':RUNNING_VERSION,'update_available':source_version()!=RUNNING_VERSION,
            'issues':manager.issues+([queues.recovery_issue] if queues.recovery_issue else []),'lifecycle':manager.lifecycle,
            'queue_id':queues.unfinished()['id'] if queues.unfinished() else None,
            'cleanup_pending':[{'id':m['id'],'resources':m.get('cleanup_pending',[]),'recovery_pending':m.get('recovery_pending',False)}
                               for m in manager.records.values() if m.get('cleanup_pending') or m.get('recovery_pending')]}


@app.post('/api/lifecycle/repair-link')
def repair_link():
    path=STATE/'service.json'
    if path.is_file() and recover_json(path,None) is None:
        backup=STATE/'recovery'/('service-'+new_id()+'.json')
        backup.parent.mkdir(parents=True,exist_ok=True)
        backup.write_bytes(path.read_bytes())
    save_json(STATE/'service.json',{'port':PORT,'identity':IDENTITY,'nonce':NONCE,'pid':os.getpid(),'code_version':RUNNING_VERSION})
    return {'ok':True}


@app.post('/api/lifecycle')
def lifecycle(body: LifecycleRequest):
    with manager.lock:
        if getattr(manager,'tool_testing',False):raise HTTPException(409,'模型工具测试正在进行，请等待结果后再退出或重启，避免中断已发出的请求。')
        if manager.lifecycle.get('status') in {'waiting','stopping','ready','exiting'}:
            raise HTTPException(409,'已有退出或重启操作正在进行。')
        queues.suspend()
        manager.accepting = False
        ticket=new_id()
        ack=threading.Event()
        manager.lifecycle_ack=ack
        manager.lifecycle = {'id':ticket,'action':body.action,'status':'stopping' if body.mode=='stop' else 'waiting'}
        if body.mode=='stop' and manager.active:
            manager.cancel(manager.active)
    def finish():
        manager.idle.wait()
        with manager.lock:
            if manager.lifecycle.get('id')!=ticket:
                return
            pending = [m for m in manager.records.values() if m.get('cleanup_pending') or m.get('recovery_pending')]
            if pending and not body.allow_pending_cleanup:
                manager.lifecycle = {'id':ticket,'action':body.action,'status':'blocked','can_exit_with_pending':True,
                                     'error':'仍有资源或状态无法确认清理。可重试清理，或明确保留待处理清单后退出 Python 服务。'}
                manager.accepting = True
                return
            if not hasattr(app.state,'request_shutdown'):
                manager.lifecycle = {'status':'blocked','error':'此实例不支持进程控制，请从正式启动器打开。'}
                manager.accepting = True
                return
            manager.lifecycle = {'id':ticket,'action':body.action,'status':'ready','pending_resources':[{'id':m['id'],'resources':m.get('cleanup_pending',[])} for m in pending]}
        ack.wait(10)
        if not manager.commit_lifecycle_exit(ticket):
            return
        time.sleep(.4)
        app.state.request_shutdown(body.action=='restart')
    threading.Thread(target=finish,daemon=True).start()
    return manager.lifecycle


@app.post('/api/lifecycle/ack')
def lifecycle_ack(body: LifecycleTicket):
    with manager.lock:
        if manager.lifecycle.get('id')!=body.id or manager.lifecycle.get('status')!='ready':
            raise HTTPException(409,'退出确认已变化，请重新读取状态。')
        manager.lifecycle_ack.set()
        return manager.lifecycle


@app.post('/api/lifecycle/cancel')
def lifecycle_cancel(body: LifecycleTicket):
    return manager.cancel_lifecycle(body.id)


@app.get('/api/storage')
def storage_summary(refresh: bool=False):
    from .storage import SUMMARY
    from types import SimpleNamespace
    with manager.lock:
        snapshot=SimpleNamespace(records=json.loads(json.dumps(manager.records)),active=manager.active,protected_run_ids=queues.protected_runs())
    return SUMMARY.get(snapshot,refresh=refresh)


@app.post('/api/storage/preview')
def storage_preview(body: CleanupSelection):
    from .storage import preview
    with manager.lock:
        return preview(body.ids,manager)


@app.delete('/api/storage')
def storage_delete(body: CleanupCommit):
    from .storage import delete
    with manager.lock:
        return delete(body.token,manager)


@app.get('/api/directories')
def directories(path: str = ''):
    folder = Path(path or ROOT).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError('目录不存在，请检查路径。')
    try:
        children = sorted([p for p in folder.iterdir() if p.is_dir() and p.name not in {'.git', '.venv', '__pycache__'}], key=lambda p:p.name.lower())
    except PermissionError:
        raise ValueError('没有权限浏览这个目录，请选择其他位置。')
    return {'path':str(folder), 'parent':str(folder.parent), 'children':[{'name':p.name,'path':str(p)} for p in children[:300]], 'truncated':len(children)>300}


@app.post('/api/bench/preview')
def bench_preview(body: BenchRequest):
    from .bench import preview
    if body.allow_download:
        raise ValueError('下载已移到可取消的“加载题目”任务，请使用新版页面。')
    return preview(body.dataset, body.instance_id, False)


@app.get('/api/runs')
def runs():
    with manager.lock:
        return [redact(x) for x in manager.list()[:50]]


@app.get('/api/history')
def history(search: str='',offset: int=0,limit: int=30,archived: bool=False):
    rows = manager.list(search[:200],archived)
    offset=max(0,offset)
    limit=max(1,min(100,limit))
    return {'items':[redact(x) for x in rows[offset:offset+limit]],'total':len(rows),'offset':offset,'limit':limit,'issues':manager.issues}


@app.get('/api/requests/{request_id}')
def find_request(request_id: str):
    with manager.lock:
        if request_id in manager.deleted_requests:
            raise HTTPException(410,'该任务记录已经由用户删除；不会自动重复执行。')
        workflow=next((q for q in queues.records.values() if q['request_id']==request_id and q.get('kind')=='workflow'),None)
        if workflow:
            child_id=next((a['run_id'] for item in workflow['items'] for a in item['attempts'] if a.get('run_id')),None)
            if child_id:return run_detail(child_id) | {'workflow_id':workflow['id']}
            return {'workflow_id':workflow['id'],'workflow':queues.view(workflow),'status':workflow['status']}
        if request_id not in manager.request_ids:
            raise HTTPException(404,'未发现已创建任务。')
        return redact(manager.get(manager.request_ids[request_id]))


@app.post('/api/runs')
def create_run(body: RunRequest):
    if body.kind=='swebench' and body.auto_evaluate:
        with manager.lock:
            previous=next((q for q in queues.records.values() if q['request_id']==body.request_id),None)
            if not previous:
                if manager.active or not manager.accepting:raise ValueError('请等待当前任务结束后再开始。')
                from .profiles import freeze_profile,snapshot_settings
                snapshot=freeze_profile(body.profile_id or 'legacy')
                if (body.profile_id or 'legacy')=='legacy' and body.settings_version!=inspect_settings()['version']:
                    raise HTTPException(409,'原配置已变化，请重新加载后确认。')
                if body.profile_fingerprint and body.profile_fingerprint!=snapshot['fingerprint']:
                    raise HTTPException(409,'所选模型参数或密钥已变化，请重新加载模型档案后确认。')
                limits=snapshot_settings(snapshot)
                plan=queues.preview(BatchRequest(dataset=body.dataset,instance_ids=[body.instance_id],operation='solve',
                    profile_id=body.profile_id or 'legacy',settings_version=body.settings_version,auto_evaluate=True,
                    total_budget=limits['cost_limit'],auto_approve=body.auto_approve,
                    allow_data_download=body.allow_data_download if body.allow_data_download is not None else body.allow_download,
                    allow_image_download=body.allow_image_download if body.allow_image_download is not None else body.allow_download))
                queues.previews[plan['token']]['kind']='workflow'
                previous=queues.commit(BatchCommit(token=plan['token'],request_id=body.request_id))
            queues.tick()
            return find_request(body.request_id)
    return manager.create(body)


@app.get('/api/runs/{run_id}')
def run_detail(run_id: str):
    meta=manager.get(run_id)
    if meta.get('evaluation') and not meta['evaluation'].get('category'):
        from .bench import summarize_report
        reports=list(job_dir(run_id).glob('*.'+run_id+'.json'))
        if reports:
            report=recover_json(reports[0],None)
            if isinstance(report,dict):
                meta['evaluation']=meta['evaluation'] | summarize_report(report)
    value=redact(meta) | {'artifacts':artifact_list(run_id)}
    if meta['kind'] in {'swebench','evaluate'}:
        value['result_summary']=redact(result_index().for_run(run_id))
    if meta.get('queue_id') in queues.records:
        value['workflow_id']=meta['queue_id']
        workflow=queues.get(meta['queue_id'])
        value['workflow']=redact(queues.view(workflow))
        if workflow.get('kind')=='workflow':
            solved=workflow['items'][0].get('solve_run_id')
            if solved:value['result_summary']=redact(result_index().for_run(solved))
    return value


@app.get('/api/benchmarks')
def benchmark_choices():
    return {'items':list(REGISTRY.values())}


@app.get('/api/patches')
def patch_choices(instance_id: str,dataset: DatasetId | None=None):
    from .patches import catalog, source_paths
    return redact(catalog(instance_id, source_paths(list(manager.records.values()),dataset=dataset or ''),dataset=dataset or ''))


@app.get('/api/library')
def library_list(dataset: DatasetId='verified',search: str='',cached: Literal['all','yes','no']='all',
                 patch: Literal['all','yes','no']='all',result: str='all',offset: int=0,environment_state: Literal['all','cached','missing','unknown']='all',limit: int=8):
    from .library import listing
    with manager.lock:records=list(manager.records.values())
    return redact(listing(dataset,records,search,cached,patch,result,offset,environment_state,limit))


@app.get('/api/instance-options')
def instance_options(dataset: DatasetId='verified',search: str=''):
    from .library import suggestions
    return redact(suggestions(dataset,search))


@app.get('/api/library/{dataset}/{instance}')
def library_detail(dataset: str,instance: str):
    from .library import readiness,image_inventory
    with manager.lock:records=list(manager.records.values())
    return redact(readiness(dataset,instance,records,image_inventory()))


@app.post('/api/library/sync')
def library_sync(body: CatalogSync):
    if not body.allow_network:raise ValueError('请明确确认同步题目目录。')
    return manager.create(RunRequest(kind='catalog_sync',dataset=body.dataset,request_id=body.request_id,
                                     settings_version=inspect_settings()['version'],allow_data_download=True))


@app.get('/api/queues')
def queue_list():
    with manager.lock:
        return [{'id':q['id'],'request_id':q['request_id'],'status':q['status'],'operation':q['operation'],'created':q['created'],
                 'count':len(q['items']),'completed':sum(x['status']=='completed' for x in q['items'])} for q in sorted(queues.records.values(),key=lambda x:x['created'],reverse=True)]


@app.post('/api/queues/preview')
def queue_preview(body: BatchRequest):
    result=queues.preview(body)
    return redact(result) | {'token':result['token']}


@app.post('/api/queues')
def queue_create(body: BatchCommit):
    return redact(queues.commit(body))


@app.get('/api/queues/{queue_id}')
def queue_detail(queue_id: str):
    with manager.lock:return redact(queues.view(queues.get(queue_id)))


@app.post('/api/queues/{queue_id}/control')
def queue_control(queue_id: str,body: BatchControl):
    return redact(queues.control(queue_id,body))


@app.get('/api/experiments')
def experiment_list():
    with manager.lock:
        return [redact(queues.view(q)) for q in sorted(queues.records.values(),key=lambda x:x['created'],reverse=True) if q.get('kind')=='experiment']


@app.post('/api/experiments/preview')
def experiment_preview(body: ExperimentRequest):
    result=queues.preview(body)
    return redact(result) | {'token':result['token']}


@app.post('/api/experiments')
def experiment_create(body: BatchCommit):
    previous=next((q for q in queues.records.values() if q['request_id']==body.request_id),None)
    if previous and previous.get('kind')!='experiment':raise ValueError('请求编号已用于其他批次。')
    if not previous and queues.previews.get(body.token,{}).get('kind')!='experiment':raise ValueError('请先预览同题实验。')
    return redact(queues.commit(body))


@app.get('/api/experiments/{experiment_id}')
def experiment_detail(experiment_id: str):
    with manager.lock:
        q=queues.get(experiment_id)
        if q.get('kind')!='experiment':raise ValueError('这不是同题实验。')
        return redact(queues.view(q))


@app.post('/api/experiments/{experiment_id}/control')
def experiment_control(experiment_id: str,body: BatchControl):
    experiment_detail(experiment_id)
    return redact(queues.control(experiment_id,body))


def result_index():
    from .results import Results
    with manager.lock:
        return Results(json.loads(json.dumps(list(manager.records.values()))),json.loads(json.dumps(list(queues.records.values()))))


@app.get('/api/results')
def result_list(dataset: str='all',search: str='',limit: int=50,offset: int=0):
    return redact(result_index().listing(dataset=dataset,search=search[:200],limit=max(1,min(100,limit)),offset=max(0,offset)))


@app.get('/api/results/compare')
def result_compare(group_ids: str='',dataset: str='all',experiment_id: str | None=None):
    return redact(result_index().compare(group_ids=[s for s in group_ids.split(',') if s] or None,dataset=dataset,experiment_id=experiment_id))


@app.get('/api/results/export')
def result_export(format: Literal['csv','json']='json',experiment_id: str | None=None,dataset: str='all',group_ids: str=''):
    index=result_index()
    options={'experiment_id':experiment_id,'dataset':dataset,'group_ids':None if not group_ids.strip() else [s for s in group_ids.split(',') if s]}
    content=index.export_csv(**options) if format=='csv' else index.export_json(**options)
    if not export_allowed(content):raise ValueError('导出内容包含疑似凭据，已阻止下载。')
    return Response(content,media_type='text/csv' if format=='csv' else 'application/json',
                    headers={'Content-Disposition':f'attachment; filename="mini-evaluation-results.{format}"'})


@app.get('/api/results/{attempt_id}')
def result_detail(attempt_id: str):
    return redact(result_index().detail(attempt_id))


@app.post('/api/patches/inspect')
def patch_inspect(request: PatchRequest):
    from .patches import inspect_patch
    return redact(inspect_patch(Path(request.path), request.instance_id,dataset=request.dataset or ''))


@app.get('/api/runs/{run_id}/conversation-source')
def conversation_source(run_id: str, before: int | None = None, limit: int = 50):
    from .patches import conversation_for, source_paths
    meta = manager.get(run_id)
    if meta['kind'] != 'evaluate':
        return {'messages': [], 'note': '此任务的对话通过实时事件显示。'}
    folder = job_dir(run_id)
    snapshot = folder / 'input.predictions.jsonl'
    if not snapshot.is_file():
        return {'messages': [], 'note': '此旧记录没有评测输入快照，无法可靠关联原始对话。'}
    paths = source_paths(list(manager.records.values()),dataset=meta.get('dataset',''))
    paths.append((snapshot,meta))
    request = recover_json(folder / 'request.json', {})
    request = request if isinstance(request,dict) else {}
    context = request.get('queue_context') if isinstance(request.get('queue_context'),dict) else {}
    source = dict(meta)
    for key in ('solve_run_id', 'attempt_id'):
        source[key] = meta.get(key) or request.get(key) or context.get(key)
    references = [record for record in manager.records.values() if record['kind'] == 'swebench' and
                  (record['id'] == source.get('solve_run_id') or
                   (source.get('attempt_id') and record.get('attempt_id') == source['attempt_id']))]
    paths.extend(source_paths(references,dataset=meta.get('dataset','')))
    return conversation_for(snapshot, meta['instance_id'], paths, before, limit,dataset=meta.get('dataset',''),source=source)


@app.get('/api/runs/{run_id}/timeline')
def timeline(run_id: str, before: int | None = None, limit: int = 50):
    from .timeline import event_page
    manager.get(run_id)
    return event_page(job_dir(run_id)/'events.jsonl',before,limit)


@app.get('/api/bench/cached')
def cached_problem(dataset: str, instance_id: str):
    from .bench import dataset_file,load_instance,problem_description
    if not dataset_file(dataset,instance_id).is_file():
        return {'ready':False,'note':'本题尚未缓存，请先加载题目；不会自动下载。'}
    row=load_instance(dataset,instance_id,False)
    return redact({'ready':True,'dataset':dataset,'instance_id':instance_id,'problem_statement':problem_description(row,dataset),
                   'note':'已核对本机题目缓存，可继续。开始时会再次检查缓存、Docker 和镜像；不自动下载。'})


@app.post('/api/runs/{run_id}/cleanup')
def retry_cleanup(run_id: str):
    return manager.retry_cleanup(run_id)


@app.post('/api/runs/{run_id}/archive')
def archive_run(run_id: str,body: ArchiveRequest):
    with manager.lock:
        meta=manager.get(run_id)
        if meta['status'] not in TERMINAL or manager.active==run_id:
            raise HTTPException(409,'活动任务不能归档。')
        meta['archived']=body.archived
        manager.store(meta)
        return redact(meta)


@app.get('/api/runs/{run_id}/reuse')
def reuse(run_id: str):
    manager.get(run_id)
    payload=recover_json(job_dir(run_id)/'request.json',{})
    data={k:payload.get(k,'') for k in ['kind','task','cwd','dataset','instance_id','prediction_path']}
    if not export_allowed(json.dumps(data,ensure_ascii=False)):
        raise HTTPException(409,'原任务含疑似凭据，已阻止复制；原文未修改，请先在本机检查。')
    return data | {'parent_id':run_id,'auto_approve':False,'allow_download':False}


@app.post('/api/runs/{run_id}/approve')
def approve(run_id: str, body: Approval):
    return manager.approve(run_id, body)


@app.post('/api/runs/{run_id}/cancel')
def cancel(run_id: str):
    return manager.cancel(run_id)


@app.get('/api/runs/{run_id}/events')
async def events(run_id: str, request: Request, after: int = 0):
    manager.get(run_id)
    after = max(after, int(request.headers.get('last-event-id', '0')))
    async def stream():
        position = 0
        last = after
        while not await request.is_disconnected():
            if manager.lifecycle.get('status')=='exiting':
                yield 'event: service-exit\ndata: {}\n\n'
                break
            path = job_dir(run_id) / 'events.jsonl'
            if path.is_file():
                with path.open('r', encoding='utf-8') as handle:
                    handle.seek(position)
                    while line := handle.readline():
                        if not line.endswith('\n'):
                            break
                        position = handle.tell()
                        event = json.loads(line)
                        if event['id'] > last:
                            last = event['id']
                            if event['kind']=='message':
                                from .timeline import message_preview
                                event=event | {'data':message_preview(event['data'])}
                            yield f"id: {last}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield ': heartbeat\n\n'
            if manager.get(run_id)['status'] in TERMINAL and manager.active != run_id:
                yield 'event: done\ndata: {}\n\n'
                break
            await asyncio.sleep(0.35)
    return StreamingResponse(stream(), media_type='text/event-stream')


@app.get('/api/runs/{run_id}/artifact/{name}')
def artifact(run_id: str, name: str):
    entries={entry['id']:entry for entry in artifact_list(run_id)}
    if name not in entries:
        raise HTTPException(404, '该文件尚未生成。')
    path=job_dir(run_id)/entries[name]['relative_path']
    if not export_allowed(path.read_text(encoding='utf-8-sig',errors='replace')):
        raise HTTPException(409,'文件含疑似凭据，下载已阻止。私有原文未改写，请先在本机检查并移除敏感内容。')
    return FileResponse(path, filename=path.name)


def artifact_list(run_id: str):
    folder=job_dir(run_id)
    paths=[folder/name for name in ['trajectory.traj.json','predictions.jsonl','events.jsonl','changes.diff','changes.json'] if (folder/name).is_file() and (name!='changes.diff' or (folder/name).stat().st_size>0)]
    paths.extend(folder.glob('*.'+run_id+'.json'))
    for name in ['report.json','test_output.txt','run_instance.log']:
        paths.extend(folder.glob('logs/run_evaluation/**/'+name))
    return [{'id':p.name if p.parent==folder and p.name in {'trajectory.traj.json','predictions.jsonl','events.jsonl','changes.diff','changes.json'} else hashlib.sha256(str(p.relative_to(folder)).encode()).hexdigest()[:16],
             'name':p.name,'relative_path':p.relative_to(folder).as_posix(),'bytes':p.stat().st_size} for p in paths if not p.is_symlink() and p.resolve().is_relative_to(folder.resolve())]


@app.get('/api/legacy')
def legacy():
    files = list((ROOT / '.local').glob('*.traj.json')) + list((ROOT / '.local').glob('*.predictions.jsonl'))
    return [{'name': p.name, 'path': str(p), 'bytes': p.stat().st_size} for p in sorted(files)]


@app.post('/api/legacy/read')
async def legacy_read(request: Request):
    path = Path((await request.json()).get('path', '')).resolve()
    allowed = {Path(x['path']).resolve() for x in legacy()}
    if path not in allowed:
        raise HTTPException(403, '请选择已索引的历史文件。')
    with path.open('r',encoding='utf-8-sig',errors='replace') as handle:
        content=handle.read(200001)
    return {'content':redact(content[:200000]),'truncated':len(content)>200000,
            'note':'仅展示前 200000 字符，原始记录未修改。可用原文件路径继续评测。' if len(content)>200000 else '原始记录只读预览。',
            'path':str(path)}


app.mount('/static', StaticFiles(directory=STATIC), name='static')
