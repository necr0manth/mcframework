"""Filesystem generations used to invalidate verified in-memory content."""

from __future__ import annotations

import os
import time
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation", ctypes.c_longlong),
            ("access", ctypes.c_longlong),
            ("write", ctypes.c_longlong),
            ("change", ctypes.c_longlong),
            ("attributes", wintypes.DWORD),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _create = _kernel.CreateFileW
    _create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _create.restype = wintypes.HANDLE
    _query = _kernel.GetFileInformationByHandleEx
    _query.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _query.restype = wintypes.BOOL
    _close = _kernel.CloseHandle
    _close.argtypes = [wintypes.HANDLE]
    _close.restype = wintypes.BOOL


def _change_time(path: Path, fallback: int) -> int:
    if os.name != "nt":
        return fallback
    # Windows st_ctime is creation time, not inode change time. Query ChangeTime
    # without following reparse points, including directories, with all sharing.
    handle = _create(str(path), 0x80, 7, None, 3, 0x02000000 | 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        return -time.monotonic_ns()  # Unqueryable files must never be cache hits.
    try:
        info = _BasicInfo()
        if not _query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            return -time.monotonic_ns()
        return int(info.change)
    finally:
        _close(handle)


Generation = tuple[int, int, int, int, int] | None


def generation(path: Path) -> Generation:
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        current.st_mode,
        current.st_mtime_ns,
        _change_time(path, current.st_ctime_ns),
        current.st_size,
        current.st_ino,
    )
