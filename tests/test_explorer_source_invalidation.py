from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import mcframework.project as project_module
from mcframework.project import ProjectService


def write_changed(path: Path, content: str) -> None:
    previous = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(content, encoding="utf-8")
    os.utime(path, ns=(previous + 1_000_000_000,) * 2)


def linked_service(project: Path, branch: str = "client") -> tuple[ProjectService, Path]:
    game = project.parent / f"{project.name}-game"
    game.mkdir()
    service = ProjectService(project)
    service.link(branch, "dev", game)
    return service, game


@pytest.mark.parametrize("ancestor", [False, True])
def test_payload_edit_invalidates_without_instance_change(
    layer_factory, tmp_path: Path, ancestor: bool
) -> None:
    source = layer_factory("base", payload={"nested/config.txt": "same"})
    layer_factory("client", parents=["../base"])
    service, game = linked_service(tmp_path, "client" if ancestor else "base")
    (game / "nested").mkdir()
    actual = game / "nested/config.txt"
    actual.write_text("same")
    initial = service.explorer_snapshot("dev")
    original_stat = actual.stat()
    assert initial.entries["nested/config.txt"]["status"] == "unchanged"

    write_changed(source / "nested/config.txt", "diff")

    refreshed = service.explorer_snapshot("dev")
    current_stat = actual.stat()
    assert (current_stat.st_mtime_ns, current_stat.st_size, current_stat.st_ino) == (
        original_stat.st_mtime_ns,
        original_stat.st_size,
        original_stat.st_ino,
    )
    assert refreshed is not initial
    assert refreshed.entries["nested/config.txt"]["status"] == "change"
    assert refreshed.delta_items == ProjectService(tmp_path).explorer_snapshot("dev").delta_items


@pytest.mark.parametrize("mutation", ["add", "delete"])
def test_nested_payload_membership_invalidates(
    layer_factory, tmp_path: Path, mutation: str
) -> None:
    source = layer_factory("client", payload={"nested/existing.txt": "same"})
    service, game = linked_service(tmp_path)
    (game / "nested").mkdir()
    (game / "nested/existing.txt").write_text("same")
    initial = service.explorer_snapshot("dev")

    if mutation == "add":
        (source / "nested/new.txt").write_text("wanted")
        path, status = "nested/new.txt", "remove"
    else:
        (source / "nested/existing.txt").unlink()
        path, status = "nested/existing.txt", "add"

    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not initial
    assert refreshed.entries[path]["status"] == status


@pytest.mark.parametrize("mutation", ["edit", "add", "delete"])
def test_ancestor_metadata_changes_invalidate(
    layer_factory, tmp_path: Path, mutation: str
) -> None:
    ignore = {"type": "ignore", "selector": {"kind": "exact", "path": "extra.txt"}}
    source = layer_factory("base", operations=[ignore] if mutation == "delete" else [])
    layer_factory("client", parents=["../base"])
    service, game = linked_service(tmp_path)
    (game / "extra.txt").write_text("extra")
    initial = service.explorer_snapshot("dev")

    if mutation == "delete":
        (source / ".pack/operations.json").unlink()
    else:
        name = "operations.json" if mutation == "edit" else "new.json"
        write_changed(source / ".pack" / name, json.dumps({"operations": [ignore]}))

    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not initial
    assert refreshed.entries["extra.txt"]["ignored"] is (mutation != "delete")


def test_parent_marker_switch_replaces_watched_ancestors(layer_factory, tmp_path: Path) -> None:
    layer_factory("old", payload={"config.txt": "same"})
    replacement = layer_factory("new", payload={"config.txt": "next"})
    source = layer_factory("client", parents=["../old"])
    service, game = linked_service(tmp_path)
    (game / "config.txt").write_text("same")
    initial = service.explorer_snapshot("dev")

    write_changed(
        source / ".pack/layer.json",
        json.dumps({"schema": 1, "id": "client", "parents": ["../new"]}),
    )
    switched = service.explorer_snapshot("dev")
    assert switched is not initial
    assert switched.entries["config.txt"]["status"] == "change"

    write_changed(replacement / "config.txt", "same")
    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not switched
    assert refreshed.entries["config.txt"]["status"] == "unchanged"


