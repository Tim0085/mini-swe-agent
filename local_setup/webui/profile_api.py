"""Authenticated profile management and explicitly confirmed capability probes."""
import secrets
import time
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .common import STATE, now, recover_json, redact, save_json
from . import profiles
from .profile_model import protocol_parameters


class ProbeCommit(BaseModel):
    token: str
    request_id: str=Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')


class ProbeClose(BaseModel):
    confirmed: bool = False


def profile_router(manager):
    router=APIRouter()
    previews={}

    def store_result(path, record):
        identity = (str(STATE), record['request_id'])
        try:
            save_json(path, record)
        except OSError:
            record = record | {'persistence_error': '验证已结束，但记录暂未写入磁盘。请保持后台开启并查询结果，系统会重试保存，不会重复调用模型。'}
            profiles.PROBE_UNSAVED[identity] = record
        else:
            profiles.PROBE_UNSAVED.pop(identity, None)
        return record

    def read_record(profile_id, request_id):
        if not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', request_id):
            raise HTTPException(404, '验证记录不存在。')
        path = STATE / 'profiles/tests' / (request_id + '.json')
        record = profiles.PROBE_UNSAVED.get((str(STATE), request_id)) or recover_json(path, None)
        if record and record.get('profile_id') != profile_id:
            raise HTTPException(404, '验证记录不存在。')
        if record:
            record = profiles.probe_record(record)
            if record.get('persistence_error'):
                record = store_result(path, {k: v for k, v in record.items() if k != 'persistence_error'})
        return path, record

    @router.get('/api/profiles')
    def listing():
        with manager.lock:return profiles.list_profiles()

    @router.post('/api/connections')
    def create_connection(body: dict):
        with manager.lock:return profiles.save_connection(body)

    @router.put('/api/connections/{connection_id}')
    def update_connection(connection_id: str,body: dict):
        with manager.lock:return profiles.save_connection(body | {'id':connection_id})

    @router.put('/api/connections/{connection_id}/key')
    def update_key(connection_id: str,body: dict):
        with manager.lock:return profiles.save_connection_key(connection_id,body.get('key',''),body.get('version',''))

    @router.get('/api/connections/{connection_id}/models')
    def models(connection_id: str):
        return profiles.fetch_models(connection_id)

    @router.post('/api/profiles')
    def create_profile(body: dict):
        with manager.lock:return profiles.save_profile(body)

    @router.put('/api/profiles/default')
    def default_profile(body: dict):
        with manager.lock:return profiles.set_default(body.get('profile_id',''))

    @router.put('/api/profiles/{profile_id}')
    def update_profile(profile_id: str,body: dict):
        with manager.lock:return profiles.save_profile(body | {'id':profile_id})

    @router.post('/api/profiles/{profile_id}/test/preview')
    def preview_probe(profile_id: str):
        snapshot=profiles.freeze_profile(profile_id)
        parameters=protocol_parameters(snapshot,256)
        if not profiles.profile_price_available(snapshot):raise ValueError('请先配置模型价格，才能确认测试费用限制。')
        if not profiles.load_snapshot_secret(snapshot):raise ValueError('请先为连接保存密钥。')
        token=secrets.token_hex(24)
        with manager.lock:
            for key in list(previews):
                if previews[key]['expires']<time.monotonic():previews.pop(key)
            previews[token]={'snapshot':snapshot,'expires':time.monotonic()+600}
        return {'token':token,'profile_id':profile_id,'model_name':snapshot['model_name'],'api_base':snapshot['api_base'],
                'api_model_id':snapshot['model_name'].removeprefix('openai/'),'protocol':snapshot['protocol'],
                'endpoint':snapshot['api_base'].rstrip('/')+('/responses' if snapshot['protocol']=='responses' else '/chat/completions'),
                'parameters':parameters.pop('extra_body',{}) | parameters,
                'steps':2,'max_output_tokens':256,'budget_limit':.05,
                'note':'最多两次生成请求，每次最多 256 输出 token；估算阈值 $0.05，单次请求可能超出。只验证固定工具往返，不执行命令。'}

    @router.get('/api/profiles/{profile_id}/test/{request_id}')
    def probe_status(profile_id: str, request_id: str):
        with manager.lock:
            _,record=read_record(profile_id,request_id)
            if not record:raise HTTPException(404,'尚未找到验证记录。可查询或结束本次等待；不会自动重发。')
            return record

    @router.post('/api/profiles/{profile_id}/test/{request_id}/close')
    def close_probe(profile_id: str, request_id: str, body: ProbeClose):
        if not body.confirmed:raise HTTPException(400,'请确认结束等待不会取消服务商侧已收到的请求。')
        with manager.lock:
            path,record=read_record(profile_id,request_id)
            if (str(STATE), request_id) in profiles.PROBE_ACTIVE:
                raise HTTPException(409,'后台仍在执行这次验证，请等待或查询结果，不能重复派发。')
            if record is None:
                record={'request_id':request_id,'profile_id':profile_id,'status':'incomplete','created':now(),
                        'created_order':time.time_ns(),'finished':now(),'closed':True,
                        'error':'本次等待已结束，迟到的原请求已拦截。重新验证需要重新预览并确认费用。'}
                # Persist the tombstone before releasing the browser; late POSTs must not execute.
                save_json(path,record)
            if record.get('persistence_error'):
                raise HTTPException(409,record['persistence_error'])
            for token in list(previews):
                if previews[token]['snapshot']['profile_id']==profile_id:
                    previews.pop(token)
            return record

    @router.post('/api/profiles/{profile_id}/test')
    def probe(profile_id: str,body: ProbeCommit):
        path=STATE/'profiles/tests'/(body.request_id+'.json')
        with manager.lock:
            _,previous=read_record(profile_id,body.request_id)
            if previous:
                if previous['profile_id']!=profile_id:raise ValueError('请求编号已用于另一模型。')
                return profiles.probe_record(previous)
            plan=previews.get(body.token)
            if not plan or plan['expires']<time.monotonic() or plan['snapshot']['profile_id']!=profile_id:raise ValueError('测试预览已过期，请重新确认。')
            if profiles.check_snapshot(plan['snapshot'])['changed']:raise ValueError('模型配置已变化，请重新确认测试。')
            if manager.active or manager.queues.owns_slot() or getattr(manager,'tool_testing',False) or not manager.accepting:
                raise ValueError('请等待当前任务结束，或暂停队列后再验证模型。')
            record={'request_id':body.request_id,'profile_id':profile_id,'status':'running','created':now(),
                    'created_order':time.time_ns(),
                    'session':profiles.PROBE_SESSION,'fingerprint':plan['snapshot']['fingerprint'],
                    'note':'测试已派发；若服务中断，请先核对服务商账单，不自动重试。'}
            save_json(path,record)
            previews.pop(body.token)
            manager.tool_testing=True
            profiles.PROBE_ACTIVE.add((str(STATE),body.request_id))
        try:
            result=profiles.test_profile(profile_id,confirmed=True,max_output_tokens=256,budget_limit=.05,snapshot=plan['snapshot'],persist=False)
            record.update(status='completed',finished=now(),result=result)
        except Exception as exc:
            record.update(status='incomplete',finished=now(),error=str(redact(str(exc))))
        finally:
            with manager.lock:
                try:record=store_result(path,record)
                finally:
                    profiles.PROBE_ACTIVE.discard((str(STATE),body.request_id))
                    manager.tool_testing=False
        return record

    return router
