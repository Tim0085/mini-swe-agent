import json
import os
import subprocess
import sys
import uuid
import threading
import time
from pathlib import Path

from .approval import ApprovalInput
from .common import CONFIG_ROOT, PRIVATE_VALUES, ROOT, assert_no_credentials, job_dir, read_json, redact, save_json

OUT = sys.stdout
EMIT_LOCK=threading.Lock()
APPROVAL_INPUT = ApprovalInput(sys.stdin)


def emit(kind: str, data) -> None:
    with EMIT_LOCK:
        OUT.write('@@MINI_WEB@@' + json.dumps({'kind': kind, 'data': redact(data)}, ensure_ascii=False) + '\n')
        OUT.flush()


class SafeLog:
    def write(self, value):
        if value.strip():
            emit('log', value.rstrip())
        return len(value)

    def flush(self):
        OUT.flush()

    def isatty(self):
        return False


def approval(actions: list[dict], cwd: str, *, deadline: float | None = None) -> dict:
    ticket = uuid.uuid4().hex
    pending = {'ticket': ticket, 'actions': actions, 'cwd': cwd}
    if deadline is not None:
        pending['expires_at'] = time.time() + max(0, deadline - time.monotonic())
    emit('approval', pending)
    try:
        answer = APPROVAL_INPUT.wait(ticket, deadline)
    except TimeoutError:
        from minisweagent.exceptions import TimeExceeded
        emit('approval_expired', {'ticket': ticket, 'reason': 'time_limit'})
        raise TimeExceeded({'role':'exit','content':'TimeExceeded',
                            'extra':{'exit_status':'TimeExceeded','submission':''}}) from None
    emit('state', 'running')
    return answer


def initialize_repository(env, commit: str, report) -> None:
    import re
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('题目的 base_commit 无效。')
    report('phase', '初始化题目代码')
    result = env.execute({'command': 'git reset --hard ' + commit})
    details = '\n'.join(str(result.get(key) or '') for key in ('output', 'exception_info')).strip()
    report('log', f"题目代码初始化：退出码 {result['returncode']}\n{details[-8000:]}")
    if result['returncode']:
        raise ValueError('题目代码初始化失败（退出码 ' + str(result['returncode']) + '）。具体原因已保存在“命令与日志”；本次尚未请求模型，请检查后再明确重试。')


