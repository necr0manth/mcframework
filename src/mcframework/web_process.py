"""Safe background lifecycle for one loopback development server per instance."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .errors import MetadataError
from .local_state import LocalState

_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_FOUND = 1168


class _WindowsProcessApi(Protocol):
    def open_process(self, pid: int) -> int | None: ...

    def wait_for_exit(self, handle: int) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def last_error(self) -> int: ...


class _CtypesWindowsProcessApi:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ctypes: Any = ctypes
        self._open_process: Any = kernel32.OpenProcess
        self._open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._open_process.restype = wintypes.HANDLE
        self._wait_for_single_object: Any = kernel32.WaitForSingleObject
        self._wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._wait_for_single_object.restype = wintypes.DWORD
        self._close_handle: Any = kernel32.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

    def open_process(self, pid: int) -> int | None:
        handle = self._open_process(_SYNCHRONIZE, False, pid)
        if not handle:
            return None
        value = self._ctypes.cast(handle, self._ctypes.c_void_p).value
        return None if value is None else int(value)

    def wait_for_exit(self, handle: int) -> int:
        return int(self._wait_for_single_object(handle, 0))

    def close_handle(self, handle: int) -> None:
        self._close_handle(handle)

    def last_error(self) -> int:
        return int(self._ctypes.get_last_error())


def web_is_alive(state: LocalState, instance: str) -> bool:
    record = state.read_web(instance)
    if record is None or record.get("status") != "running":
        return False
    return _web_record_is_alive(record)


def _web_record_is_alive(record: dict[str, Any]) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{record['port']}/api/v1/health",
        headers={"Authorization": f"Bearer {record['management_token']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return bool(
        response.status == 200
        and payload.get("nonce") == record["nonce"]
        and payload.get("pid") == record["pid"]
    )


def _pid_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if _is_windows():
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_pid_is_alive(pid: int, api: _WindowsProcessApi | None = None) -> bool:
    probe = api or _CtypesWindowsProcessApi()
    handle = probe.open_process(pid)
    if handle is None:
        error = probe.last_error()
        # Access denied and unknown probe failures cannot prove that the PID is gone.
        return error not in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_FOUND}
    try:
        wait_result = probe.wait_for_exit(handle)
        if wait_result == _WAIT_OBJECT_0:
            return False
        if wait_result == _WAIT_TIMEOUT:
            return True
        # A failed or unknown wait must not authorize state deletion.
        return True
    finally:
        probe.close_handle(handle)


def _port_is_open(port: object) -> bool:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def _shutdown_conditions(state: LocalState, record: dict[str, Any]) -> list[str]:
    remaining = []
    if _web_record_is_alive(record):
        remaining.append("authenticated health endpoint is still responding")
    if _pid_is_alive(record.get("pid")):
        remaining.append(f"PID {record.get('pid')} is still alive")
    if _port_is_open(record.get("port")):
        remaining.append(f"port {record.get('port')} is still accepting connections")
    if not state.probe_web_server_lock():
        remaining.append(f"lifetime lock {state.web_server_lock_path} is still held")
    return remaining


def _terminate_owned_process(process: subprocess.Popen[Any], timeout: float) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=max(0.1, timeout))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=max(0.1, timeout))


def start_web(project: Path, instance: str, *, timeout: float = 10.0) -> str:
    state = LocalState(project)
    if not state.probe_web_server_lock():
        raise MetadataError("web server is already running for this project")
    current = state.read_web(instance)
    current_alive = bool(
        current is not None and current.get("status") == "running" and _web_record_is_alive(current)
    )
    with state.lock():
        latest = state.read_web(instance)
        if latest is not None:
            if current is None or (latest.get("nonce"), latest.get("status")) != (
                current.get("nonce"),
                current.get("status"),
            ):
                raise MetadataError("web server state changed while starting; retry")
            if latest.get("status") == "running" and current_alive:
                raise MetadataError(f"web server is already running for {instance!r}")
            if latest.get("status") == "starting":
                started = float(latest.get("started_at", 0.0))
                if time.time() - started < timeout:
                    raise MetadataError(f"web server is already starting for {instance!r}")
            state.web_path(instance).unlink(missing_ok=True)
        reservation = state.reserve_web(instance)
        reservation["started_at"] = time.time()  # type: ignore[assignment]
        state.write_web(instance, reservation)
    log_path = state.web_log_path(instance)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "mcframework.web_server",
        str(project.resolve()),
        instance,
        reservation["nonce"],
    ]
    creationflags = 0
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    with log_path.open("ab") as log:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                creationflags=creationflags,
                **kwargs,
            )
        except OSError as exc:
            with state.lock():
                current = state.read_web(instance)
                if current is not None and current.get("nonce") == reservation["nonce"]:
                    state.web_path(instance).unlink(missing_ok=True)
            raise MetadataError(f"cannot start web server process: {exc}") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = state.read_web(instance)
        if (
            record is not None
            and record.get("status") == "running"
            and record.get("nonce") == reservation["nonce"]
            and web_is_alive(state, instance)
        ):
            return f"http://127.0.0.1:{record['port']}/?token={reservation['browser_capability']}"
        exit_code = process.poll()
        if exit_code is not None:
            with state.lock():
                latest = state.read_web(instance)
                if latest is not None and latest.get("nonce") == reservation["nonce"]:
                    state.web_path(instance).unlink(missing_ok=True)
            if not state.probe_web_server_lock():
                raise MetadataError("web server is already running for this project")
            raise MetadataError(
                f"web server failed to start with exit code {exit_code}; see {log_path}"
            )
        time.sleep(0.05)
    latest = state.read_web(instance)
    entered_running = bool(
        latest is not None
        and latest.get("nonce") == reservation["nonce"]
        and latest.get("status") == "running"
    )
    _terminate_owned_process(process, min(2.0, timeout))
    with state.lock():
        latest = state.read_web(instance)
        if latest is not None and latest.get("nonce") == reservation["nonce"]:
            state.web_path(instance).unlink(missing_ok=True)
    if entered_running:
        raise MetadataError(f"web server entered running state but failed health; see {log_path}")
    raise MetadataError(f"web server failed to start; see {log_path}")


def stop_web(project: Path, instance: str, *, timeout: float = 5.0) -> bool:
    state = LocalState(project)
    record = state.read_web(instance)
    if record is None:
        return False
    if record.get("status") != "running" or not _web_record_is_alive(record):
        remaining = _shutdown_conditions(state, record)
        if remaining:
            raise MetadataError(
                f"web server for {instance!r} is unresponsive or still stopping: "
                + "; ".join(remaining)
            )
        with state.lock():
            latest = state.read_web(instance)
            if latest is not None and latest.get("nonce") == record.get("nonce"):
                state.web_path(instance).unlink(missing_ok=True)
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{record['port']}/api/v1/shutdown",
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {record['management_token']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0):
            pass
    except (OSError, urllib.error.URLError) as exc:
        raise MetadataError(f"cannot stop verified web server for {instance!r}: {exc}") from exc
    deadline = time.monotonic() + timeout
    remaining = _shutdown_conditions(state, record)
    while time.monotonic() < deadline:
        remaining = _shutdown_conditions(state, record)
        if not remaining:
            break
        time.sleep(0.05)
    if remaining:
        raise MetadataError(f"web server for {instance!r} did not stop: " + "; ".join(remaining))
    with state.lock():
        latest = state.read_web(instance)
        if latest is not None and latest.get("nonce") == record["nonce"]:
            state.web_path(instance).unlink(missing_ok=True)
    return True
