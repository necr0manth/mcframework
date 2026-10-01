from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.model import IgnoreSelector, SelectorKind
from mcframework.operations import AddIgnoreSelector, operation_from_json, operation_to_json
from mcframework.project import ProjectService
from mcframework.transport import LocalFilesystemInstance
from mcframework.web_process import start_web, stop_web


def game_path(project: Path, suffix: str = "game") -> Path:
    return project.parent / f"{project.name}-{suffix}"


def planned_decisions(
    service: ProjectService,
    path: str,
    modes: dict[str, str] | None = None,
    target_layer: str | None = None,
) -> dict[str, dict[str, object]]:
    plan = service.explorer_plan("dev", path, target_layer)
    decisions: dict[str, dict[str, object]] = {}
    for item in plan["items"]:
        item_id = str(item["id"])
        available = [str(value) for value in item["capture_modes"]]
        decision: dict[str, object] = {
            "mode": (modes or {}).get(item_id, available[0]),
        }
        if "expected_sha256" in item:
            decision["expected_sha256"] = item["expected_sha256"]
        else:
            decision["expected_absent"] = True
        decisions[item_id] = decision
    return decisions


def test_ignore_selectors_preserve_legacy_globs_and_literal_special_characters() -> None:
    legacy = operation_from_json({"type": "ignore", "glob": "logs/**"}, source="test", index=0)
    assert isinstance(legacy, AddIgnoreSelector)
    assert legacy.selector.kind is SelectorKind.GLOB
    assert legacy.selector.contains("logs/deep/latest.log")
    assert operation_to_json(legacy) == {"type": "ignore", "glob": "logs/**"}

    exact = IgnoreSelector(SelectorKind.EXACT, "mods/a*?[b].jar")
    subtree = IgnoreSelector(SelectorKind.SUBTREE, "config/[profile]")
    assert exact.contains("mods/a*?[b].jar")
    assert not exact.contains("mods/anything.jar")
    assert subtree.contains("config/[profile]")
    assert subtree.contains("config/[profile]/nested/file")
    assert not subtree.contains("config/[profile]-copy/file")


@pytest.mark.parametrize("operation, ignored", [
    ({"type": "ignore", "selector": {"kind": "subtree", "path": "mods/.connector"}},
     "mods/.connector/nested/generated.jar"),
    ({"type": "ignore", "selector": {"kind": "exact", "path": "mods/generated.jar"}},
     "mods/generated.jar"),
    ({"type": "ignore", "glob": "mods/cache/**"}, "mods/cache/nested/generated.jar"),
])
def test_folder_plan_skips_ignored_packages_before_reading(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    operation: dict[str, object], ignored: str,
) -> None:
    layer_factory("client", operations=[operation])
    game = game_path(tmp_path)
    ignored_file = game / ignored
    ignored_file.parent.mkdir(parents=True)
    ignored_file.write_bytes(b"must not inspect")
    (game / "mods/keep.txt").write_text("keep")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    snapshot = service.explorer_snapshot("dev")
    assert snapshot.entries[ignored]["ignored"]
    original_read = LocalFilesystemInstance.read

    def read(transport, path: str):
        assert path != ignored, "ignored package must not be read or enriched"
        return original_read(transport, path)

    monkeypatch.setattr("mcframework.project.LocalFilesystemInstance.read", read)
    plan = service.explorer_plan("dev", "mods")
    assert [item["path"] for item in plan["items"]] == ["mods/keep.txt"]
    assert plan["unsupported"] == []
    service.capture_explorer_path("dev", "mods", decisions=planned_decisions(service, "mods"))
    assert ignored_file.read_bytes() == b"must not inspect"
    assert service.instance_diff(service.instance_context("dev"))["items"] == []
    assert not any(
        path.name == "generated.jar" for path in service.state.feature_root("dev").rglob("*")
    )


