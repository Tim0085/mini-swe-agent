import subprocess
import sys

import pytest

from local_setup.webui.docker_runtime import DockerHostError, check_docker_result
from local_setup.webui.runtime import stream_command
from local_setup.webui.results import Results
from local_setup.webui.queues import BatchCommit, BatchControl
from local_setup.webui.server import manager, queues
from test_results import run, evaluation
from test_library_queues import queue_client
from test_experiments import experiment, model_profiles
from uuid import uuid4


def test_host_initialization_exit_is_fatal_but_test_failure_is_not():
    result=stream_command([sys.executable,'-c','import ctypes;ctypes.windll.kernel32.ExitProcess(0xc0000142)'],emit=lambda *args:None)
    with pytest.raises(DockerHostError,match='0xc0000142'):
        check_docker_result(result)
    with pytest.raises(DockerHostError):
        check_docker_result({'returncode':-1073741502})
    assert check_docker_result(subprocess.CompletedProcess(['docker'],1)).returncode==1


def test_comparison_defaults_align_cases_and_expose_failure_evidence(tmp_path):
    a=run(1)
    b=run(3,profile_snapshot=a['profile_snapshot']|{'model_name':'openai/second','profile_id':'two','profile_name':'Model B'})
    c=run(5,instance_id='other__repo-1',profile_snapshot=a['profile_snapshot']|{'model_name':'openai/third'})
    duplicate=run(7,profile_snapshot=a['profile_snapshot']|{'limits':{'step_limit':50,'cost_limit':3}})
    result=Results([a,evaluation(2,a,False),b,evaluation(4,b,False),c,evaluation(6,c),duplicate,evaluation(8,duplicate)],run_root=tmp_path)
    data=result.listing();comparison=result.compare(data['comparison_defaults'])
    assert comparison['summary']['common_instances']==1 and len(comparison['groups'])==2
    assert len({group['model'] for group in comparison['groups']})==2
    assert comparison['rows'][0]['cells'][0]['tests']['repair']['failure_names']==['repair']


def test_unknown_billing_rejects_resume_before_changing_queue_state(queue_client):
    _,profiles=model_profiles(queue_client,1)
    plan=experiment(queue_client,profiles)
    with manager.lock:
        row=queues.commit(BatchCommit(token=plan['token'],request_id=uuid4().hex))
        q=queues.records[row['id']];q.update(status='paused',billing_unknown=True)
        before=set(manager.records)
        with pytest.raises(ValueError,match='费用无法确认'):
            queues.control(q['id'],BatchControl(action='resume'))
        assert q['status']=='paused' and set(manager.records)==before
