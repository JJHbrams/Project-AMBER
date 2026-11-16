"""Windows Job Object helpers: tie child processes to the creator's lifetime.

Windows does not kill children when a parent dies. A job created with
KILL_ON_JOB_CLOSE makes the kernel kill every member once the last handle to
the job closes, which happens automatically when the owning process exits (even
on a native crash). Every function here is best-effort: it never raises and is
a no-op (returns None / False) off Windows.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


def create_kill_on_close_job():
    """Return a job handle (int) with KILL_ON_JOB_CLOSE, or None if unavailable."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _ExtLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _ExtLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        if not k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle(job)
            return None
        return int(job)
    except Exception:
        log.debug("[job] create_kill_on_close_job failed", exc_info=True)
        return None


def close_job(job) -> None:
    """Close a job handle (kills all members). Used by tests; hosts never call it."""
    if not job or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle(job)


def assign_to_job(job, proc, label: str = "child") -> bool:
    """Assign a subprocess.Popen to ``job``. Logs a warning and returns False on failure."""
    if not job or proc is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):  # type: ignore[attr-defined]
            log.warning("[job] %s job 할당 실패 pid=%s winerror=%s", label,
                        getattr(proc, "pid", "?"), ctypes.get_last_error())
            return False
        return True
    except Exception as exc:
        log.warning("[job] %s job 할당 예외 pid=%s: %s", label, getattr(proc, "pid", "?"), exc)
        return False