@pytest.mark.parametrize("ignored", ["mods/old.txt", "mods/new.txt"])
def test_folder_plan_skips_rename_with_either_side_ignored(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignored: str,
) -> None:
    layer_factory("client", operations=[
        {"type": "ignore", "selector": {"kind": "exact", "path": ignored}},
    ], payload={"mods/old.txt": "same"})
    game = game_path(tmp_path)
    (game / "mods").mkdir(parents=True)
    (game / "mods/new.txt").write_text("same")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    snapshot = service.explorer_snapshot("dev")
    # Fast snapshots currently split rename sides; exercise a rename delta explicitly.
    rename = dict(snapshot.delta_items[0], path="mods/old.txt", to_path="mods/new.txt",
                  status="rename")
    monkeypatch.setattr(service, "explorer_snapshot", lambda *args, **kwargs: replace(
        snapshot, delta_items=(rename,), context_verified=True,
    ))
    assert service.explorer_plan("dev", "mods")["items"] == []


def test_folder_plan_ignores_unsupported_descendants(layer_factory, tmp_path: Path) -> None:
    layer_factory("client", operations=[
        {"type": "ignore", "selector": {"kind": "subtree", "path": "config/blocked"}},
    ], payload={"config/blocked/file.txt": "wanted"})
    game = game_path(tmp_path)
    (game / "config").mkdir(parents=True)
    (game / "config/blocked").write_text("conflict")
    (game / "config/keep.txt").write_text("keep")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "config")
    assert plan["unsupported"] == []
    assert [item["path"] for item in plan["items"]] == ["config/keep.txt"]
    service.capture_explorer_path("dev", "config", decisions=planned_decisions(service, "config"))
    assert (game / "config/blocked").read_text() == "conflict"
    assert not (service.state.feature_root("dev") / "config/blocked").exists()
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


def test_explorer_unions_real_tree_ghost_deletions_empty_dirs_and_ignored_status(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "client",
        operations=[
            {
                "type": "ignore",
                "selector": {"kind": "exact", "path": "ignored.txt"},
            }
        ],
        payload={"deep/deleted.txt": "wanted", "same.txt": "same"},
    )
    game = game_path(tmp_path)
    (game / "empty").mkdir(parents=True)
    (game / "same.txt").write_text("same")
    (game / "ignored.txt").write_text("extra")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    snapshot = service.explorer_snapshot("dev")
    assert snapshot.entries["empty"]["type"] == "directory"
    assert snapshot.entries["deep/deleted.txt"]["synthetic"] is True
    assert snapshot.entries["deep/deleted.txt"]["status"] == "remove"
    assert snapshot.entries["deep"]["changed_count"] == 1
    assert snapshot.entries["ignored.txt"]["ignored"] is True
    assert snapshot.entries["ignored.txt"]["status"] == "add"
    assert service.instance_diff(service.instance_context("dev"))["items"] == [
        next(
            item
            for item in service.instance_diff(
                service.instance_context("dev"), respect_ignore=False
            )["items"]
            if item["path"] == "deep/deleted.txt"
        )
    ]

    hidden = service.explorer_page("dev", show_ignored=False)
    visible = service.explorer_page("dev", show_ignored=True)
    assert "ignored.txt" not in {item["path"] for item in hidden["items"]}
    assert "ignored.txt" in {item["path"] for item in visible["items"]}
    changed_only = service.explorer_page("dev", mode="changed-only", show_ignored=True)
    assert "same.txt" not in {item["path"] for item in changed_only["items"]}


@pytest.mark.parametrize("with_actual_file", [False, True])
def test_desired_only_snapshot_entry_is_actionable_for_empty_and_nonempty_scans(
    layer_factory, tmp_path: Path, with_actual_file: bool
) -> None:
    layer_factory("client", operations=[], payload={"ghost.txt": "wanted"})
    game = game_path(tmp_path)
    game.mkdir()
    if with_actual_file:
        (game / "unrelated.txt").write_text("actual")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    ghost = service.explorer_snapshot("dev").entries["ghost.txt"]
    assert ghost["synthetic"] is True
    assert ghost["status"] == "remove"
    assert ghost["actionable"] is True


