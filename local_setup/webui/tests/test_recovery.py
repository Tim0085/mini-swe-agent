import json
from pathlib import Path

import pytest

from local_setup.webui.common import PRIVATE_VALUES, export_allowed, read_json, save_json, redact
from local_setup.webui.settings import Settings, env_edit, inspect_settings, paths, save_settings, backup_list, restore_backup, recover_transaction, replace_key, load_secret,runtime_call_limit


def make_config(root):
    (root/'.local').mkdir()
    (root/'local_setup').mkdir()
    (root/'.local/.env').write_bytes(b"MSWEA_MODEL_NAME='openai/test'\r\nOPENAI_API_BASE='https://example.com/v1'\r\nUNKNOWN='keep'\r\n")
    source=Path(__file__).parents[1]/'defaults.yaml'
    (root/'local_setup/windows.yaml').write_bytes(source.read_bytes())


def test_exact_business_payload_and_safe_export(tmp_path):
    payload={'model_patch':'+demo = "sk-not-real-example-token"\n','token_count':23,'task':'Use bearer sample-authorization-text'}
    save_json(tmp_path/'record.json',payload)
    assert read_json(tmp_path/'record.json')==payload
    assert not export_allowed(payload['model_patch'])
    sentinel='private-test-secret-not-for-output'
    PRIVATE_VALUES.append(sentinel)
    try:
        with pytest.raises(ValueError,match='密钥'):
            save_json(tmp_path/'blocked.json',{'task':sentinel})
        assert not (tmp_path/'blocked.json').exists()
    finally:
        PRIVATE_VALUES.remove(sentinel)


def test_env_url_roundtrip_and_newlines():
    with pytest.raises(ValueError):
        Settings(model_name='test',api_base="https://example.com/v1'bad")
    value='https://example.com/v1%27valid'
    before="OPENAI_API_BASE='old'\r\nOTHER='keep'\r\n"
    assert env_edit(before,{'OPENAI_API_BASE':value})==f"OPENAI_API_BASE='{value}'\r\nOTHER='keep'\r\n"
    assert env_edit(before,{'OPENAI_API_BASE':None})=="OTHER='keep'\r\n"


def test_unicode_dpapi_roundtrip_and_single_source(tmp_path):
    make_config(tmp_path)
    env=tmp_path/'.local/.env'
    env.write_text(env.read_text()+"OPENAI_API_KEY='obsolete-test-fixture'\n",encoding='utf-8')
    key='仅供离线测试的密钥😀'
    try:
        replace_key(key,tmp_path)
        assert load_secret(tmp_path)==key
        assert 'OPENAI_API_KEY' not in env.read_text()
        assert key.encode('utf-8') not in (tmp_path/'.local/api-key.dpapi').read_bytes()
    finally:
        if key in PRIVATE_VALUES:
            PRIVATE_VALUES.remove(key)


def test_display_redaction_preserves_usage_numbers():
    result=redact({'usage':{'prompt_tokens':23,'token_count':24},'headers':{'Authorization':'Bearer fake-value'},'client_secret':'private'})
    assert result['usage']=={'prompt_tokens':23,'token_count':24}
    assert result['headers']['Authorization']=='[REDACTED]' and result['client_secret']=='[REDACTED]'


def test_global_call_limit_tracks_steps_and_respects_explicit_cap(tmp_path):
    make_config(tmp_path)
    import os
    previous=os.environ.pop('MSWEA_GLOBAL_CALL_LIMIT',None)
    try:
        assert runtime_call_limit(250,tmp_path)=='251'
        path=paths(tmp_path)[0]
        path.write_text(path.read_text()+"MSWEA_GLOBAL_CALL_LIMIT='70'\n",encoding='utf-8')
        assert runtime_call_limit(250,tmp_path)=='70'
        assert '70' in inspect_settings(tmp_path)['runtime_limit_note']
    finally:
        if previous is not None:
            os.environ['MSWEA_GLOBAL_CALL_LIMIT']=previous


def test_invalid_config_can_be_repaired_and_restored(tmp_path):
    make_config(tmp_path)
    before=inspect_settings(tmp_path)
    selected=Settings(**{k:before[k] for k in Settings.model_fields})
    selected.timeout=65
    saved=save_settings(selected,before['version'],tmp_path)
    backups=backup_list(tmp_path)
    assert backups and saved['timeout']==65
    restored=restore_backup(backups[0]['id'],saved['version'],tmp_path)
    assert restored['timeout']==120
    yaml_path=paths(tmp_path)[1]
    yaml_path.write_text('broken: [',encoding='utf-8')
    broken=inspect_settings(tmp_path)
    assert not broken['valid'] and broken['issues']
    fixed=save_settings(selected,broken['version'],tmp_path,repair=True)
    assert fixed['valid'] and fixed['timeout']==65
    assert any((p/'1.original').is_file() and (p/'1.original').read_bytes()==b'broken: [' for p in (tmp_path/'.local/webui/config-backups').iterdir())


def test_interrupted_transaction_restores_complete_pair(tmp_path):
    make_config(tmp_path)
    before=inspect_settings(tmp_path)
    old=[p.read_bytes() for p in paths(tmp_path)[:2]]
    selected=Settings(**{k:before[k] for k in Settings.model_fields})
    selected.timeout=77
    save_settings(selected,before['version'],tmp_path)
    backup=backup_list(tmp_path)[0]['id']
    # Recreate the durable on-disk state of a process dying after only one replacement.
    paths(tmp_path)[1].write_bytes(old[1])
    save_json(tmp_path/'.local/webui/config-transaction.json',{'backup_id':backup})
    assert recover_transaction(tmp_path)
    assert [p.read_bytes() for p in paths(tmp_path)[:2]]==old
    assert not (tmp_path/'.local/webui/config-transaction.json').exists()
