"""Only explicit, previewed cleanup of workbench-owned paths."""
import hashlib
import copy
import json
import secrets
import shutil
import time
import threading
from pathlib import Path

from .common import CONFIG_ROOT, RUNS, STATE, TERMINAL, job_dir, now, read_json, save_json
from .results import Results
from .settings import backup_list

PLANS={}
PROTECTED_NOTE='测试和审查资料仅统计，不自动清理。同题实验的首轮任务永久保留，以保存首轮成绩；未结束队列引用的任务也受保护。下方只可选择其他已结束任务和旧配置备份；保留最近备份。原配置、密钥、旧评测、HF 缓存和 Docker 镜像不在清理范围。'


class SummaryCache:
    """Single background inventory; cleanup always validates current files separately."""

    def __init__(self, ttl: float=60):
        self.ttl=ttl
        self.lock=threading.Lock()
        self.value=None
        self.scanning=False
        self.error=None
        self.completed=0.0
        self.scanned_at=None
        self.generation=0
        self.thread=None

    def get(self, manager, *, refresh: bool=False):
        with self.lock:
            expired=time.monotonic()-self.completed>=self.ttl
            if not self.scanning and (refresh or ((self.value is None or expired) and not self.error)):
                self.scanning=True
                self.error=None
                self.thread=threading.Thread(target=self.scan,args=(manager,self.generation),daemon=True)
                self.thread.start()
            return copy.deepcopy(self.value or {'categories':[], 'items':[], 'protected':PROTECTED_NOTE}) | {
                'scanning':self.scanning, 'scanned_at':self.scanned_at,
                'stale':bool(self.value and (self.scanning or self.error or expired)), 'error':self.error}

    def scan(self, manager, generation):
        value=None
        error=None
        try:
            value=summary(manager)
        except Exception:
            error='容量统计暂未完成，请检查目录访问权限后点击重新统计；原文件未被修改。'
        with self.lock:
            self.scanning=False
            if generation!=self.generation:
                return
            self.error=error
            if value is not None:
                self.value=value
                self.completed=time.monotonic()
                self.scanned_at=now()

    def invalidate(self):
        with self.lock:
            self.generation+=1
            self.value=None
            self.error=None
            self.completed=0.0
            self.scanned_at=None


SUMMARY=SummaryCache()


def inventory(path: Path):
    path=path.resolve()
    entries=[]
    if not path.exists():
        return entries
    candidates=[path] if path.is_file() else path.rglob('*')
    for child in candidates:
        if child.is_symlink() or child.is_junction():
            raise ValueError('清理范围含链接或连接点，未处理；请先人工检查。')
        if child.is_file():
            stat=child.stat()
            entries.append((str(child.relative_to(path)) if child!=path else child.name,stat.st_size,stat.st_mtime_ns))
    return sorted(entries)


def size(path):
    try:
        if path.is_symlink() or path.is_junction():
            return None
        return sum(x[1] for x in inventory(path))
    except (OSError,ValueError):
        return None


def candidates(manager):
    result={}
    protected=getattr(manager,'protected_run_ids',None)
    if protected is None:protected=manager.queues.protected_runs() if hasattr(manager,'queues') else set()
    if protected:
        protected=set(protected)
        for item in Results(list(manager.records.values())).items:
            if item['solve_run_id'] in protected:protected.update(item['evaluation_run_ids'])
    for meta in manager.records.values():
        if meta['status'] in TERMINAL and meta['status']!='cleanup_required' and not meta.get('cleanup_pending') and manager.active!=meta['id'] and meta['id'] not in protected:
            path=job_dir(meta['id'])
            result['run:'+meta['id']]={'path':path,'label':meta['title']+' · '+meta['id']}
    journal=CONFIG_ROOT/'.local/webui/config-transaction.json'
    locked=None
    if journal.exists():
        try:
            locked=read_json(journal)['backup_id']
        except (OSError,ValueError,KeyError):
            locked='all'
    backups=backup_list()
    # Always retain the most recent usable backup and any in-progress transaction backup.
    for backup in backups[1:]:
        if locked not in {'all',backup['id']}:
            result['backup:'+backup['id']]={'path':CONFIG_ROOT/'.local/webui/config-backups'/backup['id'],'label':'旧配置备份 · '+backup['id']}
    return result


