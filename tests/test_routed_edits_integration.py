from __future__ import annotations

import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mcframework.errors import MetadataError, ResolutionError, UnsafePathError
from mcframework.package_inspect import ProviderMatch, file_hashes
from mcframework.project import ProjectService


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def layer(project: Path, name: str, parents: tuple[str, ...] = (), schema: int = 2) -> Path:
    root = project / name
    write(root / ".pack/layer.json", {"schema": schema, "id": name, "parents": parents})
    return root


def package(identity: str = "magic") -> dict:
    return {
        "type": "package",
        "id": identity,
        "kind": "mod",
        "activation": "available",
        "path_mode": "layer",
        "destination": f"mods/{identity}.jar",
        "source": {"provider": "local", "location": "unused.jar", "sha256": "0" * 64},
    }


def service_for(tmp_path: Path, root: Path) -> ProjectService:
    service = ProjectService(tmp_path)
    service.link(root.name, "dev", tmp_path.parent / (tmp_path.name + "-game"))
    return service


def rule(identity: str, path: str, mode: str = "local", **extra: object) -> dict:
    return {
        "type": "routing_rule",
        "id": identity,
        "when": {"operation_types": ["ignore"]},
        "write": {"file": {"path": path, "path_mode": mode}},
        **extra,
    }


def test_nested_parent_rules_use_owner_relative_parent_and_declaration_paths(tmp_path: Path):
    parent = layer(tmp_path, "base")
    child = layer(tmp_path, "client", ("../base",))
    write(parent / "mods/.pack/rules.json", {"operations": [rule("ignore", "ignored.json")]})
    service = service_for(tmp_path, child)
    service.add_ignore("dev", "logs/a.txt", "exact", "client")
    destination = child / "mods/.pack/ignored.json"
    assert destination.is_file()
    assert not (child / ".pack/ignored.json").exists()
    own = next(item for item in service.ignore_rules("dev", "client") if item["own"])
    assert own["source"] == "mods/.pack/ignored.json"
    service.remove_ignore("dev", own["identity"], "client")
    assert json.loads(destination.read_text())["operations"] == []


def test_routed_write_rolls_back_after_post_write_resolution_failure(tmp_path: Path, monkeypatch):
    root = layer(tmp_path, "client")
    service = service_for(tmp_path, root)
    original = service.instance_context
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MetadataError("injected post-write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "instance_context", fail_second)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    with (
        pytest.raises(MetadataError, match="post-write"),
        service.metadata_destination("config/.pack/ignore.json"),
    ):
        service.add_ignore("dev", "logs/a.txt", "exact", "client")
    assert before == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert not (root / "config").exists()


def test_preview_is_pure_and_schema_one_rejects_unreadable_nested_destination(tmp_path: Path):
    root = layer(tmp_path, "client", schema=1)
    service = service_for(tmp_path, root)
    operation = {"type": "ignore", "selector": {"kind": "exact", "path": "logs/a.txt"}}
    before = tuple(root.rglob("*"))
    with pytest.raises(MetadataError, match="schema 2"):
        service.plan_metadata_writes(root, [operation], destination="mods/.pack/ignore.json")
    assert tuple(root.rglob("*")) == before
    preview = service.preview_routing("dev", operation)
    assert preview["writes"][0]["file"] == ".pack/local-edits.json"
    assert not service.state.feature_root("dev").exists()


@pytest.mark.parametrize("destination", [".pack/layer.json", "payload.json", "../out/.pack/x.json"])
def test_plan_rejects_control_payload_and_escaping_destinations(tmp_path: Path, destination: str):
    root = layer(tmp_path, "client")
    service = service_for(tmp_path, root)
    with pytest.raises((MetadataError, UnsafePathError)):
        service.plan_metadata_writes(
            root, [{"type": "ignore", "glob": "logs/**"}], destination=destination
        )


def test_same_layer_disable_preserves_declaration_source_then_remove_validates(tmp_path: Path):
    root = layer(tmp_path, "client")
    source = root / "mods/.pack/magic.json"
    write(source, {"operations": [package()]})
    service = service_for(tmp_path, root)
    service.remove_package("dev", "magic", target_layer="client")
    assert json.loads(source.read_text())["operations"][0]["activation"] == "disabled"
    assert not (root / "mods/.pack/disabled.json").exists()
    service.remove_package("dev", "magic", mode="remove", target_layer="client")
    assert json.loads(source.read_text())["operations"] == []
    assert not service.instance_context("dev").resolution.state.packages


