"""Fail closed when the host Docker executable cannot initialize."""
import os


class DockerHostError(OSError):
    pass


def check_docker_result(result):
    if isinstance(result, dict) and result.get('timed_out'):
        raise DockerHostError('Docker 命令已超时，容器内命令状态无法确认。已停止本次求解和后续模型请求，正在清理所属容器；请查看清理结果后明确重试。')
    code = result.returncode if hasattr(result, 'returncode') else result['returncode']
    if code & 0xffffffff in {0xc0000142, 0xc0000135, 0xc000007b, 0xc0000005}:
        raise DockerHostError(f'Windows 无法正常运行 Docker 命令（0x{code & 0xffffffff:08x}）。已停止本次求解，不再请求模型。请检查 Docker 运行状态后明确重试。')
    return result


def suppress_worker_error_dialogs():
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetErrorMode(kernel.GetErrorMode() | 0x0001 | 0x0002)
