"""Public instance directory and local readiness, never solution patches."""
import json
import shutil
import subprocess
import time
from pathlib import Path

from .bench import DATASETS,dataset_file,docker,image_name,valid_instance
from .common import CONFIG_ROOT,ROOT,STATE,now,read_json,recover_json,save_json
from .benchmarks import check_snapshot,is_pinned,is_pro,is_rebench,snapshot,validate_dataset

_images={'expires':0,'value':{}}
_pro_cache={}


def page_rows(dataset: str,offset: int,length: int=100) -> dict:
    import requests
    validate_dataset(dataset)
    if is_pinned(dataset):
        raise ValueError('固定版本题集使用专用读取器，不使用浮动版本分页接口。')
    response=requests.get('https://datasets-server.huggingface.co/rows',params={
        'dataset':DATASETS[dataset],'config':'default','split':'test','offset':offset,'length':length},timeout=(8,30))
    response.raise_for_status()
    data=response.json()
    if not isinstance(data.get('rows'),list):
        raise ValueError('官方目录响应无效；旧目录未替换。')
    return data


def public_row(row: dict,index: int,dataset: str='verified') -> dict:
    valid_instance(row['instance_id'],dataset)
    description=row.get('problem_statement','')
    if not isinstance(description,str) or not description.strip():
        raise ValueError('题目描述缺失，旧目录未替换。')
    if is_rebench(dataset):
        from .rebench_data import solver_input
        description=solver_input(row)
    if is_pro(dataset):
        description='\n\n'.join([description,*[str(row[key]) for key in ('requirements','interface') if row.get(key)]])
    return {'instance_id':row['instance_id'],'repo':str(row.get('repo','')),'title':description.strip().splitlines()[0][:200],
            'problem_statement':description[:20000],'description_truncated':len(description)>20000,
            'difficulty':str(row.get('difficulty','')),'created_at':str(row.get('created_at','')),
            'image':image_name(row,dataset),'row_index':index}


def directory(dataset: str) -> dict:
    validate_dataset(dataset)
    data=recover_json(STATE/'catalog'/(dataset+'.json'),None)
    if not isinstance(data,dict) or not isinstance(data.get('items'),list):
        return {'dataset':dataset,'items':[],'synced_at':None,'note':'尚无可用目录，请先同步。同步不会下载镜像。'}
    if is_pinned(dataset):check_snapshot(dataset,data.get('benchmark_snapshot'))
    return data


def synchronize(dataset: str,emit) -> dict:
    rows=[]
    seen=set()
    offset=0
    expected=None
    if is_pinned(dataset):
        if is_pro(dataset):
            from .pro_data import catalog_rows
        else:
            from .rebench_data import catalog_rows
        source=catalog_rows(emit)
        rows=[public_row(row,index,dataset) for index,row in enumerate(source)]
        if len({row['instance_id'] for row in rows})!=len(rows):
            raise ValueError('目录存在重复题号；原目录保留。')
        result={'dataset':dataset,'source':DATASETS[dataset],'benchmark_snapshot':snapshot(dataset),
                'synced_at':now(),'items':rows,'note':'固定版本公开目录；不含标准补丁，镜像与完整题目资料按需另行下载。'}
        save_json(STATE/'catalog'/(dataset+'.json'),result)
        return {'dataset':dataset,'count':len(rows),'synced_at':result['synced_at']}
    while True:
        data=page_rows(dataset,offset)
        total=data.get('num_rows_total')
        if isinstance(total,int):
            if expected is not None and expected!=total:
                raise ValueError('同步期间目录数量发生变化，请重试；旧目录保留。')
            expected=total
        batch=data['rows']
        if not batch:
            if expected is not None and len(rows)!=expected:
                raise ValueError('目录下载未完成，旧目录保留。')
            break
        for index,entry in enumerate(batch):
            item=public_row(entry['row'],entry.get('row_idx',offset+index))
            if item['instance_id'] in seen:
                raise ValueError('目录出现重复题号，旧目录保留。')
            seen.add(item['instance_id']);rows.append(item)
        offset+=len(batch)
        emit('phase',f'同步目录：已读取 {offset} 题'+(f' / {expected}' if expected is not None else ''))
        if expected is not None and offset>=expected:
            break
        if expected is None and len(batch)<100:
            break
    if not rows or (expected is not None and len(rows)!=expected):
        raise ValueError('目录为空或不完整，旧目录保留。')
    result={'dataset':dataset,'source':DATASETS[dataset],'synced_at':now(),'items':rows,
            'note':'仅保存公开题目信息；不包含标准补丁，不下载运行镜像。'}
    save_json(STATE/'catalog'/(dataset+'.json'),result)
    return {'dataset':dataset,'count':len(rows),'synced_at':result['synced_at']}