def test_remove_declaration_with_dependency_rolls_back(tmp_path: Path):
    root = layer(tmp_path, "client")
    source = root / "mods/.pack/magic.json"
    write(
        source,
        {
            "operations": [
                package(),
                package("library"),
                {"type": "dependency", "source": "magic", "target": "library"},
            ]
        },
    )
    service = service_for(tmp_path, root)
    before = source.read_bytes()
    with pytest.raises(ResolutionError, match="missing"):
        service.remove_package("dev", "library", mode="remove", target_layer="client")
    assert source.read_bytes() == before


def test_cross_file_section_membership_is_one_declaration_and_existing_package_stays(
    tmp_path: Path,
):
    root = layer(tmp_path, "client")
    source = root / "mods/.pack/magic.json"
    sections = root / ".pack/sections.json"
    write(source, {"operations": [package()]})
    write(
        sections,
        {
            "operations": [
                {"type": "section", "id": "Mods", "title": "All mods", "members": ["magic"]}
            ]
        },
    )
    service = service_for(tmp_path, root)
    plan = service.plan_metadata_writes(
        root, [package("library")], destination="mods/.pack/libraries.json", section="Mods"
    )
    assert {item.path for item in plan.writes} == {sections, root / "mods/.pack/libraries.json"}
    service._apply_metadata_plan(plan, root)
    assert service.instance_context("dev").resolution.state.sections["Mods"].members == (
        "magic",
        "library",
    )
    plan = service.plan_metadata_writes(
        root, [dict(package(), activation="disabled")], destination="mods/.pack/elsewhere.json"
    )
    assert [item.path for item in plan.writes] == [source]


def test_renaming_instance_moves_rules_and_rule_saving_preserves_mixed_source(tmp_path: Path):
    root = layer(tmp_path, "client")
    source = root / "mods/.pack/rules.json"
    write(source, {"operations": [rule("ignore", "ignore.json"), package()]})
    service = service_for(tmp_path, root)
    service.save_routing_rule("dev", rule("ignore", "changed.json"), "layer", "client")
    assert (
        json.loads(source.read_text())["operations"][0]["write"]["file"]["path"] == "changed.json"
    )
    assert not (root / ".pack/routing.json").exists()
    service.save_routing_rule("dev", rule("ignore", "custom.json"), "instance")
    service.rename_instance("dev", "renamed")
    assert service.state.routing_rules("renamed")[0]["id"] == "ignore"
    assert not service.state.routing_rules_path("dev").exists()


def test_deleting_instance_removes_its_local_routing_rules(tmp_path: Path):
    root = layer(tmp_path, "client")
    service = service_for(tmp_path, root)
    service.save_routing_rule("dev", rule("ignore", "private.json"), "instance")

    service.delete_instance("dev")

    assert not service.state.routing_rules_path("dev").exists()


def test_all_metadata_edits_serialize_on_the_shared_physical_layer(tmp_path: Path):
    root = layer(tmp_path, "client")
    source = root / "mods/.pack/mods.json"
    write(source, {"operations": [package("one"), package("two")]})
    setup = ProjectService(tmp_path)
    setup.link("client", "first", tmp_path.parent / f"{tmp_path.name}-game-first")
    setup.link("client", "second", tmp_path.parent / f"{tmp_path.name}-game-second")

    def enable(instance_and_package: tuple[str, str]) -> None:
        instance, identity = instance_and_package
        ProjectService(tmp_path).set_activation(instance, identity, "disabled", "client")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tuple(pool.map(enable, (("first", "one"), ("second", "two"))))

    operations = json.loads(source.read_text(encoding="utf-8"))["operations"]
    assert {item["id"]: item["activation"] for item in operations} == {
        "one": "disabled",
        "two": "disabled",
    }


