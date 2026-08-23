"""Local and optional-Paramiko instance filesystem transports."""

from __future__ import annotations

import posixpath
import stat as stat_module
from contextlib import suppress
from dataclasses import dataclass
from errno import ENOSYS, ENOTSUP, EOPNOTSUPP
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .errors import TransportError
from .paths import atomic_write, confined_path, normalize_relative_path


@dataclass(frozen=True)
class FileStat:
    size: int


class InstanceTransport(Protocol):
    def list_files(self) -> tuple[str, ...]: ...
    def stat(self, path: str) -> FileStat | None: ...
    def read(self, path: str) -> bytes: ...
    def write(self, path: str, content: bytes) -> None: ...
    def create_directories(self, path: str) -> None: ...
    def delete(self, path: str) -> None: ...
    def move_or_replace(self, source: str, destination: str) -> None: ...


class LocalFilesystemInstance:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list_files(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                file.relative_to(self.root).as_posix()
                for file in self.root.rglob("*")
                if file.is_file() and not file.is_symlink()
            )
        )

    def stat(self, path: str) -> FileStat | None:
        target = confined_path(self.root, path)
        return FileStat(target.stat().st_size) if target.is_file() else None

    def read(self, path: str) -> bytes:
        target = confined_path(self.root, path)
        if not target.is_file():
            raise TransportError(f"instance file does not exist: {path!r}")
        return target.read_bytes()

    def write(self, path: str, content: bytes) -> None:
        atomic_write(confined_path(self.root, path), content)

    def create_directories(self, path: str) -> None:
        confined_path(self.root, path).mkdir(parents=True, exist_ok=True)

    def delete(self, path: str) -> None:
        target = confined_path(self.root, path)
        if target.is_dir():
            raise TransportError(f"refusing to delete directory through file API: {path!r}")
        target.unlink(missing_ok=True)

    def move_or_replace(self, source: str, destination: str) -> None:
        source_path = confined_path(self.root, source)
        destination_path = confined_path(self.root, destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.replace(destination_path)


class SftpInstance:
    """SFTP transport. Paramiko is imported only by ``connect``."""

    def __init__(self, sftp: Any, root: str, *, ssh_client: Any | None = None):
        if not root.startswith("/"):
            raise TransportError("SFTP root must be an absolute POSIX path")
        self.sftp = sftp
        self.root = posixpath.normpath(root)
        self.ssh_client = ssh_client

    @classmethod
    def connect(
        cls,
        *,
        host: str,
        root: str,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        key_filename: str | None = None,
        known_hosts: str | None = None,
    ) -> SftpInstance:
        try:
            import paramiko
        except ImportError as exc:
            raise TransportError("SFTP support requires the 'sftp' optional dependency") from exc
        client = paramiko.SSHClient()
        if known_hosts:
            client.load_host_keys(known_hosts)
        else:
            client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                host,
                port=port,
                username=username,
                password=password,
                key_filename=key_filename,
            )
            return cls(client.open_sftp(), root, ssh_client=client)
        except Exception as exc:
            client.close()
            raise TransportError(f"cannot connect to SFTP host {host!r}: {exc}") from exc

    def close(self) -> None:
        self.sftp.close()
        if self.ssh_client is not None:
            self.ssh_client.close()

    def list_files(self) -> tuple[str, ...]:
        files: list[str] = []
        self._assert_root_safe()

        def visit(remote: str, relative: str) -> None:
            try:
                entries = self.sftp.listdir_attr(remote)
            except OSError as exc:
                raise TransportError(f"cannot list SFTP directory {remote!r}: {exc}") from exc
            for entry in entries:
                child_relative = f"{relative}/{entry.filename}" if relative else entry.filename
                child_remote = posixpath.join(remote, entry.filename)
                if stat_module.S_ISDIR(entry.st_mode):
                    visit(child_remote, child_relative)
                elif stat_module.S_ISREG(entry.st_mode):
                    files.append(normalize_relative_path(child_relative))

        visit(self.root, "")
        return tuple(sorted(files))

    def stat(self, path: str) -> FileStat | None:
        remote = self._validated_path(path, allow_missing=True)
        try:
            result = self.sftp.stat(remote)
        except OSError:
            return None
        return FileStat(result.st_size)

    def read(self, path: str) -> bytes:
        remote = self._validated_path(path, allow_missing=False)
        try:
            with self.sftp.open(remote, "rb") as handle:
                return bytes(handle.read())
        except OSError as exc:
            raise TransportError(f"cannot read SFTP file {path!r}: {exc}") from exc

    def write(self, path: str, content: bytes) -> None:
        normalized = normalize_relative_path(path)
        destination = self._validated_path(normalized, allow_missing=True)
        parent = PurePosixPath(normalized).parent.as_posix()
        if parent != ".":
            self.create_directories(parent)
        temporary = f"{destination}.mcpack-tmp"
        self._assert_absolute_safe(temporary, allow_missing=True)
        try:
            with self.sftp.open(temporary, "wb") as handle:
                handle.write(content)
            self._replace(temporary, destination)
        except OSError as exc:
            with suppress(OSError):
                self.sftp.remove(temporary)
            raise TransportError(f"cannot write SFTP file {path!r}: {exc}") from exc

    def create_directories(self, path: str) -> None:
        normalized = normalize_relative_path(path)
        self._validated_path(normalized, allow_missing=True)
        current = self.root
        for part in PurePosixPath(normalized).parts:
            current = posixpath.join(current, part)
            try:
                self.sftp.mkdir(current)
            except OSError:
                try:
                    if not stat_module.S_ISDIR(self.sftp.stat(current).st_mode):
                        raise TransportError(f"SFTP path exists but is not a directory: {current}")
                except OSError as exc:
                    raise TransportError(
                        f"cannot create SFTP directory {current!r}: {exc}"
                    ) from exc

    def delete(self, path: str) -> None:
        remote = self._validated_path(path, allow_missing=False)
        try:
            self.sftp.remove(remote)
        except OSError as exc:
            raise TransportError(f"cannot delete SFTP file {path!r}: {exc}") from exc

    def move_or_replace(self, source: str, destination: str) -> None:
        source_remote = self._validated_path(source, allow_missing=False)
        destination_remote = self._validated_path(destination, allow_missing=True)
        try:
            self._replace(source_remote, destination_remote)
        except OSError as exc:
            raise TransportError(
                f"cannot move SFTP file {source!r} to {destination!r}: {exc}"
            ) from exc

    def _path(self, path: str) -> str:
        normalized = normalize_relative_path(path)
        remote = posixpath.normpath(posixpath.join(self.root, normalized))
        if remote != self.root and not remote.startswith(self.root.rstrip("/") + "/"):
            raise TransportError(f"SFTP path escapes root: {path!r}")
        return remote

    def _validated_path(self, path: str, *, allow_missing: bool) -> str:
        remote = self._path(path)
        self._assert_root_safe()
        current = self.root
        for part in PurePosixPath(normalize_relative_path(path)).parts:
            current = posixpath.join(current, part)
            try:
                attributes = self.sftp.lstat(current)
            except OSError:
                if allow_missing:
                    return remote
                raise TransportError(f"SFTP path does not exist: {path!r}") from None
            if stat_module.S_ISLNK(attributes.st_mode):
                raise TransportError(f"managed SFTP path contains a symlink: {path!r}")
        return remote

    def _assert_root_safe(self) -> None:
        current = "/"
        for part in PurePosixPath(self.root).parts:
            if part == "/":
                continue
            current = posixpath.join(current, part)
            try:
                attributes = self.sftp.lstat(current)
            except OSError as exc:
                raise TransportError(f"SFTP root does not exist: {self.root!r}") from exc
            if stat_module.S_ISLNK(attributes.st_mode):
                raise TransportError(f"SFTP root contains a symlink: {current!r}")

    def _assert_absolute_safe(self, remote: str, *, allow_missing: bool) -> None:
        relative = posixpath.relpath(remote, self.root)
        if relative == "." or relative.startswith("../"):
            raise TransportError(f"SFTP path escapes root: {remote!r}")
        self._validated_path(relative, allow_missing=allow_missing)

    def _replace(self, source: str, destination: str) -> None:
        posix_rename = getattr(self.sftp, "posix_rename", None)
        if callable(posix_rename):
            try:
                posix_rename(source, destination)
                return
            except OSError as exc:
                if not is_unsupported_operation(exc):
                    raise
        self._standard_replace(source, destination)

    def _standard_replace(self, source: str, destination: str) -> None:
        # Standard SFTP rename is not guaranteed to overwrite. Removing the
        # destination first is a compatibility fallback and is not atomic.
        try:
            self.sftp.lstat(destination)
        except OSError:
            pass
        else:
            self.sftp.remove(destination)
        self.sftp.rename(source, destination)


def is_unsupported_operation(error: OSError) -> bool:
    if error.errno in {ENOSYS, ENOTSUP, EOPNOTSUPP}:
        return True
    message = " ".join(str(argument) for argument in error.args).lower()
    return any(
        marker in message
        for marker in ("operation unsupported", "unsupported operation", "operation not supported")
    )
