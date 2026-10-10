"""Double-click entry point; never attaches to an existing user terminal."""
import ctypes
import json
import hashlib
import msvcrt
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from local_setup.webui.common import STATE, new_id, read_json, recover_json, save_json, source_version


def health(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        return {}


def serve():
    import uvicorn
    os.environ['MSWEA_GLOBAL_CONFIG_DIR']=str(Path(os.getenv('MINI_WEB_CONFIG_ROOT',str(ROOT)))/'.local')
    os.environ['MSWEA_CONFIGURED']='true'
    from local_setup.webui.server import app, PORT, IDENTITY, NONCE, RUNNING_VERSION
    lock = (STATE / 'service.lock').open('a+b')
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    sock = socket.socket()
    restart=False
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=PORT,access_log=False,log_level='warning',log_config=None,timeout_graceful_shutdown=15))
    def shutdown(should_restart):
        nonlocal restart
        restart=should_restart
        server.should_exit=True
    app.state.request_shutdown=shutdown
    try:
        sock.bind(('127.0.0.1', PORT))
        save_json(STATE/'service.json',{'port':PORT,'identity':IDENTITY,'nonce':NONCE,'pid':os.getpid(),'code_version':RUNNING_VERSION})
        server.run(sockets=[sock])
    finally:
        sock.close()
        lock.close()
    if restart:
        env=os.environ | {'MINI_WEB_RESTART_NONCE':NONCE}
        subprocess.Popen([str(ROOT/'.venv/Scripts/pythonw.exe'),str(Path(__file__).resolve()),'--no-browser'],cwd=ROOT,env=env,
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)


def service_locked():
    handle=(STATE/'service.lock').open('a+b')
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def upgrade_legacy(data):
    message='检测到旧版工作台后台。新版包含退出和恢复功能，需要重启此后台才能生效。\n\n这会停止本工作台的活动任务；不会结束其他 Python 或 Docker Desktop。是否现在升级？'
    if ctypes.windll.user32.MessageBoxW(None,message,'Mini 工作台升级确认',0x24)!=6:
        return False
    current=health(data['port'])
    if current.get('identity')!=data['identity']:
        raise RuntimeError('旧实例身份发生变化，未结束任何进程，请重新打开入口。')
    pid=int(data['pid'])
    script=f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; [Console]::Write($p.CommandLine)"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=15,
                          env=os.environ | {'PSModulePath':str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/Modules')})
    if str(Path(__file__).resolve()).lower() not in result.stdout.lower() or '--serve' not in result.stdout:
        raise RuntimeError('无法确认旧后台进程归属，未结束任何进程。')
    port=int(data['port'])
    owner=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                          f"Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort {port} | Select-Object -ExpandProperty OwningProcess"],
                         capture_output=True,text=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW,
                         env=os.environ | {'PSModulePath':str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/Modules')})
    if str(pid) not in owner.stdout.split():
        raise RuntimeError('端口与进程归属不一致，未结束任何进程。')
    os.kill(pid,15)
    for _ in range(100):
        if not service_locked():
            return True
        time.sleep(.1)
    raise RuntimeError('旧后台尚未完全释放，未启动重复实例。')


def launch(open_browser=True):
    STATE.mkdir(parents=True, exist_ok=True)
    lock = (STATE / 'launch.lock').open('a+b')
    for _ in range(100):
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError('工作台启动仍在进行，请稍后再次打开。')
    try:
        data=recover_json(STATE/'service.json',{})
        if not isinstance(data,dict) or not all(key in data for key in ['port','identity','nonce','pid']):
            data={}
        running=health(data['port']) if data else {}
        if data and running.get('identity')==data.get('identity') and 'code_version' not in running:
            if open_browser and upgrade_legacy(data):
                data,running={},{}
            elif not open_browser:
                return data['port']
        if not data or running.get('identity') != data.get('identity'):
            if service_locked():
                state_id=hashlib.sha256(str(STATE).encode()).hexdigest()[:16]
                candidates=set(range(8765,8776)) | {int(os.getenv('MINI_WEB_PORT','8765'))}
                for candidate in candidates:
                    discovered=health(candidate)
                    if discovered.get('state_id')==state_id:
                        if open_browser:
                            webbrowser.open(f'http://127.0.0.1:{candidate}/')
                        return candidate
                raise RuntimeError('后台锁仍由运行实例持有，但健康检查未响应。未启动重复服务；请在原工作台退出，或检查服务日志。')
            if (STATE/'service.json').is_file() and not data:
                backup=STATE/'recovery'/('service-'+new_id()+'.json')
                backup.parent.mkdir(parents=True,exist_ok=True)
                backup.write_bytes((STATE/'service.json').read_bytes())
            port = None
            preferred=int(os.getenv('MINI_WEB_PORT','8765'))
            for candidate in dict.fromkeys([preferred,*range(8765,8776)]):
                with socket.socket() as sock:
                    try:
                        sock.bind(('127.0.0.1', candidate))
                        port = candidate
                        break
                    except OSError:
                        continue
            if port is None:
                raise RuntimeError('8765–8775 端口均被占用；未关闭任何其他程序。')
            nonce = os.environ.get('MINI_WEB_RESTART_NONCE') or secrets.token_urlsafe(32)
            env = os.environ | {'MINI_WEB_NONCE': nonce, 'MINI_WEB_PORT': str(port), 'PYTHONUTF8': '1'}
            log = (STATE / 'service.log').open('ab')
            executable = ROOT / '.venv/Scripts/pythonw.exe'
            process = subprocess.Popen([str(executable), str(Path(__file__).resolve()), '--serve'], cwd=ROOT,
                                       env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            log.close()
            for _ in range(150):
                if health(port).get('app') == 'mini-workbench':
                    data = json.loads((STATE / 'service.json').read_text())
                    break
                if process.poll() is not None:
                    raise RuntimeError('启动失败，请查看 .local/webui/service.log。')
                time.sleep(.2)
            else:
                raise RuntimeError('启动超时，请检查 .local/webui/service.log。')
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{data['port']}/#" + data['nonce'])
        return data['port']
    finally:
        lock.close()


if __name__ == '__main__':
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        if '--serve' in sys.argv:
            serve()
        else:
            port = launch('--no-browser' not in sys.argv)
            if sys.stdout:
                print(f'Mini 工作台已就绪：http://127.0.0.1:{port}')
    except Exception as exc:
        if sys.stdout:
            raise
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Mini 工作台启动失败', 0x10)