def test_metadata_directory_symlink_watches_new_json(layer_factory, tmp_path: Path) -> None:
    source = layer_factory("client", operations=[])
    metadata = tmp_path / "shared-metadata"
    (source / ".pack").rename(metadata)
    try:
        (source / ".pack").symlink_to(metadata, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 or isinstance(exc, PermissionError):
            pytest.skip("creating directory symlinks requires permission")
        raise
    service, game = linked_service(tmp_path)
    (game / "extra.txt").write_text("extra")
    initial = service.explorer_snapshot("dev")

    write_changed(
        metadata / "new.json",
        json.dumps({"operations": [{"type": "ignore", "glob": "extra.txt"}]}),
    )

    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not initial
    assert refreshed.entries["extra.txt"]["ignored"] is True


def test_external_link_change_invalidates_source_roots(layer_factory, tmp_path: Path) -> None:
    layer_factory("client", payload={"config.txt": "same"})
    layer_factory("other", payload={"config.txt": "different"})
    service, game = linked_service(tmp_path)
    (game / "config.txt").write_text("same")
    initial = service.explorer_snapshot("dev")

    external = ProjectService(tmp_path)
    external.unlink("dev")
    external.link("other", "dev")

    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not initial
    assert refreshed.entries["config.txt"]["status"] == "change"


def test_unaffected_warm_snapshot_needs_only_saved_stats(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = layer_factory("client", payload={"nested/a.txt": "a", "nested/b.txt": "b"})
    other = layer_factory("unrelated", payload={"other.txt": "before"})
    internal = source / ".pack/artifacts"
    internal.mkdir()
    (internal / "unused.bin").write_bytes(b"before")
    service, _ = linked_service(tmp_path)
    initial = service.explorer_snapshot("dev")
    write_changed(other / "other.txt", "after")
    write_changed(internal / "unused.bin", "after")
    (internal / "new.bin").write_bytes(b"irrelevant")

    def fail(*args: object, **kwargs: object) -> Any:
        raise AssertionError("warm snapshot must not resolve, enumerate, or read payload")

    monkeypatch.setattr(service, "instance_context", fail)
    monkeypatch.setattr(project_module, "scan_instance", fail)
    monkeypatch.setattr(Path, "rglob", fail)
    monkeypatch.setattr(Path, "read_bytes", fail)
    monkeypatch.setattr(os, "scandir", fail)
    assert service.explorer_snapshot("dev") is initial
    first = initial.page("nested", mode="all", show_ignored=False, cursor=0, limit=1)
    second = initial.page("nested", mode="all", show_ignored=False, cursor=1, limit=1)
    assert first["next_cursor"] == 1
    assert second["next_cursor"] is None
    assert [first["items"][0]["path"], second["items"][0]["path"]] == [
        "nested/a.txt",
        "nested/b.txt",
    ]


def test_auto_payload_snapshot_watches_new_source_files(layer_factory, tmp_path: Path) -> None:
    source = layer_factory("client", operations=[])
    service, game = linked_service(tmp_path)
    (game / "nested").mkdir()
    (game / "nested/config.txt").write_text("same")
    service.explorer_snapshot("dev")

    result = service.capture_explorer_path("dev", "nested", "client", auto_payload=True)
    assert result is not None and result["applied"] is True
    retained = service._explorer_snapshots["dev"]
    assert retained.context_verified is False
    assert service.explorer_snapshot("dev") is retained

    write_changed(source / "nested/config.txt", "diff")

    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not retained
    assert refreshed.entries["nested/config.txt"]["status"] == "change"


def test_ignore_reprojection_refreshes_source_generations(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    service, game = linked_service(tmp_path)
    (game / "extra.txt").write_text("extra")
    service.explorer_snapshot("dev")

    def fail(*args: object, **kwargs: object) -> Any:
        raise AssertionError("ignore reprojection must preserve the warm instance scan")

    monkeypatch.setattr(project_module, "scan_instance", fail)
    service.add_ignore("dev", "extra.txt", "exact", "client")
    ignored = service._explorer_snapshots["dev"]
    assert service.explorer_snapshot("dev") is ignored
    identity = service.ignore_rules("dev", "client")[0]["identity"]
    service.remove_ignore("dev", identity, "client")
    restored = service._explorer_snapshots["dev"]
    assert service.explorer_snapshot("dev") is restored
    assert restored.entries["extra.txt"]["ignored"] is False


@pytest.mark.parametrize("remove", [False, True])
def test_ignore_reprojection_does_not_bless_stale_source(
    layer_factory, tmp_path: Path, remove: bool
) -> None:
    source = layer_factory("client", operations=[], payload={"config.txt": "same"})
    service, game = linked_service(tmp_path)
    (game / "config.txt").write_text("same")
    (game / "extra.txt").write_text("extra")
    if remove:
        service.add_ignore("dev", "extra.txt", "exact", "client")
    initial = service.explorer_snapshot("dev")
    write_changed(source / "config.txt", "diff")

    if remove:
        identity = service.ignore_rules("dev", "client")[0]["identity"]
        service.remove_ignore("dev", identity, "client")
    else:
        service.add_ignore("dev", "extra.txt", "exact", "client")

    refreshed = service.explorer_snapshot("dev")
    assert refreshed is not initial
    assert refreshed.entries["config.txt"]["status"] == "change"
    assert refreshed.entries["extra.txt"]["ignored"] is not remove
