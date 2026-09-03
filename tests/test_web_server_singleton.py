from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import mcframework.local_state as local_state_module
import mcframework.web_process as web_process
import mcframework.web_server as web_server
from mcframework.errors import MetadataError
from mcframework.local_state import LocalState, WebServerLockContended
from mcframework.web_process import start_web, stop_web


class FakeProcess:
    def __init__(self, returncode: int | None = None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        self.wait_timeouts.append(timeout)
        return self.returncode or 0


class FakeResponse:
    status = 200

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


class FakeWindowsProcessApi:
    def __init__(self, handle: int | None, error: int, wait_result: int):
        self.handle = handle
        self.error = error
        self.wait_result = wait_result
        self.opened_pids: list[int] = []
        self.closed_handles: list[int] = []

    def open_process(self, pid: int) -> int | None:
        self.opened_pids.append(pid)
        return self.handle

    def wait_for_exit(self, handle: int) -> int:
        assert handle == self.handle
        return self.wait_result

    def close_handle(self, handle: int) -> None:
        self.closed_handles.append(handle)

    def last_error(self) -> int:
        return self.error


@pytest.fixture(autouse=True)
def forbid_real_process_kernel_and_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_popen(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("singleton unit tests must not start child processes")

    def forbidden_kill(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("singleton unit tests must not signal real PIDs")

    def forbidden_ctypes_api(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("singleton unit tests must not construct the real Win32 API")

    def forbidden_socket(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("singleton unit tests must not open real sockets")

    def forbidden_urlopen(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("singleton unit tests must not make real HTTP requests")

    monkeypatch.setattr(web_process.subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(web_process.os, "kill", forbidden_kill)
    monkeypatch.setattr(web_process, "_CtypesWindowsProcessApi", forbidden_ctypes_api)
    monkeypatch.setattr(web_process.socket, "create_connection", forbidden_socket)
    monkeypatch.setattr(web_process.urllib.request, "urlopen", forbidden_urlopen)


def fake_state_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def unlocked(self: LocalState, timeout: float = 10.0) -> Iterator[None]:
        del self, timeout
        yield

    monkeypatch.setattr(LocalState, "lock", unlocked)


def running_record(nonce: str = "owned") -> dict[str, object]:
    return {
        "status": "running",
        "nonce": nonce,
        "management_token": "management",
        "csrf": "csrf",
        "pid": 1234,
        "port": 4567,
        "started_at": 1.0,
        "revision": 0,
    }


@pytest.mark.parametrize(
    ("handle", "error", "wait_result", "expected", "closed"),
    [
        (None, 87, web_process._WAIT_TIMEOUT, False, []),
        (None, 5, web_process._WAIT_TIMEOUT, True, []),
        (44, 0, web_process._WAIT_TIMEOUT, True, [44]),
        (44, 0, web_process._WAIT_OBJECT_0, False, [44]),
        (44, 0, 0xFFFFFFFF, True, [44]),
    ],
)
def test_windows_pid_probe_is_read_only_conservative_and_closes_handles(
    handle: int | None,
    error: int,
    wait_result: int,
    expected: bool,
    closed: list[int],
) -> None:
    api = FakeWindowsProcessApi(handle, error, wait_result)
    assert web_process._windows_pid_is_alive(1234, api) is expected
    assert api.opened_pids == [1234]
    assert api.closed_handles == closed


def test_windows_pid_dispatch_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[int] = []

    def windows_probe(pid: int) -> bool:
        probed.append(pid)
        return True

    def forbidden_kill(pid: int, signal: int) -> None:
        del pid, signal
        raise AssertionError("Windows PID checks must never call os.kill")

    monkeypatch.setattr(web_process, "_is_windows", lambda: True)
    monkeypatch.setattr(web_process, "_windows_pid_is_alive", windows_probe)
    monkeypatch.setattr(web_process.os, "kill", forbidden_kill)
    assert web_process._pid_is_alive(1234) is True
    assert probed == [1234]


def test_posix_pid_dispatch_retains_signal_zero_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, signal: int) -> None:
        calls.append((pid, signal))
        if pid == 2:
            raise ProcessLookupError
        if pid == 3:
            raise PermissionError
        if pid == 4:
            raise OSError

    monkeypatch.setattr(web_process, "_is_windows", lambda: False)
    monkeypatch.setattr(web_process.os, "kill", fake_kill)
    assert web_process._pid_is_alive(1) is True
    assert web_process._pid_is_alive(2) is False
    assert web_process._pid_is_alive(3) is True
    assert web_process._pid_is_alive(4) is False
    assert calls == [(1, 0), (2, 0), (3, 0), (4, 0)]


def test_stop_windows_path_never_signals_recorded_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_state_lock(monkeypatch)
    state = LocalState(tmp_path)
    state.write_web("first", running_record())
    probed: list[int] = []

    def windows_probe(pid: int) -> bool:
        probed.append(pid)
        return False

    def forbidden_kill(pid: int, signal: int) -> None:
        del pid, signal
        raise AssertionError("stop_web must not signal a recorded Windows PID")

    monkeypatch.setattr(web_process, "_is_windows", lambda: True)
    monkeypatch.setattr(web_process, "_windows_pid_is_alive", windows_probe)
    monkeypatch.setattr(web_process.os, "kill", forbidden_kill)
    monkeypatch.setattr(web_process, "_web_record_is_alive", lambda value: False)
    monkeypatch.setattr(web_process, "_port_is_open", lambda value: False)
    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: True)

    assert stop_web(tmp_path, "first") is False
    assert probed == [1234]
    assert state.read_web("first") is None


def test_project_web_lock_is_nonblocking_and_reports_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = LocalState(tmp_path)
    held = False

    def fake_lock(handle: object) -> None:
        del handle
        nonlocal held
        if held:
            raise BlockingIOError(13, "held")
        held = True

    def fake_unlock(handle: object) -> None:
        del handle
        nonlocal held
        held = False

    monkeypatch.setattr(local_state_module, "_lock_file", fake_lock)
    monkeypatch.setattr(local_state_module, "_unlock_file", fake_unlock)
    with state.web_server_lock():
        assert state.web_server_lock_path.is_file()
        assert state.probe_web_server_lock() is False
        with (
            pytest.raises(WebServerLockContended, match="already running for this project"),
            state.web_server_lock(),
        ):
            pass
    assert state.probe_web_server_lock() is True

    original_open = Path.open

    def deny(path: Path, *args: object, **kwargs: object) -> Any:
        if path == state.web_server_lock_path:
            raise PermissionError("injected permission denial")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(MetadataError, match=r"cannot open web server lock .*server\.lock"):
        state.probe_web_server_lock()


def test_start_rejects_project_lock_contention_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservations = 0

    def reserve(self: LocalState, instance: str) -> dict[str, str]:
        del self, instance
        nonlocal reservations
        reservations += 1
        raise AssertionError("reservation must not be created while the lifetime lock is held")

    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: False)
    monkeypatch.setattr(LocalState, "reserve_web", reserve)
    with pytest.raises(MetadataError, match="already running for this project"):
        start_web(tmp_path, "second")
    assert reservations == 0
    assert LocalState(tmp_path).read_web("second") is None


def test_start_loser_polls_child_early_and_removes_its_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_state_lock(monkeypatch)
    probes = iter((True, False))
    process = FakeProcess(returncode=2)
    popen_calls = 0

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        nonlocal popen_calls
        popen_calls += 1
        return process

    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: next(probes))
    monkeypatch.setattr(web_process.subprocess, "Popen", fake_popen)
    with pytest.raises(MetadataError, match="already running for this project"):
        start_web(tmp_path, "second", timeout=1.0)
    assert popen_calls == 1
    assert process.terminated is False
    assert LocalState(tmp_path).read_web("second") is None


def test_start_timeout_terminates_owned_running_child_before_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_state_lock(monkeypatch)
    process = FakeProcess()
    state = LocalState(tmp_path)

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        reservation = state.read_web("first")
        assert reservation is not None
        state.write_web("first", {**running_record(str(reservation["nonce"]))})
        return process

    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: True)
    monkeypatch.setattr(web_process.subprocess, "Popen", fake_popen)
    with pytest.raises(MetadataError, match="entered running state but failed health"):
        start_web(tmp_path, "first", timeout=0.0)
    assert process.terminated is True
    assert process.killed is False
    assert state.read_web("first") is None


def test_start_timeout_preserves_replacement_nonce_and_touches_only_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_state_lock(monkeypatch)
    owned = FakeProcess()
    foreign = FakeProcess()
    state = LocalState(tmp_path)
    replacement = running_record("replacement")

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        del args, kwargs
        state.write_web("first", replacement)
        return owned

    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: True)
    monkeypatch.setattr(web_process.subprocess, "Popen", fake_popen)
    with pytest.raises(MetadataError, match="failed to start"):
        start_web(tmp_path, "first", timeout=0.0)
    assert owned.terminated is True
    assert foreign.terminated is False
    assert state.read_web("first") == {"schema": 1, **replacement}


def test_stop_waits_for_health_pid_port_and_lifetime_lock_without_state_lock_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = LocalState(tmp_path)
    record = running_record()
    state.write_web("first", record)
    inside_state_lock = False

    @contextmanager
    def guarded_lock(self: LocalState, timeout: float = 10.0) -> Iterator[None]:
        del self, timeout
        nonlocal inside_state_lock
        assert not inside_state_lock
        inside_state_lock = True
        try:
            yield
        finally:
            inside_state_lock = False

    health = iter((True, True, False, False, False, False))
    pids = iter((True, True, False, False, False))
    ports = iter((True, True, True, False, False))
    locks = iter((False, False, False, False, True))
    clock = iter((0.0, 0.1, 0.2, 0.3, 0.4))

    def not_under_lock(value: Iterator[bool]) -> bool:
        assert not inside_state_lock
        return next(value)

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        del request, timeout
        assert not inside_state_lock
        return FakeResponse()

    monkeypatch.setattr(LocalState, "lock", guarded_lock)
    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: not_under_lock(locks))
    monkeypatch.setattr(web_process, "_web_record_is_alive", lambda value: not_under_lock(health))
    monkeypatch.setattr(web_process, "_pid_is_alive", lambda value: not_under_lock(pids))
    monkeypatch.setattr(web_process, "_port_is_open", lambda value: not_under_lock(ports))
    monkeypatch.setattr(web_process.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(web_process.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(web_process.time, "sleep", lambda seconds: None)

    assert stop_web(tmp_path, "first", timeout=1.0) is True
    assert inside_state_lock is False
    assert state.read_web("first") is None


def test_stop_timeout_preserves_state_until_every_shutdown_condition_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_state_lock(monkeypatch)
    state = LocalState(tmp_path)
    record = running_record()
    state.write_web("first", record)
    clock = iter((0.0, 2.0))

    monkeypatch.setattr(web_process, "_web_record_is_alive", lambda value: True)
    monkeypatch.setattr(web_process, "_pid_is_alive", lambda value: True)
    monkeypatch.setattr(web_process, "_port_is_open", lambda value: True)
    monkeypatch.setattr(LocalState, "probe_web_server_lock", lambda self: False)
    monkeypatch.setattr(
        web_process.urllib.request, "urlopen", lambda request, timeout: FakeResponse()
    )
    monkeypatch.setattr(web_process.time, "monotonic", lambda: next(clock))

    with pytest.raises(
        MetadataError,
        match="health endpoint.*PID 1234.*port 4567.*lifetime lock",
    ):
        stop_web(tmp_path, "first", timeout=1.0)
    assert state.read_web("first") == {"schema": 1, **record}


def test_child_holds_authoritative_project_lock_across_server_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class FakePath:
        def unlink(self, *, missing_ok: bool) -> None:
            assert missing_ok
            events.append("unlink")

    class FakeState:
        def __init__(self, project: Path):
            del project

        @contextmanager
        def web_server_lock(self) -> Iterator[None]:
            events.append("lifetime-enter")
            try:
                yield
            finally:
                events.append("lifetime-exit")

        @contextmanager
        def lock(self) -> Iterator[None]:
            events.append("state-enter")
            try:
                yield
            finally:
                events.append("state-exit")

        def read_web(self, instance: str) -> dict[str, str]:
            del instance
            return {"nonce": "owned"}

        def web_path(self, instance: str) -> FakePath:
            del instance
            return FakePath()

    class FakeServer:
        def __init__(self, project: Path, instance: str, nonce: str):
            del project, instance, nonce
            assert events == ["lifetime-enter"]
            events.append("construct")

        def serve_forever(self, *, poll_interval: float) -> None:
            assert poll_interval == 0.1
            events.append("serve")

        def server_close(self) -> None:
            events.append("close")

    monkeypatch.setattr(web_server, "LocalState", FakeState)
    monkeypatch.setattr(web_server, "McpackServer", FakeServer)
    assert web_server.main([str(tmp_path), "first", "owned"]) == 0
    assert events == [
        "lifetime-enter",
        "construct",
        "serve",
        "close",
        "state-enter",
        "unlink",
        "state-exit",
        "lifetime-exit",
    ]


def test_child_lock_loser_cleanup_is_guarded_by_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlinked = False

    class FakePath:
        def unlink(self, *, missing_ok: bool) -> None:
            del missing_ok
            nonlocal unlinked
            unlinked = True

    class FakeState:
        def __init__(self, project: Path):
            del project

        @contextmanager
        def web_server_lock(self) -> Iterator[None]:
            raise WebServerLockContended("held")
            yield

        @contextmanager
        def lock(self) -> Iterator[None]:
            yield

        def read_web(self, instance: str) -> dict[str, str]:
            del instance
            return {"nonce": "replacement"}

        def web_path(self, instance: str) -> FakePath:
            del instance
            return FakePath()

    monkeypatch.setattr(web_server, "LocalState", FakeState)
    assert web_server.main([str(tmp_path), "first", "loser"]) == 2
    assert unlinked is False


def test_constructor_failure_after_bind_closes_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = LocalState(tmp_path)
    reservation = state.reserve_web("first")
    events: list[str] = []

    def fake_server_init(
        server: Any,
        address: tuple[str, int],
        handler: type[object],
        bind_and_activate: bool = True,
    ) -> None:
        del address, handler, bind_and_activate
        server.server_port = 4567
        events.append("bind")

    def fake_server_close(server: object) -> None:
        del server
        events.append("close")

    def fail_service(project: Path) -> None:
        del project
        raise MetadataError("injected constructor failure")

    monkeypatch.setattr(web_server.ThreadingHTTPServer, "__init__", fake_server_init)
    monkeypatch.setattr(web_server.ThreadingHTTPServer, "server_close", fake_server_close)
    monkeypatch.setattr(web_server, "ProjectService", fail_service)
    with pytest.raises(MetadataError, match="injected constructor failure"):
        web_server.McpackServer(tmp_path, "first", reservation["nonce"])
    assert events == ["bind", "close"]