def test_warm_snapshot_invalidates_for_nested_add_delete_and_desired_file_change(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[], payload={"nested/desired.txt": "same"})
    game = game_path(tmp_path)
    nested = game / "nested"
    nested.mkdir(parents=True)
    desired = nested / "desired.txt"
    desired.write_text("same")
    removed = nested / "removed.txt"
    removed.write_text("remove me")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    initial = service.explorer_snapshot("dev")
    assert initial.entries["nested/desired.txt"]["changed"] is False
    added = nested / "new.txt"
    added.write_text("new")
    after_add = service.explorer_snapshot("dev")
    assert "nested/new.txt" in after_add.entries

    removed.unlink()
    after_delete = service.explorer_snapshot("dev")
    assert "nested/removed.txt" not in after_delete.entries

    previous_mtime = desired.stat().st_mtime_ns
    desired.write_text("diff")
    os.utime(desired, ns=(previous_mtime + 1_000_000_000,) * 2)
    after_change = service.explorer_snapshot("dev")
    assert after_change.entries["nested/desired.txt"]["status"] == "change"


def test_explorer_symlink_and_type_conflict_are_visible_but_not_actionable(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[], payload={"blocked/file.txt": "wanted"})
    game = game_path(tmp_path)
    game.mkdir()
    (game / "blocked").write_text("not a directory")
    link = game / "visible-link"
    try:
        os.symlink(game / "blocked", link)
    except OSError:
        pytest.skip("creating symlinks is unavailable on this Windows host")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    snapshot = service.explorer_snapshot("dev")
    assert snapshot.entries["visible-link"]["type"] == "symlink"
    assert snapshot.entries["visible-link"]["actionable"] is False
    assert snapshot.entries["blocked"]["status"] == "type-conflict"
    assert snapshot.entries["blocked/file.txt"]["actionable"] is False
    with pytest.raises(MetadataError, match="blocked"):
        service.capture_explorer_path("dev", "blocked")


def test_fast_explorer_keeps_rename_sides_separate_without_hashing(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[], payload={"old/name.txt": "same"})
    game = game_path(tmp_path)
    (game / "new").mkdir(parents=True)
    (game / "new/name.txt").write_text("same")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    snapshot = service.explorer_snapshot("dev")
    assert snapshot.entries["old"]["changed_count"] == 1
    assert snapshot.entries["new"]["changed_count"] == 1
    assert (
        snapshot.entries["old/name.txt"]["delta_id"] != snapshot.entries["new/name.txt"]["delta_id"]
    )
    assert service.instance_diff(service.instance_context("dev"))["summary"]["rename"] == 1


def test_explorer_snapshot_does_not_enrich_actual_only_files(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "mods").mkdir(parents=True)
    (game / "mods/extra.jar").write_bytes(b"not inspected")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("actual-only enrichment must stay lazy")

    monkeypatch.setattr("mcframework.project.inspect_jar", fail)
    monkeypatch.setattr("mcframework.project.LocalFilesystemInstance.read", fail)
    monkeypatch.setattr(service, "_provider_candidates", fail)

    entry = service.explorer_snapshot("dev").entries["mods/extra.jar"]
    assert entry["status"] == "add"
    assert entry["capture_modes"] == ["package", "payload"]


def test_ignore_inspector_removes_only_selected_layer_rule(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[{"type": "ignore", "glob": "logs/**"}])
    layer_factory(
        "client",
        parents=[{"path": "../base"}],
        operations=[],
    )
    game = game_path(tmp_path)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    service.add_ignore("dev", "config/*?[x]", "exact", "client")

    rules = service.ignore_rules("dev", "client")
    inherited = next(item for item in rules if item["selector"]["kind"] == "glob")
    own = next(item for item in rules if item["selector"]["kind"] == "exact")
    assert inherited["removable"] is False
    assert own["removable"] is True
    with pytest.raises(MetadataError, match="inherited"):
        service.remove_ignore("dev", inherited["identity"], "client")
    service.remove_ignore("dev", own["identity"], "client")
    assert all(
        item["selector"]["kind"] != "exact" for item in service.ignore_rules("dev", "client")
    )


