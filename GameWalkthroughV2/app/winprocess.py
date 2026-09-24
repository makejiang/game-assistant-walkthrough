"""Windows 进程树控制：把子进程绑进"父进程退出即全灭"的 Job Object。

客户端常以 pythonw / agent 方式运行，被 taskkill /F 强杀时任何优雅清理
都不会执行，子进程（浮窗、ssh 隧道）会变成孤儿继续驻留。这里提供内核级
兜底：子进程加入 KILL_ON_JOB_CLOSE 的 Job，父进程持有 Job 句柄——父进程
退出（无论怎么死）句柄被内核关闭 → Job 内所有进程随之终止。
"""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import sys
from ctypes import wintypes

_JOB_KILL_ON_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def assign_kill_on_close(proc: subprocess.Popen) -> int | None:
    """把已启动的子进程放进"父进程句柄关闭即全灭"的 Job。

    返回 Job 句柄——调用方（父进程）必须一直持有，正常 stop() 或父进程退出
    时由 close_job_handle / 内核关闭。失败返回 None（调用方自行兜底）。
    """
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPWSTR, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            kernel32.CloseHandle(job)
            return None
        return int(job)  # 故意保持打开：父进程退出时内核关闭句柄 → Job 全灭
    except Exception:
        return None


def close_job_handle(job: int | None) -> None:
    """正常收尾时释放 Job 句柄（子进程已终止，再关闭只是还句柄）。"""
    if job:
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.CloseHandle(job)
