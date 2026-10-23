"""Read Windows process integrity without changing tokens, ACLs or executables."""
from __future__ import annotations

import os


def process_integrity(pid):
    if os.name != 'nt':
        return dict(status='not_applicable')
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,
                                              wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(wintypes.BYTE)
    security.GetSidSubAuthority.argtypes = [ctypes.c_void_p,wintypes.DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    process,token = None,wintypes.HANDLE()
    try:
        process = kernel.OpenProcess(0x1000,False,int(pid))  # QUERY_LIMITED_INFORMATION
        if not process or not security.OpenProcessToken(process,0x0008,ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        security.GetTokenInformation(token,25,None,0,ctypes.byref(size))  # TokenIntegrityLevel
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token,25,data,size.value,ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data,ctypes.POINTER(ctypes.c_void_p))[0]
        count = security.GetSidSubAuthorityCount(sid)[0]
        rid = security.GetSidSubAuthority(sid,count-1)[0]
        level = ('untrusted' if rid<4096 else 'low' if rid<8192 else
                 'medium' if rid<12288 else 'high' if rid<16384 else 'system')
        return dict(status='observed',pid=int(pid),rid=rid,level=level)
    except OSError as error:
        return dict(status='unavailable',pid=int(pid),error=str(error))
    finally:
        if token:
            kernel.CloseHandle(token)
        if process:
            kernel.CloseHandle(process)


def worker_access(pid):
    host,worker = process_integrity(os.getpid()),process_integrity(pid)
    result = dict(file_policy_requested='danger-full-access',host=host,worker=worker)
    if worker.get('status')=='observed' and worker['rid']<8192:
        result['warning'] = ('DSH worker 的 Windows 完整性低于普通级；即使会话已允许修改，'
            'Windows 仍可能拒绝写入普通完整性目录。EPERM 不能据此解释为会话只读。'
            '不要自行变更系统权限；说明实际限制，由用户在本机修复运行时权限后重启。')
    return result