def test_folder_capture_is_all_or_nothing_and_rolls_back_created_overlay(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory(
        "client",
        operations=[],
        payload={"dir/change.txt": "old", "dir/deleted.txt": "wanted"},
    )
    game = game_path(tmp_path)
    (game / "dir").mkdir(parents=True)
    (game / "dir/change.txt").write_text("new")
    (game / "dir/extra.txt").write_text("extra")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    overlay = service.state.feature_root("dev")
    features_root = service.state.root / "features"
    assert not features_root.exists()
    links_before = service.state.links_path.read_bytes()
    decisions = planned_decisions(service, "dir")
    original_write = service._batch_atomic_write

    def fail_after_first(path: Path, content: bytes) -> None:
        if path.name == "extra.txt":
            raise MetadataError("injected batch failure")
        original_write(path, content)

    monkeypatch.setattr(service, "_batch_atomic_write", fail_after_first)
    with pytest.raises(MetadataError, match="injected"):
        service.capture_explorer_path("dev", "dir", decisions=decisions)
    assert not overlay.exists()
    assert not features_root.exists()
    assert service.state.links_path.read_bytes() == links_before
    assert len(service.instance_diff(service.instance_context("dev"))["items"]) == 3

    monkeypatch.setattr(service, "_batch_atomic_write", original_write)
    decisions = planned_decisions(service, "dir")
    service.capture_explorer_path("dev", "dir", decisions=decisions)
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


def test_same_layer_structured_write_is_rejected_before_mutation(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    edits = tmp_path / "client/.pack/local-edits.json"
    edits.write_text(
        json.dumps({"operations": [{"type": "put", "path": "config/a.txt", "content": "old"}]})
    )
    game = game_path(tmp_path)
    (game / "config").mkdir(parents=True)
    (game / "config/a.txt").write_text("new")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    before = edits.read_bytes()
    decisions = planned_decisions(service, "config/a.txt", target_layer="client")

    with pytest.raises(MetadataError, match="provenance-aware"):
        service.capture_explorer_path("dev", "config/a.txt", "client", decisions)
    assert edits.read_bytes() == before
    assert not (tmp_path / "client/config/a.txt").exists()


def test_plan_checks_all_target_metadata_and_later_layer_remains_supported(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "base",
        operations=[{"type": "put", "path": "config/a.txt", "content": "old"}],
    )
    layer_factory("client", parents=[{"path": "../base"}], operations=[])
    game = game_path(tmp_path)
    (game / "config").mkdir(parents=True)
    (game / "config/a.txt").write_text("new")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    metadata = tmp_path / "base/.pack/operations.json"
    metadata_before = metadata.read_bytes()

    base_plan = service.explorer_plan("dev", "config/a.txt", "base")
    assert base_plan["items"][0]["supported"] is False
    assert "operations.json" in base_plan["items"][0]["reason"]
    base_decisions = planned_decisions(service, "config/a.txt", target_layer="base")
    with pytest.raises(MetadataError, match="provenance-aware"):
        service.capture_explorer_path("dev", "config/a.txt", "base", base_decisions)
    assert metadata.read_bytes() == metadata_before
    assert not (tmp_path / "base/config/a.txt").exists()

    later_plan = service.explorer_plan("dev", "config/a.txt", "client")
    assert later_plan["items"][0]["supported"] is True
    later_decisions = planned_decisions(service, "config/a.txt", target_layer="client")
    service.capture_explorer_path("dev", "config/a.txt", "client", later_decisions)
    assert (tmp_path / "client/config/a.txt").read_text() == "new"
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


def test_same_layer_check_decodes_local_paths_in_nested_schema_two_metadata(
    layer_factory, tmp_path: Path
) -> None:
    root = layer_factory("client", operations=[])
    marker = root / ".pack/layer.json"
    marker.write_text('{"schema": 2, "id": "client", "parents": []}')
    nested = root / "config/.pack/files.json"
    nested.parent.mkdir(parents=True)
    nested.write_text(
        json.dumps(
            {"operations": [{"type": "put", "path": "../a.txt", "content": "old"}]}
        )
    )
    game = game_path(tmp_path)
    (game / "config").mkdir(parents=True)
    (game / "config/a.txt").write_text("new")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    plan = service.explorer_plan("dev", "config/a.txt", "client")
    assert plan["items"][0]["supported"] is False
    assert "files.json" in plan["items"][0]["reason"]


def test_same_layer_package_capture_updates_its_provenance_declaration(
    layer_factory, tmp_path: Path
) -> None:
    desired = tmp_path / "desired.jar"
    desired.write_bytes(b"desired")
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "activation": "enabled",
                "destination": "mods/mod.jar",
                "source": {
                    "provider": "local",
                    "location": desired.name,
                    "sha256": hashlib.sha256(b"desired").hexdigest(),
                },
            }
        ],
    )
    game = game_path(tmp_path)
    (game / "mods").mkdir(parents=True)
    (game / "mods/mod.jar").write_bytes(b"actual")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    item = service.instance_diff(service.instance_context("dev"))["items"][0]
    decisions = planned_decisions(service, item["path"], {str(item["id"]): "package"}, "client")

    service.capture_explorer_path("dev", item["path"], "client", decisions)

    document = json.loads((tmp_path / "client/.pack/operations.json").read_text())
    declaration = document["operations"][0]
    assert declaration["id"] == "mod"
    assert declaration["source"]["sha256"] == hashlib.sha256(b"actual").hexdigest()
    assert declaration["source"]["location"].startswith("client/.pack/artifacts/")
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