@pytest.mark.parametrize("entry", ["delta", "files"])
@pytest.mark.parametrize("instance_override", [False, True])
def test_provider_categories_route_real_imports_to_layer_or_instance_override(
    tmp_path: Path,
    entry: str,
    instance_override: bool,
):
    root = layer(tmp_path, "client")
    magic_rule = {
        "type": "routing_rule",
        "id": "magic",
        "when": {
            "operation_types": ["package"],
            "origin": ["instance_capture"],
            "package": {"provider": "modrinth", "categories": ["magic"], "kind": "mod"},
        },
        "write": {"file": {"path": "mods/.pack/magic.json", "path_mode": "layer"}},
    }
    write(root / ".pack/routing.json", {"operations": [magic_rule]})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"magic","version":"1"}')
    content = output.getvalue()

    class Matcher:
        def match(self, data: bytes) -> ProviderMatch:
            return ProviderMatch(
                "modrinth",
                "magic",
                "v1",
                "1",
                "https://example.invalid/magic.jar",
                file_hashes(data),
                kind="mod",
                categories=("magic",),
                tags=("magic",),
            )

    service = ProjectService(tmp_path, provider_matcher=Matcher())
    game = tmp_path.parent / (tmp_path.name + "-game")
    (game / "mods").mkdir(parents=True)
    (game / "mods/magic.jar").write_bytes(content)
    service.link("client", "dev", game)
    filename = "mods/.pack/magic.json"
    if instance_override:
        filename = "mods/.pack/private-magic.json"
        service.save_routing_rule(
            "dev",
            {**magic_rule, "write": {"file": {"path": filename, "path_mode": "layer"}}},
            "instance",
            "client",
        )
    if entry == "delta":
        item = service.instance_diff(service.instance_context("dev"), enrich_actual=False)["items"][
            0
        ]
        service.import_package(
            "dev",
            item["id"],
            "client",
            "modrinth",
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    else:
        item = service.explorer_plan("dev", "mods")["items"][0]
        service.capture_explorer_path(
            "dev",
            "mods",
            "client",
            {
                item["id"]: {
                    "mode": "package",
                    "provider_match": "modrinth",
                    "expected_sha256": item["expected_sha256"],
                },
            },
        )
    record = json.loads((root / filename).read_text())["operations"][0]
    assert record["type"] == "package"
    assert record["source"]["provider"] == "direct_url"
    assert not {"categories", "tags", "origin"}.intersection(record)
    assert service.instance_context("dev").files["mods/magic.jar"] == content


def test_per_operation_metadata_never_leaks_to_nonpackage_rules(tmp_path: Path):
    root = layer(tmp_path, "client")
    write(
        root / ".pack/routing.json",
        {
            "operations": [
                {
                    "type": "routing_rule",
                    "id": "magic",
                    "when": {"categories": ["magic"]},
                    "write": {"file": {"path": "mods/.pack/magic.json", "path_mode": "layer"}},
                }
            ]
        },
    )
    service = ProjectService(tmp_path)
    plan = service.plan_metadata_writes(
        root,
        [{"type": "ignore", "id": "magic", "glob": "logs/**"}],
        operation_contexts={"magic": {"package": {"categories": ["magic"]}}},
    )
    assert [item.path for item in plan.writes] == [root / ".pack/ignore.json"]


def test_explicit_automatic_dependency_origin_uses_dependency_default(tmp_path: Path):
    root = layer(tmp_path, "client")
    plan = ProjectService(tmp_path).plan_metadata_writes(
        root,
        [package()],
        origin="automatic_dependency",
    )
    assert [item.path for item in plan.writes] == [root / "mods/.pack/dependencies.json"]


@pytest.mark.parametrize("entry", ["delta", "files"])
def test_local_package_import_routes_with_the_same_inferred_kind_as_preview(
    tmp_path: Path, entry: str
):
    root = layer(tmp_path, "client")
    write(
        root / ".pack/routing.json",
        {
            "operations": [
                {
                    "type": "routing_rule",
                    "id": "captured-mods",
                    "when": {
                        "operation_types": ["package"],
                        "origin": ["instance_capture"],
                        "package": {"kind": ["mod"]},
                    },
                    "write": {
                        "file": {
                            "path": "mods/.pack/captured.json",
                            "path_mode": "layer",
                        }
                    },
                }
            ]
        },
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"local","version":"1"}')
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    (game / "mods/local.jar").write_bytes(output.getvalue())
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    if entry == "delta":
        item = service.instance_diff(
            service.instance_context("dev"), enrich_actual=False
        )["items"][0]
        service.import_package(
            "dev", item["id"], "client",
            expected_sha256=hashlib.sha256(output.getvalue()).hexdigest(),
        )
    else:
        item = service.explorer_plan("dev", "mods")["items"][0]
        service.capture_explorer_path(
            "dev",
            "mods",
            "client",
            {
                item["id"]: {
                    "mode": "package",
                    "expected_sha256": item["expected_sha256"],
                }
            },
        )

    operation = json.loads((root / "mods/.pack/captured.json").read_text())["operations"][0]
    assert operation["id"] == "local"
    assert operation["kind"] == "mod"


def test_package_import_preview_and_write_allocate_the_same_unique_id(tmp_path: Path):
    root = layer(tmp_path, "client")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"magic","version":"1"}')
    content = output.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    write(
        root / "mods/.pack/existing.json",
        {
            "operations": [
                {**package("magic"), "destination": "mods/existing-one.jar"},
                {
                    **package(f"magic-{digest[:8]}"),
                    "destination": "mods/existing-two.jar",
                },
            ]
        },
    )
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    (game / "mods/magic.jar").write_bytes(content)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    item = next(
        value
        for value in service.instance_diff(
            service.instance_context("dev"), enrich_actual=False
        )["items"]
        if value["path"] == "mods/magic.jar"
    )
    detail = service.delta_detail("dev", item["id"])

    assert detail["proposed_package_id"] == f"magic-{digest[:8]}-2"
    service.import_package("dev", item["id"], "client", expected_sha256=digest)

    imported = json.loads((root / "mods/.pack/mods.json").read_text())["operations"][0]
    assert imported["id"] == detail["proposed_package_id"]
    assert set(service.instance_context("dev").resolution.state.packages) == {
        "magic",
        f"magic-{digest[:8]}",
        f"magic-{digest[:8]}-2",
    }


