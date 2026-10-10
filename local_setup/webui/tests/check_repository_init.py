"""Check repository initialization using an existing image, without a model."""
import json
import subprocess
import uuid
from pathlib import Path

import yaml
from minisweagent.environments.docker import DockerEnvironment
from local_setup.webui.worker import initialize_repository

ROOT=Path(__file__).resolve().parents[3]


def main():
    name='mini-web-init-check-'+uuid.uuid4().hex[:10]
    image='swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest'
    class CheckDocker(DockerEnvironment):
        def _start_container(self):
            result=subprocess.run(['docker','run','--pull','never','--network','none','-d','--rm','--name',name,'-w','/testbed',image,'sleep','120'],capture_output=True,text=True,check=True,timeout=30)
            self.container_id=result.stdout.strip()

        def cleanup(self):
            if self.container_id:
                subprocess.run(['docker','rm','-f',self.container_id],capture_output=True,stdin=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True,timeout=20)
                self.container_id=None

    config=yaml.safe_load((ROOT/'src/minisweagent/config/benchmarks/swebench.yaml').read_text(encoding='utf-8'))['environment']
    config.pop('environment_class');config.update(image=image,timeout=120)
    events=[]
    env=CheckDocker(**config)
    try:
        initialize_repository(env,'d16bfe05a744909de4b27f5875fe0d4ed41ce607',lambda kind,data:events.append((kind,data)))
        assert any('退出码 0' in data and 'HEAD is now' in data for kind,data in events if kind=='log')
        env.config.cwd='/'
        try:
            initialize_repository(env,'d16bfe05a744909de4b27f5875fe0d4ed41ce607',lambda kind,data:events.append((kind,data)))
        except ValueError as exc:
            assert '尚未请求模型' in str(exc) and '命令与日志' in str(exc)
        else:raise AssertionError('Expected wrong repository path to fail')
        assert any('not a git repository' in data and '退出码 128' in data for kind,data in events if kind=='log')
    finally:env.cleanup()
    result={'passed':True,'original_failure_reproduced':False,'cached_instance':'astropy__astropy-12907','correct_repository_initialization':'passed','deliberate_wrong_directory':'exit_128_logged','container_removed':env.container_id is None,'model_calls':0,'image_downloads':0,'events':events}
    out=ROOT/'collaboration/artifacts/2026-09-16/repository-init';out.mkdir(parents=True,exist_ok=True)
    (out/'check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='events'},ensure_ascii=False))


if __name__=='__main__':main()
