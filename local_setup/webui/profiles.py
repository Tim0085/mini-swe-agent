"""Local OpenAI-compatible connections and immutable task model snapshots."""

import copy
import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path

import yaml
from dotenv import dotenv_values

from .common import CONFIG_ROOT, PRIVATE_VALUES, ROOT, STATE, assert_no_credentials, atomic, file_version, new_id, read_json, recover_json, save_json
from .settings import Settings, credential_status, inspect_settings, load_secret, paths, runtime_call_limit

LOCK = threading.RLock()
PROBE_SESSION = new_id()
PROBE_ACTIVE = set()
PROBE_UNSAVED = {}


def probe_record(record, state=STATE):
    identity = (str(state), record.get('request_id'))
    value = copy.deepcopy(PROBE_UNSAVED.get(identity, record))
    if value.get('status') == 'running' and (value.get('session') != PROBE_SESSION or identity not in PROBE_ACTIVE):
        value.update(status='incomplete', error='后台不再执行此验证，结果未确认。请核对服务商记录后再决定是否重新验证。')
    return value


def latest_probes(state):
    latest = {}
    order = lambda record: (record.get('created', '')[:19], record.get('created_order', 0), record.get('finished', ''), record.get('request_id', ''))
    for path in (state / 'profiles/tests').glob('*.json'):
        record = recover_json(path, None)
        if not isinstance(record, dict) or not record.get('profile_id'):
            continue
        record = probe_record(record, state)
        old = latest.get(record['profile_id'])
        if old is None or order(record) > order(old):
            latest[record['profile_id']] = record
    return latest
PRESETS = [{'id': name.lower(), 'name': name, 'protocol': 'chat', 'model_name': '', 'api_base': base} for name, base in [
    ('OpenAI', 'https://api.openai.com/v1'), ('DeepSeek', 'https://api.deepseek.com'),
    ('Claude', 'https://api.anthropic.com/v1'), ('Grok', 'https://api.x.ai/v1'),
    ('GLM', 'https://open.bigmodel.cn/api/paas/v4'), ('Kimi', 'https://api.moonshot.ai/v1'), ('Custom', '')]]
PARAMETERS = {'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'reasoning_effort', 'extra_body'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _index(state):
    path = state / 'profiles/index.json'
    if not path.exists():
        return {'connections': {}, 'profiles': {}, 'default_profile_id': 'legacy'}
    value = read_json(path)
    if not isinstance(value.get('connections'), dict) or not isinstance(value.get('profiles'), dict):
        raise ValueError('模型档案文件损坏，请保留文件并恢复备份。')
    return value


def _save(value, state):
    save_json(state / 'profiles/index.json', value)


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise ValueError('档案编号无效。')
    return value


def _name(value):
    value = str(value or '').strip()
    if not value or len(value) > 120:
        raise ValueError('请输入不超过 120 字的名称。')
    assert_no_credentials(value)
    return value


def _secret_path(connection_id, revision, state):
    return state / 'profiles/credentials' / _id(connection_id) / (_id(revision) + '.dpapi')


def _encrypt(value):
    from credentials import _powershell
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or '\n' in value or '\r' in value:
        raise ValueError('密钥为空或格式无效。')
    value = value.strip()
    if value not in PRIVATE_VALUES:
        PRIVATE_VALUES.append(value)
    encrypted = _powershell('$ErrorActionPreference="Stop"; $s=ConvertTo-SecureString ([Console]::In.ReadToEnd()) -AsPlainText -Force; [Console]::Write((ConvertFrom-SecureString $s))', value)
    if _decrypt(encrypted) != value:
        raise ValueError('密钥加密验证失败，原凭据未修改。')
    return encrypted


def _decrypt(value):
    from credentials import _powershell
    key = _powershell('$ErrorActionPreference="Stop"; $s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); [Console]::Write([Net.NetworkCredential]::new("",$s).Password)', value)
    if key and key not in PRIVATE_VALUES:
        PRIVATE_VALUES.append(key)
    return key


def _legacy_credential_version(root):
    env_path, _, key_path = paths(root)
    if key_path.exists():
        return file_version(key_path)
    return digest(dotenv_values(env_path).get('OPENAI_API_KEY') or '')


def _legacy(root):
    settings = inspect_settings(root)
    connection = {'id': 'legacy', 'name': '原配置连接', 'api_base': settings['api_base'], 'provider': 'custom',
                  'enabled': True, 'credential_version': _legacy_credential_version(root)}
    connection['version'] = digest({k: connection[k] for k in ['api_base', 'enabled']})
    try:
        raw = yaml.safe_load(paths(root)[1].read_text(encoding='utf-8-sig')) if paths(root)[1].exists() else {}
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, yaml.YAMLError):
        raw = {}
    raw_model = raw.get('model', {})
    raw_kwargs = raw_model.get('model_kwargs', {}) if isinstance(raw_model, dict) else {}
    parameters = {k: v for k, v in raw_kwargs.items() if k in PARAMETERS} if isinstance(raw_kwargs, dict) else {}
    profile = {'id': 'legacy', 'name': '原配置', 'connection_id': 'legacy', 'model_name': settings['model_name'],
               'protocol': 'responses' if settings['model_class'] == 'litellm_response' else 'chat', 'parameters': validate_parameters(parameters),
               'pricing': {'mode': 'builtin'}, 'enabled': True, 'limits': {k: settings[k] for k in ['step_limit', 'cost_limit', 'timeout']}}
    profile['version'] = digest(profile)
    return connection, profile, settings