def suggestions(dataset: str, search: str = '') -> dict:
    catalog = directory(dataset)
    terms = search.casefold().split()
    rows = [row for row in catalog['items'] if all(term in (row['instance_id']+' '+row['repo']+' '+row['problem_statement']).casefold() for term in terms)]
    return {'items':[{k:row[k] for k in ('instance_id','repo','title')} for row in rows[:8]],
            'total':len(rows),'synced':bool(catalog['synced_at'])}


def image_inventory(force=False) -> dict:
    if not force and _images['expires']>time.monotonic():
        return _images['value']
    result={'available':False,'images':{},'storage_note':'镜像由 Docker 管理，不等于项目目录占用；共享层使新增空间无法按镜像大小简单相加。'}
    try:
        probe=docker('info','--format','{{.OSType}}',timeout=5)
        result['probe_returncode']=probe.returncode
        if probe.stdout.strip()=='linux':
            listing=docker('image','ls','--format','{{json .}}',timeout=8)
            result['list_returncode']=listing.returncode
            if listing.returncode==0:
                result['available']=True
                for line in listing.stdout.splitlines():
                    row=json.loads(line)
                    result['images'][(row['Repository']+':'+row['Tag']).removeprefix('docker.io/')]=row.get('Size','未知')
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        result['probe_error']=type(exc).__name__
    _images.update(value=result,expires=time.monotonic()+15)
    return result


def _file_stamp(path: Path):
    try:
        value=path.stat()
    except FileNotFoundError:
        return None
    return value.st_size,value.st_mtime_ns,value.st_ctime_ns


def data_cache_state(dataset: str,instance: str) -> dict:
    path=dataset_file(dataset,instance)
    if not path.is_file():
        return {'data_cached':False,'data_cache_state':'missing','data_cache_note':'题目尚未缓存。'}
    if not is_pro(dataset):
        return {'data_cached':True,'data_cache_state':'ready','data_cache_note':''}
    from .pro_data import ASSET_FILES,asset_dir,solver_input
    stamp=None
    try:
        paths=[path,*[asset_dir({'instance_id':instance})/name for name in ASSET_FILES]]
        stamp=tuple(_file_stamp(item) for item in paths)
        previous=_pro_cache.get(str(path))
        if previous and previous[0]==stamp:
            return dict(previous[1])
        from .bench import load_instance
        solver_input(load_instance(dataset,instance))
        status={'data_cached':True,'data_cache_state':'ready','data_cache_note':''}
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        status={'data_cached':False,'data_cache_state':'incomplete','data_cache_note':'题目资料不完整或已变化，请重新缓存题目。'}
    if stamp is not None:
        _pro_cache[str(path)]=(stamp,status)
    return dict(status)


