import ctypes
import os
import threading
import time
from ctypes import wintypes

import pytest

from local_setup.webui.common import atomic_bytes


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing semantics')
def test_atomic_replace_handles_transient_reader_without_overwriting_on_permanent_lock(tmp_path):
    target=tmp_path/'trajectory.json'
    target.write_bytes(b'original')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    def lock():
        handle=kernel.CreateFileW(str(target),0x80000000,1,None,3,0,None)
        assert handle not in (None,ctypes.c_void_p(-1).value)
        return handle
    handle=lock()
    def release():
        time.sleep(.12)
        kernel.CloseHandle(handle)
    thread=threading.Thread(target=release)
    thread.start()
    atomic_bytes(target,b'replacement')
    thread.join(timeout=2)
    assert target.read_bytes()==b'replacement'
    handle=lock()
    try:
        with pytest.raises(PermissionError):atomic_bytes(target,b'not-committed')
        assert target.read_bytes()==b'replacement'
        assert not list(tmp_path.glob('*.tmp'))
    finally:
        kernel.CloseHandle(handle)
