import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from local_setup.webui.approval import ApprovalInput


def test_one_reader_survives_timeout_without_stealing_later_approval():
    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, 'r', encoding='utf-8') as source, os.fdopen(writer_fd, 'w', encoding='utf-8') as sink:
        inbox = ApprovalInput(source)
        with pytest.raises(TimeoutError):
            inbox.wait('expired', time.monotonic() + .03)
        thread = inbox.thread
        for ticket, decision in [('expired', True), ('wrong-ticket', True), ('current', False), ('next', True)]:
            sink.write(json.dumps({'ticket': ticket, 'approve': decision}) + '\n')
        sink.flush()
        assert inbox.wait('current', time.monotonic() + 2)['approve'] is False
        assert inbox.wait('next')['approve'] is True
        assert inbox.thread is thread
        sink.close()
        with pytest.raises(RuntimeError, match='连接已关闭'):
            inbox.wait('after-eof')
        with pytest.raises(RuntimeError, match='连接已关闭'):
            inbox.wait('after-eof-again')
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_worker_emits_expiry_then_rejects_late_ticket_and_keeps_non_pro_wait():
    code = '''
import json, time
from local_setup.webui.worker import approval, APPROVAL_INPUT
from minisweagent.exceptions import TimeExceeded
try:
    approval([{'command':'must-not-run'}], '/', deadline=time.monotonic()+.15)
except TimeExceeded as exc:
    print('EXPIRED:'+exc.messages[0]['extra']['exit_status'], flush=True)
answer=approval([{'command':'ordinary-manual-confirmation'}], '/')
print('ANSWER:'+json.dumps(answer), flush=True)
try:
    approval([{'command':'eof-check'}], '/')
except RuntimeError:
    print('EOF-CLOSED', flush=True)
APPROVAL_INPUT.thread.join(timeout=2)
print('READER-STOPPED:'+str(not APPROVAL_INPUT.thread.is_alive()), flush=True)
'''
    process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code],
        cwd=Path(__file__).resolve().parents[3], env=os.environ.copy(), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    events, lines, tickets = [], [], []
    watchdog = threading.Timer(10, process.kill)
    watchdog.start()
    try:
        for line in process.stdout:
            lines.append(line.strip())
            if not line.startswith('@@MINI_WEB@@'):
                continue
            event = json.loads(line.removeprefix('@@MINI_WEB@@'))
            events.append(event)
            if event['kind'] != 'approval':
                continue
            tickets.append(event['data']['ticket'])
            if len(tickets) == 1:
                assert isinstance(event['data']['expires_at'], float)
            elif len(tickets) == 2:
                assert 'expires_at' not in event['data']
                for ticket, approve in [(tickets[0], True), (tickets[1], False)]:
                    process.stdin.write(json.dumps({'ticket': ticket, 'approve': approve}) + '\n')
                process.stdin.flush()
            else:
                process.stdin.close()
        process.wait(timeout=10)
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0, lines
    assert {'kind': 'approval_expired', 'data': {'ticket': tickets[0], 'reason': 'time_limit'}} in events
    assert 'EXPIRED:TimeExceeded' in lines and 'EOF-CLOSED' in lines and 'READER-STOPPED:True' in lines
    assert json.loads(next(line.removeprefix('ANSWER:') for line in lines if line.startswith('ANSWER:'))) == {
        'ticket': tickets[1], 'approve': False}


@pytest.mark.parametrize(('expiry_source',), [('api',), ('event',)])
def test_manager_rejects_expired_approval_without_writing_to_live_worker(tmp_path, expiry_source):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from local_setup.webui.server import app, manager, NONCE
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.post('/session', json={'nonce': NONCE}).status_code == 200
        response = client.post('/api/runs', json={'kind': 'offline', 'task': 'Approval deadline check',
            'cwd': str(tmp_path), 'request_id': uuid4().hex,
            'settings_version': client.get('/api/config').json()['version']})
        assert response.status_code == 200, response.text
        run_id = response.json()['id']
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                meta = manager.get(run_id)
                if meta.get('pending'):
                    break
                time.sleep(.05)
            assert meta.get('pending'), meta
            ticket = meta['pending']['ticket']
            wrong_ticket = uuid4().hex
            assert client.post('/api/runs/' + run_id + '/approve', json={
                'ticket': wrong_ticket, 'approve': True}).status_code == 409
            manager.event(run_id, 'approval_expired', {'ticket': wrong_ticket, 'reason': 'time_limit'})
            assert manager.get(run_id)['pending']['ticket'] == ticket
            if expiry_source == 'api':
                manager.event(run_id, 'approval', meta['pending'] | {'expires_at': time.time() - 1})
            else:
                manager.event(run_id, 'approval_expired', {'ticket': ticket, 'reason': 'time_limit'})
            expired = client.post('/api/runs/' + run_id + '/approve', json={'ticket': ticket, 'approve': True})
            assert expired.status_code == 409, expired.text
            assert not manager.get(run_id).get('pending')
            assert manager.get(run_id)['stats']['calls'] == 1
            assert client.post('/api/runs/' + run_id + '/approve', json={
                'ticket': ticket, 'approve': False}).status_code == 409
        finally:
            manager.cancel(run_id)
            assert manager.idle.wait(20)
        assert manager.get(run_id)['status'] == 'cancelled'
