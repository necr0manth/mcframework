from __future__ import annotations

import io
import posixpath
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcframework.errors import TransportError, UnsafePathError
from mcframework.sync import Synchronizer
from mcframework.transport import LocalFilesystemInstance, SftpInstance


def test_stateless_local_sync_prunes_only_selected_files(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    (target / "mods").mkdir(parents=True)
    (target / "mods/old.jar").write_bytes(b"old")
    (target / "mods/.index").write_bytes(b"index")
    (target / "mods/readme.txt").write_bytes(b"text")
    (target / "config").mkdir()
    (target / "config/a").write_bytes(b"old")
    report = Synchronizer().sync(
        {"mods/new.jar": b"new", "config/a": b"new-value"},
        LocalFilesystemInstance(target),
        ("mods/*.jar",),
    )
    assert report.created == ("mods/new.jar",)
    assert report.updated == ("config/a",)
    assert report.deleted == ("mods/old.jar",)
    assert (target / "mods/.index").exists()
    assert (target / "mods/readme.txt").exists()


def test_ignore_is_not_a_sync_suppression_rule(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    target.mkdir()
    (target / "logs.txt").write_bytes(b"runtime")
    report = Synchronizer().sync({}, LocalFilesystemInstance(target), ("*.txt",))
    assert report.deleted == ("logs.txt",)


def test_sync_prune_preserves_only_exact_disabled_package_alias(tmp_path: Path) -> None:
    target = tmp_path / "instance"
    (target / "mods").mkdir(parents=True)
    kept = target / "mods/kept.jar.disabled"
    unrelated = target / "mods/unrelated.jar.disabled"
    kept.write_bytes(b"kept")
    unrelated.write_bytes(b"unrelated")

    report = Synchronizer().sync(
        {},
        LocalFilesystemInstance(target),
        ("mods/**",),
        ("mods/kept.jar.disabled",),
    )

    assert report.deleted == ("mods/unrelated.jar.disabled",)
    assert kept.read_bytes() == b"kept"
    assert not unrelated.exists()


def test_sync_renames_live_disabled_mod_and_preserves_disabled_file(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    (root / "mods").mkdir(parents=True)
    active = root / "mods/mod.jar"
    disabled = root / "mods/mod.jar.disabled"
    active.write_bytes(b"mod bytes")

    report = Synchronizer().sync(
        {"config/settings.txt": b"settings"},
        LocalFilesystemInstance(root),
        preserve=("mods/mod.jar.disabled",),
    )

    assert not active.exists()
    assert disabled.read_bytes() == b"mod bytes"
    assert "mods/mod.jar.disabled" in report.created


def test_sync_reports_both_active_and_disabled_mod_without_overwriting(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    (root / "mods").mkdir(parents=True)
    active = root / "mods/mod.jar"
    disabled = root / "mods/mod.jar.disabled"
    active.write_bytes(b"active")
    disabled.write_bytes(b"disabled")

    with pytest.raises(TransportError, match="disabled package conflict"):
        Synchronizer().sync(
            {"config/settings.txt": b"settings"},
            LocalFilesystemInstance(root),
            preserve=("mods/mod.jar.disabled",),
        )

    assert active.read_bytes() == b"active"
    assert disabled.read_bytes() == b"disabled"
    assert not (root / "config/settings.txt").exists()


def test_local_transport_contract_and_path_safety(tmp_path: Path) -> None:
    transport = LocalFilesystemInstance(tmp_path / "instance")
    transport.create_directories("a/b")
    transport.write("a/b/file", b"one")
    assert transport.list_files() == ("a/b/file",)
    assert transport.stat("a/b/file").size == 3
    assert transport.read("a/b/file") == b"one"
    transport.move_or_replace("a/b/file", "moved")
    assert transport.list_files() == ("moved",)
    transport.delete("moved")
    assert transport.stat("moved") is None
    with pytest.raises(UnsafePathError):
        transport.write("../escape", b"bad")


def test_sync_rejects_desired_symlink_before_any_mutation(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    root.mkdir()
    victim = root / "target.txt"
    victim.write_bytes(b"keep")
    link = root / "a-link.txt"
    try:
        link.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(UnsafePathError, match="symlink"):
        Synchronizer().sync(
            {"a-link.txt": b"overwrite", "z-normal.txt": b"new"},
            LocalFilesystemInstance(root),
            ("**",),
        )
    assert victim.read_bytes() == b"keep"
    assert link.is_symlink()
    assert not (root / "z-normal.txt").exists()


class MemoryHandle(io.BytesIO):
    def __init__(self, sftp, path: str, mode: str, initial: bytes = b""):
        super().__init__(initial)
        self.sftp = sftp
        self.path = path
        self.mode = mode

    def close(self) -> None:
        if "w" in self.mode:
            self.sftp.files[self.path] = self.getvalue()
        super().close()


class FakeSftp:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.directories = {"/", "/srv", "/srv/minecraft"}
        self.symlinks: set[str] = set()
        self.closed = False

    def listdir_attr(self, directory: str):
        if directory not in self.directories:
            raise OSError("missing")
        children: dict[str, int] = {}
        prefix = directory.rstrip("/") + "/"
        for item in self.directories:
            if item.startswith(prefix) and "/" not in item[len(prefix) :] and item != directory:
                children[item[len(prefix) :]] = stat.S_IFDIR | 0o755
        for item in self.files:
            if item.startswith(prefix) and "/" not in item[len(prefix) :]:
                children[item[len(prefix) :]] = stat.S_IFREG | 0o644
        return [
            SimpleNamespace(filename=name, st_mode=mode) for name, mode in sorted(children.items())
        ]

    def stat(self, path: str):
        if path in self.directories:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0)
        if path in self.files:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=len(self.files[path]))
        raise OSError("missing")

    def lstat(self, path: str):
        if path in self.symlinks:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=0)
        return self.stat(path)

    def open(self, path: str, mode: str):
        if "r" in mode and path not in self.files:
            raise OSError("missing")
        return MemoryHandle(self, path, mode, self.files.get(path, b""))

    def mkdir(self, path: str):
        if path in self.directories:
            raise OSError("exists")
        parent = posixpath.dirname(path)
        if parent not in self.directories:
            raise OSError("parent missing")
        self.directories.add(path)

    def remove(self, path: str):
        if path not in self.files:
            raise OSError("missing")
        del self.files[path]

    def posix_rename(self, source: str, destination: str):
        if source not in self.files:
            raise OSError("missing")
        self.files[destination] = self.files.pop(source)

    def rename(self, source: str, destination: str):
        if destination in self.files:
            raise OSError("destination exists")
        if source not in self.files:
            raise OSError("missing")
        self.files[destination] = self.files.pop(source)

    def close(self):
        self.closed = True


def test_sftp_mocked_contract_and_sync() -> None:
    backend = FakeSftp()
    backend.files["/srv/minecraft/mods/old.jar"] = b"old"
    backend.directories.add("/srv/minecraft/mods")
    transport = SftpInstance(backend, "/srv/minecraft")
    report = Synchronizer().sync({"mods/new.jar": b"new"}, transport, ("mods/*.jar",))
    assert report.created == ("mods/new.jar",)
    assert report.deleted == ("mods/old.jar",)
    assert transport.read("mods/new.jar") == b"new"
    assert transport.stat("mods/new.jar").size == 3
    transport.move_or_replace("mods/new.jar", "mods/final.jar")
    assert transport.list_files() == ("mods/final.jar",)
    transport.close()
    assert backend.closed


def test_sftp_rejects_relative_root_and_unsafe_paths() -> None:
    backend = FakeSftp()
    with pytest.raises(TransportError, match="absolute"):
        SftpInstance(backend, "relative")
    transport = SftpInstance(backend, "/srv/minecraft")
    with pytest.raises(UnsafePathError):
        transport.read("../escape")


def test_sftp_rejects_remote_symlink_components() -> None:
    backend = FakeSftp()
    backend.symlinks.add("/srv/minecraft/link")
    transport = SftpInstance(backend, "/srv/minecraft")
    with pytest.raises(TransportError, match="symlink"):
        transport.write("link/file.txt", b"unsafe")
    assert "/srv/minecraft/link/file.txt" not in backend.files


def test_sftp_standard_rename_fallback_replaces_with_weaker_atomicity() -> None:
    backend = FakeSftp()
    backend.directories.add("/srv/minecraft/mods")
    backend.files["/srv/minecraft/mods/file.jar"] = b"old"
    backend.posix_rename = None
    transport = SftpInstance(backend, "/srv/minecraft")
    transport.write("mods/file.jar", b"new")
    assert backend.files["/srv/minecraft/mods/file.jar"] == b"new"


def test_sftp_callable_posix_rename_falls_back_only_when_unsupported() -> None:
    backend = FakeSftp()
    backend.directories.add("/srv/minecraft/mods")
    backend.files["/srv/minecraft/mods/file.jar"] = b"old"

    def unsupported(source: str, destination: str) -> None:
        raise OSError("Operation unsupported")

    backend.posix_rename = unsupported
    SftpInstance(backend, "/srv/minecraft").write("mods/file.jar", b"new")
    assert backend.files["/srv/minecraft/mods/file.jar"] == b"new"


def test_sftp_callable_posix_rename_propagates_unrelated_error() -> None:
    backend = FakeSftp()
    backend.directories.add("/srv/minecraft/mods")
    backend.files["/srv/minecraft/mods/file.jar"] = b"old"

    def denied(source: str, destination: str) -> None:
        raise OSError("Permission denied")

    backend.posix_rename = denied
    with pytest.raises(TransportError, match="Permission denied"):
        SftpInstance(backend, "/srv/minecraft").write("mods/file.jar", b"new")
    assert backend.files["/srv/minecraft/mods/file.jar"] == b"old"