def list_profiles(*, state=STATE, root=CONFIG_ROOT):
    index = _index(state)
    connection, profile, settings = _legacy(root)
    connections = [connection, *index['connections'].values()]
    result = []
    for item in connections:
        value = copy.deepcopy(item)
        if value['id'] == 'legacy':
            value.update({k: settings[k] for k in ['key_configured', 'key_state', 'key_note']})
        else:
            revision = value.get('credential_version')
            value['key_configured'] = bool(revision and _secret_path(value['id'], revision, state).is_file())
            value['key_state'] = 'configured' if value['key_configured'] else 'missing'
        result.append(value)
    profiles = [profile, *copy.deepcopy(list(index['profiles'].values()))]
    tests = latest_probes(state)
    for item in profiles:
        item['api_model_id'] = item['model_name'].removeprefix('openai/')
        if item['id'] == 'legacy':
            item['last_test'] = index.get('legacy_last_test')
        record = tests.get(item['id'])
        if record:
            test_result = record.get('result') or {}
            item['last_test'] = test_result | {'request_id': record.get('request_id'),
                'status': test_result.get('status', record.get('status', 'incomplete')),
                'fingerprint': test_result.get('fingerprint', record.get('fingerprint')),
                'checked_at': test_result.get('checked_at', record.get('finished', record.get('created'))),
                'message': test_result.get('message') or record.get('error') or record.get('note', '')}
        item['pricing_available'] = pricing_available(resolve_pricing(item['model_name'], item['pricing']))
        item['test_current'] = False
        item['execution_fingerprint'] = None
        if item.get('enabled', True):
            try:
                item['execution_fingerprint'] = freeze_profile(item['id'], state=state, root=root)['fingerprint']
                item['test_current'] = bool(item.get('last_test') and item['last_test'].get('fingerprint') == item['execution_fingerprint'])
            except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError):
                item['issue'] = '所引用配置不可用，请修复后再运行。'
    return {'connections': result, 'profiles': profiles, 'default_profile_id': index['default_profile_id'], 'presets': PRESETS}