def summary(manager):
    names={'runs':'工作台任务','config-backups':'配置备份','datasets':'工作台题目缓存','pro-data':'Pro 公共题目资料','pro-assets':'Pro 官方题目与验证资源','catalog':'实例目录索引','queues':'顺序队列记录与补丁快照','profiles':'模型档案与加密凭据','service.log':'服务日志'}
    groups={name:[] for name in [*names.values(),'开发测试与审查资料','其他工作台资料']}
    for path in STATE.iterdir():
        category=names.get(path.name) or ('开发测试与审查资料' if path.name=='audit' or path.name.startswith(('audit-','fix-','fixes-','test-','patch-choice-','pytest-','implementation-','rebench-browser-','rebench-live-preflight','rebench-2026')) else '其他工作台资料')
        groups[category].append(size(path))
    categories=[{'name':name,'bytes':sum(x for x in amounts if x is not None),'available':all(x is not None for x in amounts)} for name,amounts in groups.items()]
    external_backups=CONFIG_ROOT/'.local/webui/config-backups'
    if external_backups.parent.resolve()!=STATE.resolve():
        amount=size(external_backups)
        categories.append({'name':'隔离配置目录备份','bytes':amount,'available':amount is not None})
    items=[]
    for item_id,item in candidates(manager).items():
        amount=size(item['path'])
        if amount is not None:
            items.append({'id':item_id,'label':item['label'],'bytes':amount})
    return {'categories':categories,'items':items,'protected':PROTECTED_NOTE}


def preview(ids,manager):
    if not ids or len(ids)>100 or len(set(ids))!=len(ids):
        raise ValueError('请勾选 1–100 项不重复的工作台记录。')
    allowed=candidates(manager)
    if any(item not in allowed for item in ids):
        raise ValueError('选择中含活动、受保护或不存在的记录，请刷新列表。')
    selected=[]
    for item_id in ids:
        original=allowed[item_id]['path']
        if original.is_symlink() or original.is_junction():
            raise ValueError('选中的根目录是链接，未执行清理。')
        path=original.resolve()
        expected=RUNS.resolve() if item_id.startswith('run:') else (CONFIG_ROOT/'.local/webui/config-backups').resolve()
        if path.parent!=expected or path.name!=item_id.split(':',1)[1]:
            raise ValueError('目录归属校验失败，未执行清理。')
        files=inventory(path)
        selected.append({'id':item_id,'path':path,'bytes':sum(x[1] for x in files),'fingerprint':hashlib.sha256(json.dumps(files).encode()).hexdigest()})
    token=secrets.token_urlsafe(24)
    PLANS[token]={'expires':time.monotonic()+300,'items':selected}
    return {'token':token,'count':len(selected),'bytes':sum(x['bytes'] for x in selected),'paths':[str(x['path']) for x in selected],'expires_in':300}


def delete(token,manager):
    plan=PLANS.pop(token,None)
    if not plan or plan['expires']<time.monotonic():
        raise ValueError('清理预览已失效，请重新预览。')
    allowed=candidates(manager)
    for item in plan['items']:
        if item['id'] not in allowed or allowed[item['id']]['path'].is_symlink() or allowed[item['id']]['path'].is_junction() or allowed[item['id']]['path'].resolve()!=item['path']:
            raise ValueError('记录状态已变化，未执行清理，请重新预览。')
        if hashlib.sha256(json.dumps(inventory(item['path'])).encode()).hexdigest()!=item['fingerprint']:
            raise ValueError('文件在预览后发生变化，未执行清理。')
    SUMMARY.invalidate()
    removed=[]
    for item in plan['items']:
        expected=RUNS.resolve() if item['id'].startswith('run:') else (CONFIG_ROOT/'.local/webui/config-backups').resolve()
        if item['path'].resolve().parent!=expected or item['path'].is_symlink() or item['path'].is_junction():
            raise ValueError('最终目录校验失败，已停止清理。')
        shutil.rmtree(item['path'])
        removed.append(item['id'])
        if item['id'].startswith('run:'):
            run_id=item['id'][4:]
            record=manager.records.pop(run_id,None)
            if record and record.get('request_id'):
                manager.request_ids.pop(record['request_id'],None)
                manager.deleted_requests.add(record['request_id'])
                save_json(STATE/'request-tombstones.json',sorted(manager.deleted_requests))
    return {'removed':removed}
