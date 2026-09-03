"""Project-local instance registry, links, parameters, and virtual layers."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import MetadataError
from .loader import validate_parameter_value
from .paths import atomic_write

LOCAL_DIR = ".mcpack"
SCHEMA = 1
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_name(value: str, label: str = "name") -> str:
    if not _NAME.fullmatch(value) or value in {".", ".."}:
        raise MetadataError(
            f"{label} must be 1-64 ASCII letters, digits, dots, underscores, or hyphens"
        )
    return value


@dataclass(frozen=True)
class InstanceRecord:
    name: str
    path: Path


@dataclass(frozen=True)
class LinkRecord:
    instance: str
    branch: str
    effective_layer: str


class WebServerLockContended(MetadataError):
    """Raised when another process owns the project-wide web-server lock."""


class LocalState:
    """Own all untracked project state under the root ``.mcpack`` directory."""

    def __init__(self, project: Path | str):
        self.project = Path(project).resolve()
        self.root = self.project / LOCAL_DIR

    @property
    def instances_path(self) -> Path:
        return self.root / "instances.json"

    @property
    def links_path(self) -> Path:
        return self.root / "links.json"

    def parameters_path(self, instance: str) -> Path:
        return self.root / "parameters" / f"{validate_name(instance, 'instance name')}.json"

    def feature_root(self, instance: str) -> Path:
        return self.root / "features" / validate_name(instance, "instance name")

    def web_path(self, instance: str) -> Path:
        return self.root / "web" / f"{validate_name(instance, 'instance name')}.json"

    def web_log_path(self, instance: str) -> Path:
        return self.root / "web" / "logs" / f"{validate_name(instance, 'instance name')}.log"

    @property
    def web_server_lock_path(self) -> Path:
        return self.root / "web" / "server.lock"

    @contextmanager
    def web_server_lock(self) -> Iterator[None]:
        """Acquire the project-wide server lifetime lock without waiting."""
        path = self.web_server_lock_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
        except OSError as exc:
            raise MetadataError(f"cannot open web server lock {path}: {exc}") from exc
        try:
            try:
                _lock_file(handle)
            except OSError as exc:
                if _is_lock_contention(exc):
                    raise WebServerLockContended(
                        "web server is already running for this project"
                    ) from None
                raise MetadataError(f"cannot acquire web server lock {path}: {exc}") from exc
            try:
                yield
            finally:
                _unlock_file(handle)
        finally:
            handle.close()

    def probe_web_server_lock(self) -> bool:
        """Return whether the lifetime lock can be acquired immediately."""
        try:
            with self.web_server_lock():
                return True
        except WebServerLockContended:
            return False

    @contextmanager
    def lock(self, timeout: float = 10.0) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._path_lock(self.root / "state.lock", timeout):
            yield

    @contextmanager
    def mutation_lock(self, instance: str, timeout: float = 10.0) -> Iterator[None]:
        path = self.root / "web" / f"{validate_name(instance, 'instance name')}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._path_lock(path, timeout):
            yield

    @contextmanager
    def target_layer_lock(self, layer: Path | str, timeout: float = 10.0) -> Iterator[None]:
        """Serialize edits to one physical layer across web-server processes."""
        canonical = str(Path(layer).resolve()).casefold().encode("utf-8")
        identity = hashlib.sha256(canonical).hexdigest()
        path = self.root / "locks" / "layers" / f"{identity}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._path_lock(path, timeout):
            yield

    @contextmanager
    def _path_lock(self, path: Path, timeout: float) -> Iterator[None]:
        handle = path.open("a+b")
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    _lock_file(handle)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise MetadataError("timed out waiting for .mcpack/state.lock") from None
                    time.sleep(0.05)
            yield
        finally:
            try:
                _unlock_file(handle)
            finally:
                handle.close()

    def instances(self) -> dict[str, InstanceRecord]:
        document = self._read_document(self.instances_path, "instances")
        raw = document["instances"]
        if not isinstance(raw, dict):
            raise MetadataError(f"{self.instances_path}: instances must be an object")
        result: dict[str, InstanceRecord] = {}
        for name, value in raw.items():
            validate_name(name, "instance name")
            if not isinstance(value, dict) or set(value) != {"path"}:
                raise MetadataError(f"{self.instances_path}: invalid instance {name!r}")
            path = value["path"]
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise MetadataError(f"{self.instances_path}: instance path must be absolute")
            result[name] = InstanceRecord(name, Path(path).resolve())
        return result

    def links(self) -> dict[str, LinkRecord]:
        document = self._read_document(self.links_path, "links")
        raw = document["links"]
        if not isinstance(raw, dict):
            raise MetadataError(f"{self.links_path}: links must be an object")
        result: dict[str, LinkRecord] = {}
        for instance, value in raw.items():
            validate_name(instance, "instance name")
            if not isinstance(value, dict) or set(value) != {"branch", "effective_layer"}:
                raise MetadataError(f"{self.links_path}: invalid link {instance!r}")
            branch = value["branch"]
            effective = value["effective_layer"]
            if not isinstance(branch, str) or not isinstance(effective, str):
                raise MetadataError(f"{self.links_path}: link fields must be strings")
            validate_name(branch, "branch name")
            result[instance] = LinkRecord(instance, branch, effective)
        return result

    def parameters(self, instance: str) -> tuple[dict[str, Any], dict[str, str]]:
        path = self.parameters_path(instance)
        document = self._read_document(path, "parameters", "secret_refs")
        parameters = document["parameters"]
        secret_refs = document["secret_refs"]
        if not isinstance(parameters, dict):
            raise MetadataError(f"{path}: parameters must be an object")
        validate_parameter_value(parameters, f"{path}: parameters")
        if not isinstance(secret_refs, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in secret_refs.items()
        ):
            raise MetadataError(f"{path}: secret_refs must be a string object")
        return parameters, secret_refs

    def write_instances(self, records: Mapping[str, InstanceRecord]) -> None:
        self._write_document(
            self.instances_path,
            {
                "instances": {
                    name: {"path": str(item.path)} for name, item in sorted(records.items())
                }
            },
        )

    def write_links(self, records: Mapping[str, LinkRecord]) -> None:
        self._write_document(
            self.links_path,
            {
                "links": {
                    name: {"branch": item.branch, "effective_layer": item.effective_layer}
                    for name, item in sorted(records.items())
                }
            },
        )

    def write_parameters(
        self,
        instance: str,
        parameters: Mapping[str, Any] | None = None,
        secret_refs: Mapping[str, str] | None = None,
    ) -> None:
        validate_parameter_value(dict(parameters or {}))
        self._write_document(
            self.parameters_path(instance),
            {"parameters": dict(parameters or {}), "secret_refs": dict(secret_refs or {})},
        )

    def read_web(self, instance: str) -> dict[str, Any] | None:
        path = self.web_path(instance)
        if not path.exists():
            return None
        document = self._read_document(
            path,
            "status",
            "nonce",
            "management_token",
            optional={"browser_capability", "pid", "port", "csrf", "started_at", "revision"},
        )
        if document["status"] not in {"starting", "running"}:
            raise MetadataError(f"{path}: invalid web status")
        if not all(
            isinstance(document[key], str) and document[key]
            for key in ("nonce", "management_token")
        ):
            raise MetadataError(f"{path}: invalid web reservation")
        if "browser_capability" in document and not isinstance(document["browser_capability"], str):
            raise MetadataError(f"{path}: invalid browser capability")
        if document["status"] == "running":
            if not isinstance(document.get("pid"), int) or not isinstance(
                document.get("port"), int
            ):
                raise MetadataError(f"{path}: running web state needs integer pid and port")
            if not isinstance(document.get("csrf"), str):
                raise MetadataError(f"{path}: running web state needs csrf")
            if not isinstance(document.get("revision"), int):
                raise MetadataError(f"{path}: running web state needs revision")
        return document

    def write_web(self, instance: str, value: Mapping[str, Any]) -> None:
        self._write_document(self.web_path(instance), dict(value))

    def reserve_web(self, instance: str) -> dict[str, str]:
        reservation = {
            "status": "starting",
            "nonce": secrets.token_urlsafe(24),
            "management_token": secrets.token_urlsafe(32),
            "browser_capability": secrets.token_urlsafe(32),
        }
        self.write_web(instance, reservation)
        return reservation

    def _read_document(
        self, path: Path, *required: str, optional: set[str] | None = None
    ) -> dict[str, Any]:
        if not path.exists():
            return {"schema": SCHEMA, **{key: {} for key in required}}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"cannot read local state {path}: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise MetadataError(f"{path}: unsupported or missing schema")
        allowed = {"schema", *required, *(optional or set())}
        if set(value) != allowed and not (set(value) <= allowed and set(required) <= set(value)):
            raise MetadataError(f"{path}: unsupported keys: {sorted(set(value) - allowed)}")
        missing = set(required) - set(value)
        if missing:
            raise MetadataError(f"{path}: missing keys: {sorted(missing)}")
        return value

    @staticmethod
    def _write_document(path: Path, value: Mapping[str, Any]) -> None:
        document = {"schema": SCHEMA, **value}
        atomic_write(
            path,
            (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(),
        )


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(  # type: ignore[attr-defined]
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
        )


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _is_lock_contention(exc: OSError) -> bool:
    return (
        isinstance(exc, BlockingIOError)
        or exc.errno in {errno.EACCES, errno.EAGAIN}
        or getattr(exc, "winerror", None) in {33, 36}
    )