def readiness(dataset: str,instance: str,records: list[dict],images: dict) -> dict:
    path=dataset_file(dataset,instance)
    row=next((x for x in directory(dataset)['items'] if x['instance_id']==instance),None)
    if row is None and path.is_file():
        cached=read_json(path)
        full=next((x for x in cached if x['instance_id']==instance),None)
        if full:row=public_row(full,0,dataset)
    if row is None:
        raise ValueError('目录中没有该题，请同步目录或核对题号。')
    relevant=[r for r in records if r.get('instance_id')==instance and r.get('dataset')==dataset]
    latest=next((r for r in sorted(relevant,key=lambda x:x['created'],reverse=True) if r.get('evaluation')),None)
    from .patches import catalog,source_paths
    patches=catalog(instance,source_paths(relevant,dataset=dataset),dataset=dataset)
    reference=row['image'].removeprefix('docker.io/')
    return row | data_cache_state(dataset,instance) | {'dataset':dataset,
                  'environment':'unknown' if not images['available'] else 'cached' if reference in images['images'] else 'missing',
                  'image_size':images['images'].get(reference),'has_patch':bool(patches['items']),
                  'patches':patches,'last_evaluation':latest.get('evaluation') if latest else None,
                  'history':[{'id':r['id'],'title':r['title'],'status':r['status']} for r in sorted(relevant,key=lambda x:x['created'],reverse=True)[:20]]}


def listing(dataset: str,records: list[dict],search='',cached='all',patch='all',result='all',offset=0,environment_state='all',limit=8) -> dict:
    catalog=directory(dataset)
    images=image_inventory()
    patch_ids={r['instance_id'] for r in records if r.get('dataset')==dataset and r.get('prediction') and Path(r['prediction']).is_file()}
    from .patches import source_paths,inspect_patch
    for path,identity in source_paths([],dataset=dataset):
        if path.stat().st_size>20_000_000:continue
        try:
            if path.name.endswith('.traj.json'):
                data=read_json(path)
                identity=data.get('instance_id') or data.get('info',{}).get('instance_id') or path.name.removesuffix('.traj.json')
                if inspect_patch(path,identity,dataset=dataset)['usable']:patch_ids.add(identity)
            else:
                data=[json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
                for value in data:
                    identity=value.get('instance_id','')
                    if inspect_patch(path,identity,dataset=dataset)['usable']:patch_ids.add(identity)
        except (OSError,ValueError,TypeError,AttributeError):
            continue
    evaluations={}
    for run in sorted(records,key=lambda x:x['created']):
        if run.get('dataset')==dataset and run.get('evaluation'):evaluations[run['instance_id']]=run['evaluation']
    rows=[]
    for item in catalog['items']:
        if search.lower() not in (item['instance_id']+' '+item['repo']+' '+item['problem_statement']).lower():continue
        data_state=data_cache_state(dataset,item['instance_id'])
        if cached!='all' and data_state['data_cached']!=(cached=='yes'):continue
        reference=item['image'].removeprefix('docker.io/')
        row=item | data_state | {'dataset':dataset,
                    'environment':'unknown' if not images['available'] else 'cached' if reference in images['images'] else 'missing',
                    'image_size':images['images'].get(reference),'has_patch':item['instance_id'] in patch_ids,
                    'last_evaluation':evaluations.get(item['instance_id'])}
        if environment_state!='all' and row['environment']!=environment_state:continue
        if patch!='all' and row['has_patch']!=(patch=='yes'):continue
        category=(row['last_evaluation'] or {}).get('category','none')
        if result!='all' and category!=result:continue
        rows.append({k:v for k,v in row.items() if k not in {'problem_statement','patches','history'}})
    if limit not in {8,16,25,50}:raise ValueError('每页数量请选择 8、16、25 或 50。')
    offset=min(max(0,offset),max(0,(len(rows)-1)//limit*limit))
    return {'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,
            'synced_at':catalog['synced_at'],'note':catalog['note'],'docker_available':images['available'],'storage_note':images['storage_note']}


def check_metadata_space():
    STATE.mkdir(parents=True,exist_ok=True)
    require_metadata_space(shutil.disk_usage(STATE).free)


def require_metadata_space(free_bytes: int):
    if free_bytes<10*1024**2:
        raise ValueError('工作台所在磁盘可用空间不足 10 MB，已暂停。Docker 镜像所在存储需另行检查。')
