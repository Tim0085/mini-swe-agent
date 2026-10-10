import io
import json
import os
import re
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import CONFIG_ROOT, PRIVATE_VALUES, ROOT, STATE, atomic, atomic_bytes, assert_no_credentials, file_version, new_id, read_json, save_json


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_name: str = Field(min_length=1, max_length=180)
    api_base: str = Field(min_length=1, max_length=500)
    model_class: Literal['litellm', 'litellm_response'] = 'litellm'
    step_limit: int = Field(default=40, ge=1, le=1000)
    cost_limit: float = Field(default=3, gt=0, le=1000, allow_inf_nan=False)
    timeout: int = Field(default=120, ge=5, le=3600)

    @field_validator('model_name')
    @classmethod
    def model_name_valid(cls, value):
        value = value.strip()
        if not re.fullmatch(r'[A-Za-z0-9_./:@+-]+', value):
            raise ValueError('模型名含不支持的字符。')
        return value if value.startswith('openai/') else 'openai/' + value

    @field_validator('api_base')
    @classmethod
    def base_valid(cls, value):
        value = value.strip().rstrip('/')
        if re.search(r'''[\s'"\\\x00-\x1f\x7f]''', value):
            raise ValueError('接口地址不能包含引号、空白或反斜杠；特殊路径字符请使用 URL 编码。')
        url = urlsplit(value)
        if url.scheme not in {'https', 'http'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('接口地址应是无密钥、无查询参数的 HTTP(S) URL。')
        if url.scheme == 'http' and url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('远程接口必须使用 HTTPS。')
        return value


def paths(root=CONFIG_ROOT):
    return root / '.local/.env', root / 'local_setup/windows.yaml', root / '.local/api-key.dpapi'


def runtime_call_limit(step_limit: int,root=CONFIG_ROOT) -> str:
    configured=dotenv_values(paths(root)[0])
    explicit=os.getenv('MSWEA_GLOBAL_CALL_LIMIT') or configured.get('MSWEA_GLOBAL_CALL_LIMIT')
    if explicit is not None:
        try:
            if int(explicit)<0:
                raise ValueError
        except (TypeError,ValueError):
            raise ValueError('附加全局调用上限格式无效，请检查 MSWEA_GLOBAL_CALL_LIMIT。')
        return str(explicit)
    # The upstream counter reserves the next call, so permit the requested last step to return.
    return str(step_limit+1)


def read_settings(root=CONFIG_ROOT) -> dict:
    env_path, yaml_path, key_path = paths(root)
    env = dotenv_values(env_path)
    config = yaml.safe_load(yaml_path.read_text(encoding='utf-8-sig'))
    values=Settings(
        model_name=env.get('MSWEA_MODEL_NAME') or 'openai/gpt-5.4-mini',
        api_base=env.get('OPENAI_API_BASE') or 'https://api.gantim.pw/v1',
        model_class=config['model'].get('model_class', 'litellm'),
        step_limit=config['agent'].get('step_limit', 40),
        cost_limit=config['agent'].get('cost_limit', 3),
        timeout=config['model'].get('model_kwargs', {}).get('timeout', 120),
    ).model_dump()
    state=credential_status(root)
    assert_no_credentials(values)
    global_cap=os.getenv('MSWEA_GLOBAL_CALL_LIMIT') or env.get('MSWEA_GLOBAL_CALL_LIMIT')
    note=f'另有显式全局调用限制 {global_cap}，它可能先于页面步数限制生效。' if global_cap and global_cap!='0' else ''
    return values | state | {'valid': True, 'issues': [], 'runtime_limit_note':note, 'version': file_version(*paths(root))}


def load_secret(root=CONFIG_ROOT) -> str:
    from credentials import _powershell
    env_path, _, key_path = paths(root)
    if key_path.is_file():
        key = _powershell('$ErrorActionPreference="Stop"; $s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); [Console]::Write([Net.NetworkCredential]::new("",$s).Password)', key_path.read_text(encoding='ascii'))
    else:
        key = dotenv_values(env_path).get('OPENAI_API_KEY') or ''
    if key and key not in PRIVATE_VALUES:
        PRIVATE_VALUES.append(key)
    return key


@lru_cache(maxsize=8)
def _credential_status(root: Path, fingerprint: str) -> dict:
    try:
        ready = bool(load_secret(root))
        legacy = bool(dotenv_values(paths(root)[0]).get('OPENAI_API_KEY'))
        return {'key_configured':ready, 'key_state':'ready' if ready else 'missing',
                'key_note':'检测到旧 .env 凭据；加密凭据优先，重新保存密钥可完成迁移。' if legacy else ''}
    except Exception:
        return {'key_configured':False, 'key_state':'unreadable', 'key_note':'密钥无法被当前 Windows 用户解密，请重新填写并保存。'}


def credential_status(root=CONFIG_ROOT):
    return _credential_status(root, file_version(paths(root)[0],paths(root)[2])).copy()


def inspect_settings(root=CONFIG_ROOT) -> dict:
    try:
        return read_settings(root)
    except Exception:
        defaults = Settings(model_name='openai/gpt-5.4-mini',api_base='https://api.gantim.pw/v1').model_dump()
        status=credential_status(root)
        try:
            env=dotenv_values(paths(root)[0])
            candidate=Settings(model_name=env.get('MSWEA_MODEL_NAME') or defaults['model_name'],api_base=env.get('OPENAI_API_BASE') or defaults['api_base']).model_dump()
            assert_no_credentials(candidate)
            defaults=candidate
        except Exception:
            pass
        return defaults | status | {'valid':False, 'issues':['配置文件缺失、损坏或字段不兼容。已尽量保留可读取的模型地址，其余为待确认默认值。可恢复备份，或明确重建后再运行。'], 'version':file_version(*paths(root))}


def env_edit(text: str, values: dict[str, str | None]) -> str:
    newline = '\r\n' if '\r\n' in text else '\n'
    for key,value in values.items():
        pattern = rf'(?m)^(?:export )?{re.escape(key)}=[^\r\n]*'
        if value is None:
            text = re.sub(pattern + r'(?:\r?\n|$)', '', text)
            continue
        escaped = value.replace('\\','\\\\').replace("'", "\\'")
        replacement = f"{key}='{escaped}'"
        text,count = re.subn(pattern,lambda _:replacement,text)
        if not count:
            text = text.rstrip('\r\n') + newline + replacement + newline
    parsed = dotenv_values(stream=io.StringIO(text))
    if any(parsed.get(k) != v for k,v in values.items()):
        raise ValueError('配置读回校验失败，原文件未修改。')
    return text


def commit_files(changes: dict[Path, bytes], root=CONFIG_ROOT) -> str:
    backup_id = new_id()
    folder = root / '.local/webui/config-backups' / backup_id
    folder.mkdir(parents=True)
    manifest = []
    for i,(path,content) in enumerate(changes.items()):
        old = path.read_bytes() if path.exists() else None
        if old is not None:
            (folder / f'{i}.original').write_bytes(old)
        manifest.append({'path':str(path.relative_to(root)), 'original':f'{i}.original' if old is not None else None,
                         'before':hashlib.sha256(old).hexdigest() if old is not None else None,
                         'after':hashlib.sha256(content).hexdigest()})
    save_json(folder/'manifest.json',manifest)
    journal = root / '.local/webui/config-transaction.json'
    save_json(journal,{'backup_id':backup_id})
    try:
        for path,content in changes.items():
            atomic_bytes(path,content)
    except OSError:
        for entry in manifest:
            path = root / entry['path']
            expected = entry['after']
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==expected:
                if entry['original']:
                    atomic_bytes(path,(folder/entry['original']).read_bytes())
                else:
                    path.unlink()
        raise
    else:
        journal.unlink(missing_ok=True)
    return backup_id


def recover_transaction(root=CONFIG_ROOT) -> list[str]:
    journal = root / '.local/webui/config-transaction.json'
    if not journal.is_file():
        return []
    try:
        folder = root / '.local/webui/config-backups' / read_json(journal)['backup_id']
        manifest = read_json(folder/'manifest.json')
        allowed = set(paths(root))
        for entry in manifest:
            path = (root / entry['path']).resolve()
            if path not in allowed:
                raise ValueError('Invalid transaction target')
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if current not in {entry['before'],entry['after']}:
                return ['上次保存被中断，且文件随后发生外部修改。请从备份列表明确选择恢复。']
        for entry in manifest:
            path = root/entry['path']
            if entry['original']:
                atomic_bytes(path,(folder/entry['original']).read_bytes())
            else:
                path.unlink(missing_ok=True)
        journal.unlink()
        return ['上次保存被中断，已恢复保存前的完整配置。']
    except (OSError, ValueError, KeyError, TypeError):
        return ['配置事务无法自动恢复，请选择可用备份或重建配置。']


def replace_yaml(text: str, updates: dict) -> str:
    nodes = []
    def visit(node, trail=()):
        if isinstance(node, yaml.MappingNode):
            for key, child in node.value:
                visit(child, (*trail, key.value))
        elif trail in updates:
            if not isinstance(node, yaml.ScalarNode):
                raise ValueError('配置结构已变化，未修改文件。')
            nodes.append((node.start_mark.index, node.end_mark.index, json.dumps(updates[trail], ensure_ascii=False)))
    visit(yaml.compose(text))
    if len(nodes) != len(updates):
        raise ValueError('配置缺少必要字段，请检查 windows.yaml；未修改文件。')
    for start, end, replacement in sorted(nodes, reverse=True):
        text = text[:start] + replacement + text[end:]
    yaml.safe_load(text)
    return text


def save_settings(settings: Settings, version: str, root=CONFIG_ROOT, *, repair=False) -> dict:
    assert_no_credentials(settings.model_dump())
    env_path, yaml_path, _ = paths(root)
    if version != file_version(*paths(root)):
        raise ValueError('配置已被其他窗口修改，请重新加载后保存。')
    old_env = env_path.read_bytes() if env_path.exists() else b''
    old_yaml = yaml_path.read_bytes() if yaml_path.exists() else b''
    source_env = old_env.decode('utf-8-sig',errors='replace' if repair else 'strict')
    source_yaml = old_yaml.decode('utf-8-sig',errors='replace' if repair else 'strict')
    if repair:
        # Explicit recovery uses the shipped adapter template and retains the damaged files in the transaction backup.
        source_yaml = (Path(__file__).parent/'defaults.yaml').read_text(encoding='utf-8')
    text = env_edit(source_env,{'MSWEA_MODEL_NAME':settings.model_name,'OPENAI_API_BASE':settings.api_base})
    yaml_text = replace_yaml(source_yaml, {
        ('agent', 'step_limit'): settings.step_limit,
        ('agent', 'cost_limit'): settings.cost_limit,
        ('environment', 'timeout'): settings.timeout,
        ('model', 'model_class'): settings.model_class,
        ('model', 'model_kwargs', 'timeout'): settings.timeout,
    })
    if version != file_version(*paths(root)):
        raise ValueError('配置在保存前发生变化，请重新加载。')
    commit_files({env_path:(('\ufeff' if old_env.startswith(b'\xef\xbb\xbf') else '')+text).encode('utf-8'),
                  yaml_path:(('\ufeff' if old_yaml.startswith(b'\xef\xbb\xbf') else '')+yaml_text).encode('utf-8')},root)
    return read_settings(root)


def replace_key(key: str, root=CONFIG_ROOT) -> None:
    from credentials import _powershell
    if not key.strip() or len(key) > 4096 or '\n' in key or '\r' in key:
        raise ValueError('密钥为空或格式无效。')
    encrypted = _powershell('$ErrorActionPreference="Stop"; $s=ConvertTo-SecureString ([Console]::In.ReadToEnd()) -AsPlainText -Force; [Console]::Write((ConvertFrom-SecureString $s))', key.strip())
    decoded = _powershell('$ErrorActionPreference="Stop"; $s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); [Console]::Write([Net.NetworkCredential]::new("",$s).Password)', encrypted)
    if decoded != key.strip():
        raise ValueError('Windows 密钥加密验证失败，原密钥未修改。')
    env_path,_,key_path = paths(root)
    changes = {key_path:encrypted.encode('ascii')}
    if env_path.is_file():
        before = env_path.read_bytes()
        cleaned = env_edit(before.decode('utf-8-sig'),{'OPENAI_API_KEY':None})
        changes[env_path] = (('\ufeff' if before.startswith(b'\xef\xbb\xbf') else '')+cleaned).encode('utf-8')
    commit_files(changes,root)
    _credential_status.cache_clear()


def backup_list(root=CONFIG_ROOT) -> list[dict]:
    directory = root/'.local/webui/config-backups'
    if not directory.is_dir():
        return []
    result = []
    for folder in sorted(directory.iterdir(),reverse=True):
        if not folder.is_dir() or not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}',folder.name):
            continue
        manifest = folder/'manifest.json'
        try:
            entries = read_json(manifest) if manifest.is_file() else []
            names = [x['path'] for x in entries if x.get('original') and x['path']!='.local/api-key.dpapi']
            if not entries:
                names = [n for n in ['env.backup','windows.yaml.backup'] if (folder/n).is_file()]
            if names:
                result.append({'id':folder.name,'files':names,'bytes':sum(p.stat().st_size for p in folder.iterdir() if p.is_file())})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def restore_backup(backup_id: str, version: str, root=CONFIG_ROOT) -> dict:
    if version!=file_version(*paths(root)):
        raise ValueError('当前配置已变化，请重新加载后再恢复。')
    if backup_id not in {x['id'] for x in backup_list(root)}:
        raise ValueError('备份不存在或无效。')
    folder = root/'.local/webui/config-backups'/backup_id
    changes = {}
    if (folder/'manifest.json').is_file():
        for entry in read_json(folder/'manifest.json'):
            target = (root/entry['path']).resolve()
            if target not in set(paths(root)[:2]) or not entry.get('original'):
                continue
            original = (folder/entry['original']).resolve()
            if original.parent!=folder.resolve():
                raise ValueError('备份内容路径无效。')
            changes[target] = original.read_bytes()
    else:
        for name,target in zip(['env.backup','windows.yaml.backup'],paths(root)[:2]):
            if (folder/name).is_file():
                changes[target] = (folder/name).read_bytes()
    if not changes:
        raise ValueError('该备份没有可恢复的配置。')
    if paths(root)[1] in changes:
        if not isinstance(yaml.safe_load(changes[paths(root)[1]].decode('utf-8-sig')),dict):
            raise ValueError('备份中的 YAML 不可用。')
    commit_files(changes,root)
    return inspect_settings(root)