def test_raw_jar_import_preview_includes_the_payload_removal(tmp_path: Path):
    root = layer(tmp_path, "client")
    raw = root / "mods/magic.jar"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"old payload")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"magic","version":"1"}')
    content = output.getvalue()
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    (game / "mods/magic.jar").write_bytes(content)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    item = service.instance_diff(service.instance_context("dev"), enrich_actual=False)["items"][0]
    detail = service.delta_detail("dev", item["id"])
    operation = {
        "type": "package",
        "id": detail["proposed_package_id"],
        "kind": "mod",
        "activation": "enabled",
        "destination": detail["path"],
        "source": {"provider": "local"},
    }

    preview = service.preview_routing(
        "dev", operation, "client", origin="instance_capture"
    )
    preview_files = {write["file"] for write in preview["writes"]}
    assert preview_files == {"mods/.pack/mods.json", "mods/.pack/remove.json"}

    service.import_package(
        "dev", item["id"], "client", expected_sha256=hashlib.sha256(content).hexdigest()
    )
    assert (root / "mods/.pack/mods.json").is_file()
    assert (root / "mods/.pack/remove.json").is_file()


def test_schema_upgrade_preserves_paths_and_policy_and_preview_is_pure(tmp_path: Path):
    root = layer(tmp_path, "client", schema=1)
    source = root / ".pack/operations.json"
    write(
        source,
        {
            "operations": [
                {"type": "put", "path": "config/a.txt", "content": "same bytes"},
                {"type": "ignore", "glob": "logs/**"},
                package(),
                rule("ignore", "ignore.json"),
            ]
        },
    )
    service = service_for(tmp_path, root)
    before = service.instance_context("dev").resolution.state
    original = source.read_bytes()
    preview = service.preview_schema_upgrade("dev", "client")
    assert preview["from_schema"] == 1 and preview["to_schema"] == 2
    assert source.read_bytes() == original
    assert json.loads((root / ".pack/layer.json").read_text())["schema"] == 1
    options = service.upgrade_layer_schema("dev", "client")
    assert options["schema"] == 2
    assert service.instance_context("dev").resolution.state == before
    records = json.loads(source.read_text())["operations"]
    assert all(record["path_mode"] == "layer" for record in records[:3])
    assert service.preview_schema_upgrade("dev", "client")["already_current"]
    service.add_ignore("dev", "new.txt", "exact", "client")
    assert (root / ".pack/ignore.json").is_file()


