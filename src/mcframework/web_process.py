"""Safe background lifecycle for one loopback development server per instance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import MetadataError
from .local_state import LocalState


def web_is_alive(state: LocalState, instance: str) -> bool:
    record = state.read_web(instance)
    if record is None or record.get("status") != "running":
        return False
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


def start_web(project: Path, instance: str, *, timeout: float = 10.0) -> str:
    state = LocalState(project)
    with state.lock():
        current = state.read_web(instance)
        if current is not None:
            if current.get("status") == "running" and web_is_alive(state, instance):
                raise MetadataError(f"web server is already running for {instance!r}")
            if current.get("status") == "starting":
                started = float(current.get("started_at", 0.0))
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
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            creationflags=creationflags,
            **kwargs,
        )
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
        time.sleep(0.05)
    raise MetadataError(f"web server failed to start; see {log_path}")


def stop_web(project: Path, instance: str, *, timeout: float = 5.0) -> bool:
    state = LocalState(project)
    record = state.read_web(instance)
    if record is None:
        return False
    if record.get("status") != "running" or not web_is_alive(state, instance):
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
    while time.monotonic() < deadline and web_is_alive(state, instance):
        time.sleep(0.05)
    if web_is_alive(state, instance):
        raise MetadataError(f"web server for {instance!r} did not stop")
    with state.lock():
        latest = state.read_web(instance)
        if latest is not None and latest.get("nonce") == record["nonce"]:
            state.web_path(instance).unlink(missing_ok=True)
    return True
