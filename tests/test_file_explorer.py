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
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.model import IgnoreSelector, SelectorKind
from mcframework.operations import AddIgnoreSelector, operation_from_json, operation_to_json
from mcframework.project import ProjectService
from mcframework.web_process import start_web, stop_web


def game_path(project: Path, suffix: str = "game") -> Path:
    return project.parent / f"{project.name}-{suffix}"


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


def test_rename_is_deduplicated_across_folder_aggregates(layer_factory, tmp_path: Path) -> None:
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
        snapshot.entries["old/name.txt"]["delta_id"] == snapshot.entries["new/name.txt"]["delta_id"]
    )


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
    original = service.capture_delta
    calls = 0

    def fail_after_first(instance: str, delta_id: str, target_layer: str | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MetadataError("injected batch failure")
        original(instance, delta_id, target_layer)

    monkeypatch.setattr(service, "capture_delta", fail_after_first)
    with pytest.raises(MetadataError, match="injected"):
        service.capture_explorer_path("dev", "dir")
    assert not overlay.exists()
    assert not features_root.exists()
    assert service.state.links_path.read_bytes() == links_before
    assert len(service.instance_diff(service.instance_context("dev"))["items"]) == 3

    monkeypatch.setattr(service, "capture_delta", original)
    service.capture_explorer_path("dev", "dir")
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

    with pytest.raises(MetadataError, match="provenance-aware"):
        service.capture_explorer_path("dev", "config/a.txt", "client")
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
    with pytest.raises(MetadataError, match="provenance-aware"):
        service.capture_explorer_path("dev", "config/a.txt", "base")
    assert metadata.read_bytes() == metadata_before
    assert not (tmp_path / "base/config/a.txt").exists()

    later_plan = service.explorer_plan("dev", "config/a.txt", "client")
    assert later_plan["items"][0]["supported"] is True
    service.capture_explorer_path("dev", "config/a.txt", "client")
    assert (tmp_path / "client/config/a.txt").read_text() == "new"
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


def test_same_layer_package_declaration_is_rejected_before_artifact_write(
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

    with pytest.raises(MetadataError, match="provenance-aware declaration"):
        service.capture_explorer_path(
            "dev", item["path"], "client", {item["id"]: {"mode": "package"}}
        )
    assert not (tmp_path / "client/.pack/artifacts").exists()


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
    with pytest.raises(MetadataError, match="explicit"):
        service.capture_explorer_path("dev", "mods/extra.jar")
    service.capture_explorer_path(
        "dev", "mods/extra.jar", decisions={item["id"]: {"mode": "payload"}}
    )
    assert service.instance_diff(service.instance_context("dev"))["items"] == []


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

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise MetadataError("injected after artifact")

    monkeypatch.setattr(service, "capture_delta", fail)
    with pytest.raises(MetadataError, match="after artifact"):
        service.capture_explorer_path(
            "dev", "batch", decisions={jar_item["id"]: {"mode": "package"}}
        )
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
