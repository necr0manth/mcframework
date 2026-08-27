from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcframework.cli import main
from mcframework.errors import MetadataError
from mcframework.local_state import LocalState
from mcframework.project import ProjectService
from mcframework.web_process import start_web, stop_web


def game_path(project: Path, name: str = "game") -> Path:
    return project.parent / f"{project.name}-{name}"


def test_init_layer_creates_only_minimal_marker_and_refuses_existing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["init", "client"]) == 0
    marker = tmp_path / "client/.pack/layer.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema": 1,
        "id": "client",
        "parents": [],
    }
    assert sorted(
        path.relative_to(tmp_path / "client").as_posix()
        for path in (tmp_path / "client").rglob("*")
        if path.is_file()
    ) == [".pack/layer.json"]
    assert ProjectService(tmp_path).branches() == ("client",)
    ProjectService(tmp_path).resolve_branch("client")

    assert main(["init", "client"]) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["init", "empty"]) == 0
    assert (empty / ".pack/layer.json").is_file()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    assert main(["init", "occupied"]) == 2
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert main(["init", "../escape"]) == 2


def test_project_root_cli_link_sync_and_split_state(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory(
        "client",
        operations=[
            {"type": "prune", "glob": "mods/*.jar"},
            {"type": "ignore", "glob": "logs/**"},
        ],
        payload={"config/value.txt": "wanted"},
    )
    instance = game_path(tmp_path)
    (instance / "mods").mkdir(parents=True)
    (instance / "mods/old.jar").write_bytes(b"old")
    (instance / "logs").mkdir()
    (instance / "logs/latest.log").write_bytes(b"runtime")
    monkeypatch.chdir(tmp_path)

    assert main(["link", "client", "dev", "-p", str(instance)]) == 0
    assert main(["sync", "dev"]) == 0

    assert (instance / "config/value.txt").read_text() == "wanted"
    assert not (instance / "mods/old.jar").exists()
    assert (instance / "logs/latest.log").exists()
    assert (tmp_path / ".mcpack/instances.json").is_file()
    assert (tmp_path / ".mcpack/links.json").is_file()
    assert (tmp_path / ".mcpack/parameters/dev.json").is_file()
    assert not (tmp_path / ".mcpack/state.json").exists()


def test_feature_overlay_and_capture_are_local_ordinary_layer(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[], payload={"config/base.txt": "base"})
    layer_factory("optional", operations=[], payload={"config/feature.txt": "feature"})
    instance = game_path(tmp_path)
    service = ProjectService(tmp_path)
    service.link("client", "dev", instance)

    link = service.add_feature("dev", "optional")
    assert link.effective_layer == ".mcpack/features/dev"
    context = service.instance_context("dev")
    assert context.features == ("optional",)
    assert context.files["config/base.txt"] == b"base"
    assert context.files["config/feature.txt"] == b"feature"

    (instance / "config").mkdir(parents=True)
    (instance / "config/base.txt").write_bytes(b"changed")
    changed = next(
        item
        for item in service.instance_diff(context)["items"]
        if item["status"] == "change"
    )
    service.capture_delta("dev", changed["id"])
    assert service.instance_context("dev").files["config/base.txt"] == b"changed"
    marker = json.loads((tmp_path / ".mcpack/features/dev/.pack/layer.json").read_text())
    assert marker["features"] == ["optional"]


def test_activation_edit_re_resolves_and_replaces_previous_value(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "activation": "available",
                "destination": "mods/mod.jar",
                "source": {"provider": "local", "location": "missing", "sha256": "0" * 64},
            }
        ],
    )
    service = ProjectService(tmp_path)
    service.link("client", "dev", game_path(tmp_path))
    service.set_activation("dev", "mod", "disabled")
    service.set_activation("dev", "mod", "available")
    operations = json.loads(
        (tmp_path / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"]
    assert operations == [{"type": "activation", "id": "mod", "state": "available"}]


def test_capture_changed_package_creates_verified_local_artifact(
    layer_factory, tmp_path: Path
) -> None:
    original = b"old jar"
    (tmp_path / "original.jar").write_bytes(original)
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
                    "location": "original.jar",
                    "sha256": hashlib.sha256(original).hexdigest(),
                },
            }
        ],
    )
    instance = game_path(tmp_path)
    (instance / "mods").mkdir(parents=True)
    (instance / "mods/mod.jar").write_bytes(b"changed jar")
    service = ProjectService(tmp_path)
    service.link("client", "dev", instance)
    context = service.instance_context("dev")
    changed = service.instance_diff(context)["items"][0]
    assert changed["package_id"] == "mod"

    service.capture_delta("dev", changed["id"])

    refreshed = service.instance_context("dev")
    assert service.instance_diff(refreshed)["items"] == []
    assert refreshed.files["mods/mod.jar"] == b"changed jar"
    declarations = json.loads(
        (tmp_path / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"]
    assert declarations[0]["source"]["provider"] == "local"


def test_instance_lifecycle_never_deletes_game_directory(layer_factory, tmp_path: Path) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    service = ProjectService(tmp_path)
    service.create_instance("old", game)
    service.link("client", "old")
    service.rename_instance("old", "new")
    service.unlink("new")
    service.delete_instance("new")
    assert game.is_dir()
    assert LocalState(tmp_path).instances() == {}


def test_link_rejects_different_path_and_build_rejects_protected_output(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    service = ProjectService(tmp_path)
    game = game_path(tmp_path)
    service.link("client", "dev", game)
    with pytest.raises(MetadataError, match="different -p"):
        service.link("client", "dev", game_path(tmp_path, "other"))
    assert service.safe_build_output("client", None) == (tmp_path / "build/client").resolve()
    with pytest.raises(MetadataError, match="intersects"):
        service.safe_build_output("client", tmp_path / "client/output")
    with pytest.raises(MetadataError, match="intersects"):
        service.safe_build_output("client", game / "subdir")


def test_cli_import_captures_instance_payload(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    instance = game_path(tmp_path)
    instance.mkdir()
    (instance / "options.txt").write_text("fov=90")
    service = ProjectService(tmp_path)
    service.link("client", "dev", instance)
    monkeypatch.chdir(tmp_path)

    assert main(["import", "dev"]) == 0

    assert service.instance_context("dev").files["options.txt"] == b"fov=90"


def test_web_background_singleton_bootstrap_and_idempotent_stop(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[], payload={"config/a": "one"})
    layer_factory("optional", operations=[], payload={"config/b": "two"})
    service = ProjectService(tmp_path)
    service.link("client", "dev", game_path(tmp_path))
    url = start_web(tmp_path, "dev")
    base = url.split("?", 1)[0]
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        with opener.open(url, timeout=3) as response:
            assert response.status == 200
        with opener.open(base + "api/v1/bootstrap", timeout=3) as response:
            bootstrap = json.loads(response.read())
        assert bootstrap["instance"]["name"] == "dev"
        assert bootstrap["link"]["branch"] == "client"
        running = LocalState(tmp_path).read_web("dev")
        assert running is not None
        assert "browser_capability" not in running

        replay = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        with pytest.raises(urllib.error.HTTPError) as replayed:
            replay.open(url, timeout=3)
        assert replayed.value.code == 401

        unauthorized = urllib.request.Request(
            base + "api/v1/features",
            data=b'{"feature":"optional"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(unauthorized, timeout=3)
        assert rejected.value.code == 400

        request = urllib.request.Request(
            base + "api/v1/features",
            data=b'{"feature":"optional"}',
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Mcpack-CSRF": bootstrap["csrf"],
                "If-Match": f'"{bootstrap["revision"]}"',
            },
        )
        with pytest.raises(urllib.error.HTTPError) as missing_origin:
            opener.open(request, timeout=3)
        assert missing_origin.value.code == 400

        request.add_header("Origin", base.rstrip("/"))
        with opener.open(request, timeout=3) as response:
            changed = json.loads(response.read())
        assert changed["link"]["features"] == ["optional"]
        assert changed["revision"] == bootstrap["revision"] + 1

        stale = urllib.request.Request(
            base + "api/v1/features",
            data=b'{"feature":"optional"}',
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Mcpack-CSRF": bootstrap["csrf"],
                "If-Match": f'"{bootstrap["revision"]}"',
                "Origin": base.rstrip("/"),
            },
        )
        with pytest.raises(urllib.error.HTTPError) as conflict:
            opener.open(stale, timeout=3)
        assert conflict.value.code == 409
        conflict_document = json.loads(conflict.value.read())
        assert conflict_document["error"]["code"] == "revision_conflict"
        assert ProjectService(tmp_path).instance_context("dev", prepare=False).features == (
            "optional",
        )
        running = LocalState(tmp_path).read_web("dev")
        assert running is not None and running["revision"] == changed["revision"]
        with pytest.raises(MetadataError, match="already running"):
            start_web(tmp_path, "dev")
    finally:
        stop_web(tmp_path, "dev")
    assert not stop_web(tmp_path, "dev")