def solve(request: dict, folder: Path) -> None:
    from .benchmarks import is_pro
    pro = request['kind'] == 'swebench' and is_pro(request['dataset'])
    pro_runtime = {}
    deadline = None
    queued=request.get('queue_context')
    profile=request.get('profile_snapshot') if request['kind']!='offline' else None
    if request['kind'] != 'offline':
        from .settings import load_secret,runtime_call_limit,inspect_settings
        if profile:
            from .profiles import load_snapshot_secret
            key=load_snapshot_secret(profile)
        else:
            if queued and inspect_settings()['version']!=request['settings_version']:raise ValueError('配置在派发后变化，未调用模型。')
            key=load_secret()
            if queued and inspect_settings()['version']!=request['settings_version']:raise ValueError('密钥或配置变化，未调用模型。')
        os.environ['MSWEA_GLOBAL_CONFIG_DIR']=str(CONFIG_ROOT/'.local')
        os.environ['MSWEA_CONFIGURED']='true'
        os.environ['OPENAI_API_KEY']=key
        os.environ['PATH']=str(ROOT/'.venv/Scripts')+os.pathsep+os.environ['PATH']
        os.environ['MSWEA_GLOBAL_CALL_LIMIT']=str(profile['runtime']['call_limit']) if profile else runtime_call_limit(request['settings']['step_limit'])
        if profile:os.environ['MSWEA_GLOBAL_COST_LIMIT']=str(profile['runtime'].get('global_cost_limit', '0'))
        os.environ.setdefault('MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT','2')
        if queued or profile:os.environ['MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT']='1'
        settings = request['settings']
        os.environ['OPENAI_API_BASE'] = settings['api_base']
        os.environ['OPENAI_BASE_URL'] = settings['api_base']
        os.environ['MSWEA_MODEL_NAME'] = settings['model_name']
        PRIVATE_VALUES.append(os.getenv('OPENAI_API_KEY', ''))
        if not os.getenv('OPENAI_API_KEY'):
            raise ValueError('尚未配置 API 密钥，请先打开设置。')
    else:
        os.environ['MSWEA_GLOBAL_CONFIG_DIR'] = str(folder)
        os.environ['MSWEA_CONFIGURED'] = 'true'
        os.environ.pop('OPENAI_API_KEY', None)
    os.environ['HF_HOME'] = str(ROOT / '.local/hf-cache')
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.config import get_config_from_spec
    from minisweagent.models import get_model
    from minisweagent.models.test_models import DeterministicModel, make_output
    from minisweagent.utils.serialize import recursive_merge
    from windows_env import WindowsLocalEnvironment
    from .runtime import stream_command, command_environment

    class StreamingWindows(WindowsLocalEnvironment):
        def execute(self,action,cwd='',*,timeout=None):
            assert_no_credentials(action)
            output=stream_command([str(self.bash),'--noprofile','--norc','-c',action.get('command','')],
                                  emit=emit,cwd=cwd or self.config.cwd,env=command_environment(self.config.env),timeout=timeout or self.config.timeout)
            self._check_finished(output)
            return output

    class WebAgent(DefaultAgent):
        def stats(self):
            return {'calls':self.n_calls,'cost':self.cost,**getattr(self.model,'totals',{})}
        def add_messages(self, *messages):
            result = super().add_messages(*messages)
            for message in messages:
                if message.get('role') != 'system':
                    emit('message', message)
            return result

        def query(self):
            emit('state', 'running')
            emit('phase', '等待模型回复')
            if deadline is not None:
                if time.monotonic() >= deadline:
                    from minisweagent.exceptions import TimeExceeded
                    raise TimeExceeded({'role':'exit','content':'TimeExceeded','extra':{'exit_status':'TimeExceeded','submission':''}})
                self.model.config.model_kwargs['timeout'] = min(request['settings']['timeout'], max(.1, deadline-time.monotonic()))
            if queued and (self.cost>=self.config.cost_limit or self.n_calls>=self.config.step_limit):return super().query()
            if queued:emit('billing',{'pending':True,'unknown':False,'cost_floor':self.cost})
            try:
                result=super().query()
            except Exception as exc:
                if queued:
                    from minisweagent.exceptions import FormatError
                    response_cost=getattr(self.model,'last_cost',None)
                    format_known=isinstance(exc,FormatError) and bool(exc.messages) and (exc.messages[0].get('extra',{}).get('cost_known',False) or exc.messages[0].get('extra',{}).get('cost',0)>0)
                    known=response_cost is not None or format_known
                    amount=response_cost if response_cost is not None else exc.messages[0]['extra']['cost'] if format_known else 0
                    emit('billing',{'pending':False,'unknown':not known,'cost_floor':self.cost+amount})
                raise
            else:
                if queued:emit('billing',{'pending':False,'unknown':False,'cost_floor':self.cost})
                return result
            finally:
                emit('stats', self.stats())

        def execute_actions(self, message):
            actions = message.get('extra', {}).get('actions', [])
            if actions and not request.get('auto_approve'):
                self.save(self.config.output_path)
                decision = approval(actions, self.env.config.cwd, deadline=deadline)
                if not decision.get('approve'):
                    outputs = [{'output': 'User rejected this command. ' + decision.get('feedback', ''), 'returncode': -1, 'exception_info': ''} for _ in actions]
                    return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))
            emit('phase', '执行命令')
            return super().execute_actions(message)

        def save(self, path, *extra):
            emit('stats', self.stats())
            data = self.serialize(*extra)
            if path:
                save_json(Path(path), data)
            return data

    if request['kind'] == 'swebench':
        from .bench import load_instance, ensure_image
        from minisweagent.environments.docker import DockerEnvironment
        row = load_instance(request['dataset'], request['instance_id'], request.get('allow_data_download',request.get('allow_download',False)),emit)
        image = ensure_image(row, request.get('allow_image_download',request.get('allow_download',False)), emit,
                             request['dataset'],request.get('queue_context',{}).get('image_id'))
        config = profile['configs']['swebench'] if profile else get_config_from_spec(str(ROOT / 'src/minisweagent/config/benchmarks/swebench.yaml'))
        container_name = 'mini-web-' + folder.name
        emit('container', container_name)

        class WebDocker(DockerEnvironment):
            def _start_container(self):
                if pro:
                    from .pro_runtime import start_solver
                    self.container_id = start_solver(self.config.image, container_name)
                    return
                from .bench import docker
                result = docker('run', '-d', '--rm', '--name', container_name,
                                '-w', '/testbed', self.config.image, 'sleep', '2h', timeout=120)
                result.check_returncode()
                self.container_id = result.stdout.strip()

            def execute(self, action, cwd='', *, timeout=None):
                from .docker_runtime import check_docker_result
                assert_no_credentials(action)
                args = [self.config.executable, 'exec', '-w', cwd or self.config.cwd]
                for name in self.config.forward_env:
                    if name in os.environ:args.extend(['-e', f'{name}={os.environ[name]}'])
                for name, value in self.config.env.items():args.extend(['-e', f'{name}={value}'])
                seconds = timeout or self.config.timeout
                if deadline is not None:
                    if time.monotonic() >= deadline:
                        from minisweagent.exceptions import TimeExceeded
                        raise TimeExceeded({'role':'exit','content':'TimeExceeded','extra':{'exit_status':'TimeExceeded','submission':''}})
                    seconds = min(seconds, max(.1, deadline-time.monotonic()))
                command = action.get('command', '')
                if pro:
                    from .pro_runtime import wrap_action
                    command = wrap_action(command, seconds)
                args.extend([self.container_id, *self.config.interpreter, command])
                output = check_docker_result(stream_command(args, emit=emit, timeout=seconds + 10 if pro else seconds))
                if pro and output['returncode'] in {124,137}:
                    if time.monotonic() >= deadline:
                        from minisweagent.exceptions import TimeExceeded
                        raise TimeExceeded({'role':'exit','content':'TimeExceeded','extra':{'exit_status':'TimeExceeded','submission':''}})
                    raise TimeoutError('Pro 单条命令达到时限，已终止容器内命令并暂停求解；保留本次改动，不自动调用模型。')
                if pro and output['returncode'] == 125:
                    raise RuntimeError('Pro 命令组未能正常结束，已暂停求解，请检查清理结果。')
                self._check_finished(output)
                return output

            def cleanup(self):
                if self.container_id:
                    from .bench import docker
                    docker('rm', '-f', self.container_id, timeout=60).check_returncode()
                    self.container_id = None

        env_config = config['environment'].copy()
        env_config.pop('environment_class', None)
        if pro:
            from .bench import docker
            checked = docker('image', 'inspect', image, '--format', '{{.Id}}')
            checked.check_returncode()
            image = checked.stdout.strip()
            env_config.update(cwd='/', forward_env=[], env={}, interpreter=['/bin/bash','-lc'])
        env = WebDocker(**(env_config | {'image': image, 'timeout': request['settings']['timeout']}))
        try:
            if pro:
                from .pro_runtime import prepare_solver
                pro_runtime = prepare_solver(env.container_id, row)
                env.config.cwd = pro_runtime['cwd']
                emit('benchmark_runtime', pro_runtime)
            else:
                initialize_repository(env, row.get('base_commit', ''), emit)
            from .bench import problem_description
            task = problem_description(row, request['dataset'])
        except Exception:
            env.cleanup()
            raise
    else:
        adapter = CONFIG_ROOT/'local_setup/windows.yaml'
        if request['kind']=='offline':
            adapter=Path(__file__).parent/'defaults.yaml'
        config = profile['configs']['local'] if profile else recursive_merge(get_config_from_spec(str(ROOT / 'src/minisweagent/config/mini.yaml')),
                                                                          get_config_from_spec(str(adapter)))
        env = StreamingWindows(cwd=request['cwd'], timeout=request['settings']['timeout'])
        task = request['task']
        if request['kind']=='local' or request.get('offline_scenario')=='edit':
            from .changes import snapshot
            emit('phase','记录任务开始前的文件指纹，不修改 Git 索引')
            save_json(folder/'before-files.json',snapshot(Path(request['cwd'])))

    try:
        if request['kind'] == 'offline':
            scenario=request.get('offline_scenario','basic')
            if scenario!='basic' and os.getenv('MINI_WEB_TESTING')!='1':
                raise ValueError('离线诊断未在隔离测试实例启用。')
            command={'basic':"pwd\nprintf '离线连接正常\\n'",'slow_shell':"printf 'STREAM_STARTED\\n'; sleep 20; printf 'STREAM_FINISHED\\n'",'slow_model':"pwd",'edit':"printf 'offline fixture output\\n' > outcome.txt"}[scenario]
            fixture_cost=.02 if queued else 0
            outputs=[make_output('离线演示：执行受控测试步骤。',[{'command':command}],cost=fixture_cost),make_output('检查结束，提交结果。',[{'command':"printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n离线检查完成；没有调用付费模型。\\n'"}],cost=fixture_cost)]
            if scenario=='slow_model':
                outputs.insert(0,make_output('等待中的离线测试模型。',[{'command':'/sleep 20'}],cost=0))
            model = DeterministicModel(outputs=outputs,cost_per_call=0)
        elif profile:
            from .profile_model import make_profile_model
            model = make_profile_model(profile,key,config.get('model',{}))
        else:
            model_config = config.get('model', {}) | {'model_name': request['settings']['model_name'], 'model_class': request['settings']['model_class']}
            if queued:model_config['cost_tracking']='default'
            model_config['model_kwargs'] = model_config.get('model_kwargs', {}) | {'timeout': request['settings']['timeout'], 'num_retries': 0, 'drop_params': True}
            model = get_model(config=model_config)
        agent_config = config['agent'].copy()
        for key in ['mode', 'whitelist_actions', 'agent_class', 'confirm_exit']:
            agent_config.pop(key, None)
        agent_config.update(step_limit=request['settings']['step_limit'], cost_limit=request['settings']['cost_limit'], output_path=folder / 'trajectory.traj.json')
        if pro:
            execution = request['benchmark_snapshot']['execution_config']
            agent_config.update(instance_template=execution['instance_template'], wall_time_limit_seconds=execution['wall_time_limit_seconds'])
            deadline = time.monotonic() + execution['wall_time_limit_seconds']
        agent = WebAgent(model, env, **agent_config)
    except Exception:
        if request['kind'] == 'swebench':
            env.cleanup()
        raise
    try:
        try:
            info = agent.run(task)
        except Exception as exc:
            if pro:
                info = dict(agent.messages[-1].get('extra', {})) if agent.messages else {}
                info.update(exit_status=info.get('exit_status') or type(exc).__name__, exception_str=str(exc), submission='')
                capture_benchmark_attempt(agent, request, folder, info, env, pro_runtime)
                emit('result', info)
            raise
        if request['kind'] == 'swebench':
            capture_benchmark_attempt(agent, request, folder, info, env, pro_runtime)
        emit('result', info)
        completed = info.get('exit_status') == 'Submitted' or (pro and info.get('exit_status') in {'LimitsExceeded','TimeExceeded'} and info.get('patch_capture',{}).get('captured'))
        emit('state', 'completed' if completed else 'failed')
    finally:
        if request['kind'] == 'swebench':
            env.cleanup()
    return info