def test_shared_target_layer_lock_serializes_separate_service_instances(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    game.mkdir()
    ProjectService(tmp_path).link("client", "dev", game)

    def add(path: str) -> None:
        ProjectService(tmp_path).add_ignore("dev", path, "exact", "client")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(add, ("one.txt", "two.txt")))
    operations = json.loads(
        (tmp_path / "client/.pack/local-edits.json").read_text(encoding="utf-8")
    )["operations"]
    assert {item["selector"]["path"] for item in operations} == {"one.txt", "two.txt"}


def test_jar_capture_requires_explicit_choice(layer_factory, tmp_path: Path) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "mods").mkdir(parents=True)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"extra","version":"1"}')
    (game / "mods/extra.jar").write_bytes(content.getvalue())
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    item = service.instance_diff(service.instance_context("dev"))["items"][0]
    assert item["capture_modes"] == ["package", "payload"]
    with pytest.raises(MetadataError, match="decisions"):
        service.capture_explorer_path("dev", "mods/extra.jar")
    decisions = planned_decisions(service, "mods/extra.jar", {str(item["id"]): "payload"})
    service.capture_explorer_path("dev", "mods/extra.jar", decisions=decisions)
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


def test_folder_package_capture_preserves_disabled_suffix_semantics(
    layer_factory, tmp_path: Path
) -> None:
    enabled = io.BytesIO()
    disabled = io.BytesIO()
    old_disabled = io.BytesIO()
    with zipfile.ZipFile(enabled, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"enabled","version":"1"}')
    with zipfile.ZipFile(disabled, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"inactive","version":"1"}')
    with zipfile.ZipFile(old_disabled, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"inactive","version":"0"}')
    layer_factory(
        "client",
        operations=[],
        payload={"mods/inactive.jar.disabled": old_disabled.getvalue()},
    )
    game = game_path(tmp_path)
    (game / "mods").mkdir(parents=True)
    (game / "mods/enabled.jar").write_bytes(enabled.getvalue())
    (game / "mods/inactive.jar.disabled").write_bytes(disabled.getvalue())
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "mods")
    modes = {str(item["id"]): "package" for item in plan["items"]}

    service.capture_explorer_path(
        "dev", "mods", decisions=planned_decisions(service, "mods", modes)
    )

    operations = json.loads(
        (tmp_path / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"]
    packages = {item["destination"]: item for item in operations if item["type"] == "package"}
    assert {item["path"] for item in operations if item["type"] == "remove"} == {
        "mods/inactive.jar.disabled"
    }
    assert packages["mods/enabled.jar"]["activation"] == "enabled"
    assert packages["mods/inactive.jar"]["activation"] == "disabled"
    context = service.instance_context("dev")
    assert "mods/enabled.jar" in context.files
    assert "mods/inactive.jar" not in context.files
    assert (game / "mods/enabled.jar").read_bytes() == enabled.getvalue()
    assert (game / "mods/inactive.jar.disabled").read_bytes() == disabled.getvalue()
    assert service.instance_diff(context)["items"] == []


def test_folder_capture_requires_exact_plan_preconditions_before_writes(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    game.mkdir()
    (game / "extra.txt").write_text("extra")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "extra.txt")
    item = plan["items"][0]

    invalid = [
        None,
        {},
        {item["id"]: {"mode": "payload"}},
        {
            item["id"]: {
                "mode": "payload",
                "expected_sha256": item["expected_sha256"],
                "expected_absent": True,
            }
        },
    ]
    for decisions in invalid:
        with pytest.raises(MetadataError, match="decisions|exactly one"):
            service.capture_explorer_path("dev", "extra.txt", decisions=decisions)
        assert not service.state.feature_root("dev").exists()


def test_package_artifact_and_metadata_roll_back_on_later_batch_failure(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "batch").mkdir(parents=True)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"extra","version":"1"}')
    (game / "batch/a.jar").write_bytes(content.getvalue())
    (game / "batch/z.txt").write_text("later")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    jar_item = next(
        item
        for item in service.instance_diff(service.instance_context("dev"))["items"]
        if item["path"].endswith(".jar")
    )
    links_before = service.state.links_path.read_bytes()

    decisions = planned_decisions(service, "batch", {str(jar_item["id"]): "package"})

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise MetadataError("injected after artifact")

    monkeypatch.setattr(service, "_append_operations", fail)
    with pytest.raises(MetadataError, match="after artifact"):
        service.capture_explorer_path("dev", "batch", decisions=decisions)
    assert not service.state.feature_root("dev").exists()
    assert service.state.links_path.read_bytes() == links_before


def test_file_api_preserves_auth_csrf_revision_and_invalidates_snapshot(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    game.mkdir()
    (game / "extra.txt").write_text("extra")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    url = start_web(tmp_path, "dev")
    base = url.split("?", 1)[0]
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        opener.open(url, timeout=3).read()
        bootstrap = json.loads(opener.open(base + "api/v1/bootstrap", timeout=3).read())
        page = json.loads(
            opener.open(
                base + "api/v1/files?" + urllib.parse.urlencode({"show_ignored": "false"}),
                timeout=3,
            ).read()
        )
        assert {item["path"] for item in page["items"]} == {"extra.txt"}

        plan = json.loads(
            opener.open(
                base + "api/v1/files/plan?" + urllib.parse.urlencode({"path": "extra.txt"}),
                timeout=3,
            ).read()
        )
        missing_decisions_body = json.dumps(
            {"path": "extra.txt", "target_layer": "client", "plan_revision": plan["revision"]}
        ).encode()
        missing_decisions = urllib.request.Request(
            base + "api/v1/files/capture",
            data=missing_decisions_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Mcpack-CSRF": bootstrap["csrf"],
                "If-Match": f'"{plan["revision"]}"',
                "Origin": base.rstrip("/"),
            },
        )
        with pytest.raises(urllib.error.HTTPError) as missing_decisions_response:
            opener.open(missing_decisions, timeout=3)
        assert missing_decisions_response.value.code == 400
        assert not (tmp_path / "client/extra.txt").exists()

        body = json.dumps({"path": "extra.txt", "kind": "exact", "target_layer": "client"}).encode()
        missing_origin = urllib.request.Request(
            base + "api/v1/files/ignore",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Mcpack-CSRF": bootstrap["csrf"],
                "If-Match": f'"{bootstrap["revision"]}"',
            },
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            opener.open(missing_origin, timeout=3)
        assert rejected.value.code == 400

        request = urllib.request.Request(
            base + "api/v1/files/ignore",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Mcpack-CSRF": bootstrap["csrf"],
                "If-Match": f'"{bootstrap["revision"]}"',
                "Origin": base.rstrip("/"),
            },
        )
        changed = json.loads(opener.open(request, timeout=3).read())
        assert changed["revision"] == bootstrap["revision"] + 1
        hidden = json.loads(opener.open(base + "api/v1/files", timeout=3).read())
        assert hidden["items"] == []
        visible = json.loads(opener.open(base + "api/v1/files?show_ignored=true", timeout=3).read())
        assert visible["items"][0]["ignored"] is True
    finally:
        stop_web(tmp_path, "dev")
