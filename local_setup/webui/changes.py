"""Read-only before/after file inventory; does not change the user's Git index."""
import hashlib
import difflib
import os
import re
import time
from pathlib import Path

from .common import atomic, export_allowed, read_json, save_json

SKIP={'.git','.venv','node_modules','__pycache__','.local','.cache'}
PRIVATE=re.compile(r'(^\.env(?:\.|$)|\.(?:pem|key|p12|pfx)$|^id_rsa|api-key|service\.json$|^(?:credentials?|secrets?)\.)',re.I)
PRIVATE_CONTENT=re.compile(r'''(?im)^\s*["']?(?:[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)|api_key|access_token)["']?\s*[:=]\s*\S+''')


def snapshot(cwd: Path) -> dict:
    files={}
    texts={}
    text_bytes=0
    seen=[]
    skipped=0
    started=time.monotonic()
    for folder,dirs,names in os.walk(cwd):
        dirs[:]=[name for name in dirs if name not in SKIP and not (Path(folder)/name).is_symlink() and not (Path(folder)/name).is_junction()]
        for name in names:
            path=Path(folder)/name
            if len(seen)>=10000 or time.monotonic()-started>10:
                return {'files':files,'texts':texts,'seen':seen,'walk_complete':False,'complete':False,'skipped':skipped,'note':'达到扫描数量或时间上限；未把未检查文件认定为未改动。'}
            try:
                seen.append(path.relative_to(cwd).as_posix())
                if PRIVATE.search(name) or path.is_symlink() or path.stat().st_size>2_000_000:
                    skipped+=1
                    continue
                data=path.read_bytes()
                files[path.relative_to(cwd).as_posix()]=hashlib.sha256(data).hexdigest()
                if len(data)<=200000 and text_bytes+len(data)<=2000000 and b'\0' not in data:
                    try:
                        content=data.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    if export_allowed(content) and not PRIVATE_CONTENT.search(content):
                        texts[path.relative_to(cwd).as_posix()]=content
                        text_bytes+=len(data)
            except OSError:
                skipped+=1
    return {'files':files,'texts':texts,'seen':seen,'walk_complete':True,'complete':skipped==0,'skipped':skipped,'note':'文件指纹排除环境、缓存、疑似凭据和超过 2 MB 的文件；内容预览限 UTF-8 文本，每文件 200 KB、每次快照合计 2 MB。'}


def finish(cwd: Path,folder: Path):
    path=folder/'before-files.json'
    if not path.is_file():
        return {'files':[],'note':'任务在初始文件快照完成前停止，无法可靠归属改动。'}
    before=read_json(path)
    after=snapshot(cwd)
    rows=[]
    for name in sorted(set(before['files'])|set(after['files'])):
        a,b=before['files'].get(name),after['files'].get(name)
        if a!=b:
            change='修改'
            if a is None:
                change='新增' if before.get('walk_complete') and name not in before.get('seen',[]) else '内容未完整核对'
            elif b is None:
                change='删除' if after.get('walk_complete') and name not in after.get('seen',[]) else '内容未完整核对'
            rows.append({'path':name,'change':change})
    chunks=[]
    preview_count=0
    preview_bytes=0
    for row in rows:
        name=row['path']
        old=before.get('texts',{}).get(name, '' if row['change']=='新增' else None)
        new=after.get('texts',{}).get(name, '' if row['change']=='删除' else None)
        if old is None or new is None or not export_allowed(name):
            row['preview_note']='内容未保存为可预览文本（旧快照、二进制、大小上限、编码或隐私限制）；请在本机检查原文件。'
            continue
        before_name='/dev/null' if row['change']=='新增' else 'a/'+name
        after_name='/dev/null' if row['change']=='删除' else 'b/'+name
        lines=difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),fromfile=before_name,tofile=after_name)
        diff=''.join(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n' for line in lines)
        if not diff:
            diff=f"{row['change']}空文件：{name}\n"
        if preview_bytes+len(diff.encode('utf-8'))>2000000:
            row['preview_note']='差异预览达到合计 2 MB 上限；文件清单仍保留。'
            continue
        chunks.append(diff)
        preview_bytes+=len(diff.encode('utf-8'))
        if preview_count<30:
            row['preview']=diff[:4000]
            row['preview_note']='仅展示开头 4000 字符，完整预览见 changes.diff。' if len(diff)>4000 else '运行前后内容预览，不改变 Git 索引。'
            preview_count+=1
        else:
            row['preview_note']='页面只展开前 30 个文件的预览，完整预览见 changes.diff。'
    result={'files':rows,'complete':before['complete'] and after['complete'],
            'note':after['note']+' 内容对比基于本次运行前后的快照，不包含此前已有的修改；无法区分并行人工修改。差异用于审阅，生成预览不改写项目文件。'}
    if chunks:
        atomic(folder/'changes.diff','\n'.join(chunks))
    else:
        result['note']+=' 没有可安全导出的文本差异；不会生成空的差异下载。'
    save_json(folder/'changes.json',result)
    return result