def save_connection(data, *, state=STATE, root=CONFIG_ROOT):
    with LOCK:
        if 'key' in data or 'api_key' in data:
            raise ValueError('请通过独立密钥入口保存凭据，连接配置不能包含密钥。')
        index = _index(state)
        connection_id = _id(data.get('id') or new_id())
        if connection_id == 'legacy':
            raise ValueError('原配置连接请通过原配置设置修改；可新建独立连接。')
        old = index['connections'].get(connection_id)
        if old and data.get('version') != old['version']:
            raise ValueError('连接已变化，请重新加载后保存。')
        name = _name(data.get('name'))
        api_base = Settings(model_name='validation', api_base=data.get('api_base', '')).api_base
        value = {'id': connection_id, 'name': name, 'api_base': api_base, 'provider': str(data.get('provider') or 'custom'),
                 'enabled': bool(data.get('enabled', True)), 'credential_version': (old or {}).get('credential_version', '')}
        value['version'] = digest({k: v for k, v in value.items() if k != 'credential_version'})
        value['edit_version'] = digest(value)
        # The external edit token must change on a key-only edit as well.
        value['version'] = value.pop('edit_version')
        assert_no_credentials(value)
        index['connections'][connection_id] = value
        _save(index, state)
        return copy.deepcopy(value)