def capture_benchmark_attempt(agent, request: dict, folder: Path, info: dict, env, runtime: dict) -> None:
    from .bench import prediction
    from .benchmarks import is_pro
    identity = {'instance_id':request['instance_id']} | {key:request[key] for key in ('dataset','benchmark_snapshot','case_key') if key in request}
    if is_pro(request['dataset']):
        from .pro_runtime import capture_patch
        emit('phase', '从本次求解容器保存真实代码改动')
        info['submission'] = ''
        info['patch_capture'] = {'source':'container_git_diff', 'partial':info.get('exit_status') != 'Submitted',
                                 'reason':info.get('exit_status'), 'captured':False}
        info['benchmark_runtime'] = runtime
        agent.save(folder / 'trajectory.traj.json', identity, {'info':info})
        try:
            info['submission'] = capture_patch(env.container_id, env.config.cwd, runtime['head'])
        except Exception as exc:
            info['patch_capture']['error'] = redact(str(exc))
            agent.save(folder / 'trajectory.traj.json', identity, {'info':info})
            emit('result', info)
            raise
        info['patch_capture']['captured'] = bool(info['submission'].strip())
    agent.save(folder / 'trajectory.traj.json', identity, {'info':info})
    if info.get('submission', '').strip() and (info.get('exit_status') == 'Submitted' or info.get('patch_capture',{}).get('captured')):
        pred = prediction(folder / 'trajectory.traj.json', request['instance_id'])
        assert_no_credentials(pred)
        (folder / 'predictions.jsonl').write_text(json.dumps(pred, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
        emit('prediction', str(folder / 'predictions.jsonl'))


def evaluate(request: dict, folder: Path) -> None:
    from .bench import load_instance, ensure_image, check_harness, prediction, dataset_file, docker, summarize_report
    from .runtime import command_environment
    emit('phase', '检查题目、补丁与 Docker 环境；本次不调用模型')
    row = load_instance(request['dataset'], request['instance_id'], request.get('allow_data_download',request.get('allow_download',False)),emit)
    check_harness(row,request['dataset'])
    image = ensure_image(row, request.get('allow_image_download',request.get('allow_download',False)), emit,
                 request['dataset'],request.get('queue_context',{}).get('image_id'))
    pred = prediction(Path(request['prediction_path']), request['instance_id'])
    pred['model_name_or_path'] = pred['model_name_or_path'].replace('/', '__').replace(':', '_')
    assert_no_credentials(pred)
    (folder / 'predictions.jsonl').write_text(json.dumps(pred, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    context = docker('context', 'inspect', '--format', '{{.Endpoints.docker.Host}}')
    if context.returncode:
        raise ValueError('无法读取 Docker 当前连接配置。')
    emit('container', 'sweb.eval.' + request['instance_id'].lower() + '.' + folder.name)
    emit('state', 'evaluating')
    emit('phase', '正在运行测试；暂无新输出时请等待，可随时停止任务')
    from .evaluation_progress import EVALUATION_TIMEOUT,Progress
    stop_progress=threading.Event()
    progress=threading.Thread(target=Progress(folder,emit, row.get('pro_task',{}).get('verifier_timeout',EVALUATION_TIMEOUT)).watch,args=(stop_progress,),daemon=True)
    progress.start()
    command = [sys.executable, '-X', 'utf8', '-m', 'swebench.harness.run_evaluation',
               '-d', str(dataset_file(request['dataset'], request['instance_id'])), '-p', str(folder / 'predictions.jsonl'),
               '-i', request['instance_id'], '--max_workers', '1', '-id', folder.name, '--report_dir', str(folder), '--modal', 'false','--timeout',str(EVALUATION_TIMEOUT)]
    from .benchmarks import is_pro,is_rebench
    evaluator_env={}
    if is_rebench(request['dataset']):
        from .rebench_harness import evaluation_command
        command,evaluator_env=evaluation_command(dataset_file(request['dataset'],request['instance_id']),folder/'predictions.jsonl',
            request['instance_id'],folder.name,folder,EVALUATION_TIMEOUT)
    elif is_pro(request['dataset']):
        from .pro_harness import evaluation_command
        checked = docker('image', 'inspect', image, '--format', '{{.Id}}')
        checked.check_returncode()
        command,evaluator_env=evaluation_command(dataset_file(request['dataset'],request['instance_id']),folder/'predictions.jsonl',
            request['instance_id'],folder.name,folder,row['pro_task']['verifier_timeout'],checked.stdout.strip())
    try:
        process = subprocess.Popen(command, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                   env=command_environment() | {'DOCKER_HOST': context.stdout.strip(), 'HF_HOME': str(ROOT / '.local/hf-cache'), 'PYTHONUTF8': '1'} | evaluator_env,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        for line in process.stdout:
            emit('log', line.rstrip())
        code = process.wait()
    finally:
        stop_progress.set()
        progress.join(timeout=3)
    reports = list(folder.glob('*.' + folder.name + '.json'))
    if not reports:
        raise ValueError('评测未生成汇总报告，请检查日志。退出码：' + str(code))
    report = read_json(reports[0])
    detail = list(folder.glob('logs/run_evaluation/**/report.json'))
    result = summarize_report(report) | {'report_path': str(reports[0]), 'detail': read_json(detail[0]) if detail else {}}
    emit('evaluation', result)
    emit('state', 'failed' if code or result['errors'] or not result['completed'] else 'completed')


def queue_fixture(request,folder):
    if os.getenv('MINI_WEB_TESTING')!='1':raise ValueError('诊断仅允许隔离实例。')
    context=request['queue_context'];scenario=context['diagnostic'];stage=context['stage']
    emit('log','隔离队列诊断样例，不调用模型或 Docker。')
    if scenario=='failure':raise ValueError('离线样例：执行异常')
    if scenario=='unknown_cost' and stage=='solve':
        emit('billing',{'pending':False,'unknown':True})
        raise ValueError('离线样例：计价未知')
    if stage=='solve':
        info=solve(request|{'kind':'offline','cwd':str(folder),'task':'离线队列求解验证',
                            'offline_scenario':'slow_shell' if scenario=='slow' else 'basic'},folder)
        if info.get('exit_status')=='Submitted':
            pred={'instance_id':request['instance_id'],'model_name_or_path':'deterministic',
                  'model_patch':'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n'}
            pred.update({key:request[key] for key in ('dataset','benchmark_snapshot','case_key') if key in request})
            path=folder/'predictions.jsonl';path.write_text(json.dumps(pred)+'\n',encoding='utf-8')
            emit('prediction',str(path))
    elif stage=='evaluate':
        from .bench import prediction,summarize_report
        pred=prediction(Path(request['prediction_path']),request['instance_id'])
        (folder/'input.predictions.jsonl').write_text(json.dumps(pred)+'\n',encoding='utf-8')
        emit('evaluation',summarize_report({'completed_instances':1,'resolved_instances':0 if scenario=='test_failed' else 1,
                                           'unresolved_instances':1 if scenario=='test_failed' else 0,'error_instances':0}))
        emit('state','completed')
    else:
        emit('phase','离线样例：资源检查完成');emit('state','completed')


def main():
    from .docker_runtime import suppress_worker_error_dialogs
    suppress_worker_error_dialogs()
    sys.stdout = SafeLog()
    sys.stderr = SafeLog()
    folder = job_dir(sys.argv[1])
    request = read_json(folder / 'request.json')
    try:
        from .benchmarks import check_snapshot
        if request['kind'] not in {'local','offline'}:
            check_snapshot(request['dataset'],request.get('benchmark_snapshot'))
        emit('state', 'preparing')
        if request.get('queue_context',{}).get('diagnostic'):
            queue_fixture(request,folder)
        elif request['kind']=='catalog_sync':
            from .library import synchronize,check_metadata_space
            check_metadata_space()
            emit('catalog_result',synchronize(request['dataset'],emit))
            emit('state','completed')
        elif request['kind'] in {'prepare','cache','download'}:
            from .bench import load_instance,preview,problem_description
            from .library import check_metadata_space
            check_metadata_space()
            row=load_instance(request['dataset'],request['instance_id'],request.get('allow_data_download',request.get('allow_download',False)),emit)
            if request['kind']=='download':
                from .bench import ensure_image
                ensure_image(row,request.get('allow_image_download',False),emit,request['dataset'])
            if request['kind']=='cache':
                emit('prepared',{'instance_id':request['instance_id'],'dataset':request['dataset'],'problem_statement':problem_description(row,request['dataset']),
                                 'ready':True,'note':'题目资料已缓存；未检查或下载 Docker 镜像。'})
            else:
                emit('phase','检查 Docker 和镜像缓存')
                emit('prepared',preview(request['dataset'],request['instance_id']))
            emit('state','completed')
        elif request['kind'] == 'evaluate':
            evaluate(request, folder)
        else:
            solve(request, folder)
    except Exception as exc:
        emit('error', f'{type(exc).__name__}: {exc}')
        emit('state', 'failed')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
