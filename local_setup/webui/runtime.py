"""Bounded command capture with live output; never owns unrelated processes."""
import os
import re
import subprocess
import threading
from collections import deque


def stream_command(args: list[str], *, emit, cwd=None, env=None, timeout=120, capture_limit=2_000_000):
    process = subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               text=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
    emit('command_process',{'pid':process.pid})
    chunks=deque()
    size=0
    truncated=False
    def read():
        nonlocal size,truncated
        while chunk:=process.stdout.readline(65536):
            emit('log',chunk.rstrip('\r\n'))
            chunks.append(chunk)
            size+=len(chunk)
            while size>capture_limit and len(chunks)>1:
                size-=len(chunks.popleft())
                truncated=True
    reader=threading.Thread(target=read,daemon=True)
    reader.start()
    timed_out=False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out=True
        subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        process.wait(timeout=10)
    reader.join(timeout=5)
    output=''.join(chunks)
    if truncated:
        output='[观察输出仅保留末尾约 2 MB；完整实时日志保存在 events.jsonl。]\n'+output
    return {'output':output,'returncode':-1 if timed_out else process.returncode,'exception_info':'Command timed out.' if timed_out else '',
            'timed_out':timed_out}


def command_environment(extra=None):
    env=os.environ | (extra or {})
    for name in list(env):
        if re.search(r'KEY|TOKEN|PASSWORD|SECRET|AUTHORIZATION',name,re.I) or name.startswith(('MINI_WEB_','CODEX_')):
            env.pop(name,None)
    return env