def test_schema_upgrade_rejects_existing_nested_content_without_mutation(tmp_path: Path):
    root = layer(tmp_path, "client", schema=1)
    write(root / "mods/.pack/old.json", {"operations": []})
    service = service_for(tmp_path, root)
    marker = root / ".pack/layer.json"
    before = marker.read_bytes()
    for method in (service.preview_schema_upgrade, service.upgrade_layer_schema):
        with pytest.raises(MetadataError, match="nested metadata"):
            method("dev", "client")
    assert marker.read_bytes() == before


def test_schema_upgrade_rolls_back_all_files_after_validation_failure(tmp_path: Path, monkeypatch):
    root = layer(tmp_path, "client", schema=1)
    write(
        root / ".pack/operations.json",
        {"operations": [{"type": "put", "path": "a", "content": "a"}]},
    )
    service = service_for(tmp_path, root)
    original = service.instance_context
    calls = 0

    def fail_third(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise MetadataError("migration verification failure")
        return original(*args, **kwargs)

    before = {path: path.read_bytes() for path in (root / ".pack").iterdir()}
    monkeypatch.setattr(service, "instance_context", fail_third)
    with pytest.raises(MetadataError, match="migration verification"):
        service.upgrade_layer_schema("dev", "client")
    assert {path: path.read_bytes() for path in (root / ".pack").iterdir()} == before


@pytest.mark.parametrize("schema", [1, 2])
def test_new_overlay_uses_main_branch_schema_and_upgrade_survives_feature_edits(
    tmp_path: Path, schema: int
):
    root = layer(tmp_path, "client", schema=schema)
    layer(tmp_path, "feature", schema=3 - schema)
    service = service_for(tmp_path, root)
    assert service.routing_options("dev")["schema"] == schema
    service.add_ignore("dev", "a.txt", "exact")
    overlay = service.state.feature_root("dev")
    marker = overlay / ".pack/layer.json"
    assert json.loads(marker.read_text())["schema"] == schema
    service.upgrade_layer_schema("dev")
    service.add_feature("dev", "feature")
    assert json.loads(marker.read_text())["schema"] == 2
    assert service.instance_context("dev").resolution.state.ignore[0].value == "a.txt"


def test_upgrade_prospective_legacy_overlay_is_explicit_and_preview_does_not_create_it(
    tmp_path: Path,
):
    root = layer(tmp_path, "client", schema=1)
    service = service_for(tmp_path, root)
    assert service.preview_schema_upgrade("dev")["files"] == [".pack/layer.json"]
    assert not service.state.feature_root("dev").exists()
    service.upgrade_layer_schema("dev")
    assert service.routing_options("dev")["schema"] == 2
    assert json.loads((root / ".pack/layer.json").read_text())["schema"] == 1


def test_remove_routing_override_reveals_parent_and_preserves_mixed_source(tmp_path: Path):
    parent = layer(tmp_path, "base")
    write(parent / ".pack/rules.json", {"operations": [rule("ignore", "parent.json")]})
    root = layer(tmp_path, "client", ("../base",))
    source = root / "mods/.pack/custom.json"
    write(source, {"operations": [package(), rule("ignore", "child.json")]})
    service = service_for(tmp_path, root)
    service.save_routing_rule("dev", rule("ignore", "instance.json"), "instance", "client")
    options = service.remove_routing_rule("dev", "ignore", "instance", "client")
    assert options["effective_rules"][0]["write"]["file"]["path"] == "child.json"
    assert options["own_rule_ids"] == ["ignore"]
    options = service.remove_routing_rule("dev", "ignore", "layer", "client")
    assert options["effective_rules"][0]["write"]["file"]["path"] == "parent.json"
    assert json.loads(source.read_text())["operations"] == [package()]
    with pytest.raises(MetadataError, match="inherited"):
        service.remove_routing_rule("dev", "ignore", "layer", "client")


@pytest.mark.parametrize(
    "operation",
    [
        {"type": "ignore", "glob": "logs/**"},
        {"type": "put", "path": "a", "content": "a"},
        {"type": "remove", "path": "a"},
    ],
)
def test_manual_section_rejects_records_without_section_membership(tmp_path: Path, operation: dict):
    root = layer(tmp_path, "client")
    with pytest.raises(MetadataError, match="manual section"):
        ProjectService(tmp_path).plan_metadata_writes(root, [operation], section="Mods")