def validate_parameters(value):
    if not isinstance(value, dict) or set(value) - PARAMETERS:
        raise ValueError('生成参数仅支持 temperature、top_p、max_tokens、max_completion_tokens、reasoning_effort、extra_body。')
    for key in ['temperature', 'top_p']:
        if key in value and (not isinstance(value[key], (int, float)) or not math.isfinite(value[key]) or not 0 <= value[key] <= (2 if key == 'temperature' else 1)):
            raise ValueError('采样参数超出范围。')
    for key in ['max_tokens', 'max_completion_tokens', 'max_output_tokens']:
        if key in value and (type(value[key]) is not int or not 1 <= value[key] <= 200000):
            raise ValueError('输出 token 上限无效。')
    if sum(key in value for key in ['max_tokens', 'max_completion_tokens', 'max_output_tokens']) > 1:
        raise ValueError('只可设置一种输出 token 上限参数。')
    if 'reasoning_effort' in value and value['reasoning_effort'] not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}:
        raise ValueError('推理强度值无效。')
    def inspect(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if re.sub(r'[^a-z]', '', key.lower()) in {'apikey', 'authorization', 'key', 'secret', 'token', 'baseurl', 'apibase', 'model', 'tools', 'messages', 'input', 'toolchoice', 'stream', 'headers', 'extraheaders'}:
                    raise ValueError('额外参数不能包含密钥、连接地址、模型或工具消息。')
                inspect(child)
        elif isinstance(item, list):
            for child in item:
                inspect(child)
    if 'extra_body' in value and not isinstance(value['extra_body'], dict):
        raise ValueError('extra_body 必须为对象。')
    if any(key in value.get('extra_body', {}) for key in ['max_tokens', 'max_completion_tokens', 'max_output_tokens']):
        raise ValueError('输出上限请使用独立参数，不能藏在 extra_body 中。')
    if set(value.get('extra_body', {})) & (set(value) - {'extra_body'}):
        raise ValueError('额外参数与独立生成参数重复，请只保留一处。')
    inspect(value)
    assert_no_credentials(value)
    if len(json.dumps(value, allow_nan=False)) > 10000:
        raise ValueError('生成参数过长。')
    return copy.deepcopy(value)


def save_connection_key(connection_id, key, version, *, state=STATE, root=CONFIG_ROOT):
    with LOCK:
        if connection_id == 'legacy':
            raise ValueError('原配置的密钥请在原配置页更新。')
        index = _index(state)
        current = index['connections'].get(_id(connection_id))
        if not current or current['version'] != version:
            raise ValueError('连接已变化，请重新加载。')
        revision = new_id()
        atomic(_secret_path(connection_id, revision, state), _encrypt(key))
        current['credential_version'] = revision
        current['version'] = digest({k: v for k, v in current.items() if k != 'version'})
        _save(index, state)
        return copy.deepcopy(current)


def fetch_models(connection_id, *, state=STATE, root=CONFIG_ROOT):
    import httpx
    if connection_id == 'legacy':
        connection, _, _ = _legacy(root)
        key = load_secret(root)
    else:
        connection = _index(state)['connections'].get(_id(connection_id))
        if not connection or not connection.get('enabled', True):
            raise ValueError('服务连接不存在或已停用。')
        revision = connection.get('credential_version')
        key = _decrypt(_secret_path(connection_id, revision, state).read_text(encoding='ascii')) if revision else ''
    if not key:
        raise ValueError('请先保存服务密钥。')
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(connection['api_base'].rstrip('/') + '/models', headers={'Authorization': 'Bearer ' + key})
        if response.status_code >= 400:
            raise ValueError(f'模型目录返回 HTTP {response.status_code}；仍可手工填写服务商模型 ID。')
        models = response.json().get('data', [])
        result = sorted({x['id'] for x in models if isinstance(x, dict) and isinstance(x.get('id'), str) and re.fullmatch(r'[A-Za-z0-9_./:@+-]{1,180}', x['id'])})
        return {'connection_id': connection_id, 'models': result[:3000], 'note': '目录只表示服务列出该模型，不代表工具调用已验证。'}
    except httpx.HTTPError:
        raise ValueError('无法读取服务模型目录；仍可手工填写模型 ID。') from None


def validate_pricing(value):
    mode = value.get('mode', 'builtin')
    if mode not in {'builtin', 'custom', 'free'}:
        raise ValueError('计价模式无效。')
    result = {'mode': mode, 'currency': 'USD'}
    if mode == 'custom':
        for key in ['input_per_million', 'output_per_million', 'cached_input_per_million']:
            price = value.get(key)
            if price is None and key == 'cached_input_per_million':
                continue
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
                raise ValueError('自定义价格应为非负美元金额，单位是每百万 token。')
            result[key] = float(price)
        if result['input_per_million'] + result['output_per_million'] == 0:
            raise ValueError('免费模型请选择明确免费，不要用未知价格填零。')
    return result


def save_profile(data, *, state=STATE, root=CONFIG_ROOT):
    with LOCK:
        index = _index(state)
        profile_id = _id(data.get('id') or new_id())
        if profile_id == 'legacy':
            raise ValueError('原配置档案请通过原配置设置修改；可复制为新档案。')
        old = index['profiles'].get(profile_id)
        if old and data.get('version') != old['version']:
            raise ValueError('模型档案已变化，请重新加载。')
        connection_id = _id(data.get('connection_id', ''))
        if connection_id != 'legacy' and connection_id not in index['connections']:
            raise ValueError('请先选择已有服务连接。')
        protocol = data.get('protocol', 'chat')
        if protocol not in {'chat', 'responses'}:
            raise ValueError('仅支持 OpenAI Chat Completions 或 Responses。')
        defaults = inspect_settings(root)
        limits = {key: data.get('limits', {}).get(key, defaults[key]) for key in ['step_limit', 'cost_limit', 'timeout']}
        validated = Settings(model_name=data.get('model_name', ''), api_base='https://validation.invalid', **limits)
        parameters = validate_parameters(data.get('parameters', data.get('generation_params', {})))
        if protocol == 'chat' and 'max_output_tokens' in parameters:
            raise ValueError('Chat 协议请使用 max_tokens 或 max_completion_tokens。')
        value = {'id': profile_id, 'name': _name(data.get('name')), 'connection_id': connection_id,
                 'model_name': validated.model_name, 'protocol': protocol, 'provider': str(data.get('provider') or 'custom'),
                 'parameters': parameters,
                 'pricing': validate_pricing(data.get('pricing', {'mode': 'builtin'})), 'enabled': bool(data.get('enabled', True)),
                 'limits': {key: getattr(validated, key) for key in limits}}
        value['version'] = digest(value)
        index['profiles'][profile_id] = value
        _save(index, state)
        return copy.deepcopy(value)


def set_default(profile_id, *, state=STATE, root=CONFIG_ROOT):
    with LOCK:
        freeze_profile(profile_id, state=state, root=root)
        index = _index(state)
        index['default_profile_id'] = profile_id
        _save(index, state)
        return {'default_profile_id': profile_id}


def resolve_pricing(model_name, pricing):
    result = validate_pricing(pricing)
    if result['mode'] == 'free':
        return result | {'input_per_million': 0.0, 'output_per_million': 0.0, 'source': 'explicit_free', 'available': True}
    if result['mode'] == 'custom':
        return result | {'source': 'custom', 'available': True}
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    import litellm
    info = litellm.model_cost.get(model_name) or litellm.model_cost.get(model_name.removeprefix('openai/')) or {}
    for key, original in [('input_per_million', 'input_cost_per_token'), ('output_per_million', 'output_cost_per_token'), ('cached_input_per_million', 'cache_read_input_token_cost')]:
        price = info.get(original)
        if isinstance(price, (int, float)) and math.isfinite(price) and price >= 0:
            result[key] = price * 1000000
    return result | {'source': 'local_litellm_catalog', 'available': all(result.get(k, 0) > 0 for k in ['input_per_million', 'output_per_million'])}


def pricing_available(pricing):
    return bool(pricing.get('available'))


def profile_price_available(profile='legacy', *, state=STATE, root=CONFIG_ROOT):
    return pricing_available(profile['pricing'] if isinstance(profile, dict) else freeze_profile(profile, state=state, root=root)['pricing'])


def _configs(root):
    from minisweagent.utils.serialize import recursive_merge
    read = lambda p: yaml.safe_load(p.read_text(encoding='utf-8-sig'))
    local = recursive_merge(read(ROOT / 'src/minisweagent/config/mini.yaml'), read(root / 'local_setup/windows.yaml'))
    benchmark = read(ROOT / 'src/minisweagent/config/benchmarks/swebench.yaml')
    # Connection-specific model options belong in the profile, never in a shared template snapshot.
    for config in [local, benchmark]:
        config['model'] = {k: v for k, v in config.get('model', {}).items() if k in {'observation_template', 'format_error_template', 'multimodal_regex'}}
    assert_no_credentials([local, benchmark])
    return {'local': local, 'swebench': benchmark}


def freeze_profile(profile_id='legacy', limits=None, *, state=STATE, root=CONFIG_ROOT):
    index = _index(state)
    legacy_connection, legacy_profile, settings = _legacy(root)
    profile_id = profile_id or index['default_profile_id']
    profile = legacy_profile if profile_id == 'legacy' else index['profiles'].get(_id(profile_id))
    if not profile or not profile.get('enabled', True):
        raise ValueError('模型档案不存在或已停用。')
    connection = legacy_connection if profile['connection_id'] == 'legacy' else index['connections'].get(profile['connection_id'])
    if not connection or not connection.get('enabled', True):
        raise ValueError('服务连接不存在或已停用。')
    configured = Settings(model_name=profile['model_name'], api_base=connection['api_base'], **(profile['limits'] | (limits or {})))
    snapshot = {'schema_version': 1, 'profile_id': profile_id, 'profile_name': profile['name'], 'profile_version': profile['version'],
                'connection_id': connection['id'], 'connection_name': connection['name'], 'connection_version': connection['version'],
                'credential_version': connection['credential_version'], 'model_name': profile['model_name'], 'api_base': connection['api_base'],
                'protocol': profile['protocol'], 'parameters': copy.deepcopy(profile['parameters']),
                'limits': {k: getattr(configured, k) for k in ['step_limit', 'cost_limit', 'timeout']},
                'pricing': resolve_pricing(profile['model_name'], profile['pricing']),
                'runtime': {'call_limit': runtime_call_limit(configured.step_limit, root), 'model_retries': 1,
                            'global_cost_limit': os.getenv('MSWEA_GLOBAL_COST_LIMIT') or dotenv_values(paths(root)[0]).get('MSWEA_GLOBAL_COST_LIMIT') or '0'}, 'configs': _configs(root)}
    global_cost = float(snapshot['runtime']['global_cost_limit'])
    if not math.isfinite(global_cost) or global_cost < 0:
        raise ValueError('附加全局费用上限无效，请先检查原配置。')
    snapshot['configuration_fingerprint'] = digest({k: snapshot[k] for k in ['profile_id', 'model_name', 'api_base', 'protocol', 'parameters', 'limits', 'pricing', 'runtime', 'configs']})
    snapshot['fingerprint'] = digest(snapshot)
    assert_no_credentials(snapshot)
    return snapshot


def snapshot_settings(snapshot):
    return {'model_name': snapshot['model_name'], 'api_base': snapshot['api_base'],
            'model_class': 'litellm_response' if snapshot['protocol'] == 'responses' else 'litellm', **snapshot['limits']}


def check_snapshot(snapshot, *, state=STATE, root=CONFIG_ROOT):
    try:
        current = freeze_profile(snapshot['profile_id'], snapshot['limits'], state=state, root=root)
    except (ValueError, OSError, KeyError):
        return {'changed': True, 'credentials_only': False, 'reason': '引用档案、连接或配置不可用，请重新配置。'}
    configuration_changed = current['configuration_fingerprint'] != snapshot['configuration_fingerprint'] or current['profile_version'] != snapshot['profile_version']
    credentials_changed = current['credential_version'] != snapshot['credential_version']
    return {'changed': configuration_changed or credentials_changed, 'credentials_only': credentials_changed and not configuration_changed,
            'reason': '模型参数、价格或服务配置已变化，请重新建立任务。' if configuration_changed else '密钥已更新，请确认剩余任务继续使用更新后的凭据。' if credentials_changed else ''}


def refresh_credentials(snapshot, *, state=STATE, root=CONFIG_ROOT):
    status = check_snapshot(snapshot, state=state, root=root)
    if status['changed'] and not status['credentials_only']:
        raise ValueError(status['reason'])
    value = copy.deepcopy(snapshot)
    if status['credentials_only']:
        current = freeze_profile(snapshot['profile_id'], snapshot['limits'], state=state, root=root)
        value.update({k: current[k] for k in ['credential_version', 'connection_version']})
        value['fingerprint'] = digest({k: v for k, v in value.items() if k != 'fingerprint'})
    return value


def load_snapshot_secret(snapshot, *, state=STATE, root=CONFIG_ROOT):
    before = check_snapshot(snapshot, state=state, root=root)
    if before['changed']:
        raise ValueError(before['reason'])
    if snapshot['connection_id'] == 'legacy':
        key = load_secret(root)
    elif snapshot['credential_version']:
        key = _decrypt(_secret_path(snapshot['connection_id'], snapshot['credential_version'], state).read_text(encoding='ascii'))
    else:
        key = ''
    if not key:
        raise ValueError('所选服务尚未配置密钥。')
    after = check_snapshot(snapshot, state=state, root=root)
    if after['changed']:
        raise ValueError(after['reason'])
    return key


def test_profile(profile_id, *, confirmed=False, max_output_tokens=128, budget_limit=.05, snapshot=None, state=STATE, root=CONFIG_ROOT, persist=True):
    if not confirmed:
        raise ValueError('工具往返测试会调用模型，必须先明确确认。')
    from .profile_model import tool_roundtrip
    snapshot = copy.deepcopy(snapshot) if snapshot is not None else freeze_profile(profile_id, state=state, root=root)
    if snapshot.get('profile_id') != profile_id:
        raise ValueError('测试预览与所选模型不一致，请重新预览。')
    result = tool_roundtrip(snapshot, load_snapshot_secret(snapshot, state=state, root=root), max_output_tokens=max_output_tokens, budget_limit=budget_limit)
    if not persist:
        return result
    with LOCK:
        index = _index(state)
        if profile_id != 'legacy' and profile_id in index['profiles']:
            index['profiles'][profile_id]['last_test'] = result
            _save(index, state)
        elif profile_id == 'legacy':
            index['legacy_last_test'] = result
            _save(index, state)
    return result
