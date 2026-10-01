from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.mcpack import McpackParser
from mcframework.package_inspect import file_hashes
from mcframework.project import ProjectService
from mcframework.sync import disabled_package_aliases


class NoMatch:
    def match(self, content: bytes) -> None:
        return None


def setup_capture(tmp_path: Path, count: int, *, modern: bool = True):
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    root = tmp_path / "client"
    if modern:
        service.init_layer("client")
    else:
        (root / ".pack").mkdir(parents=True)
        (root / ".pack/layer.json").write_text(json.dumps({"schema": 1, "id": "client"}))
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    contents = {}
    for index in range(count):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(
                "fabric.mod.json", json.dumps({"id": f"fixture{index}", "version": "1"})
            )
        suffix = ".jar.disabled" if index == count - 1 else ".jar"
        stem = (
            "continents_26.1_v1.1.13" if index == count - 1
            else "2cave" if index == 0
            else "sodium-0.6.13" if index == 1
            else f"fixture{index}"
        )
        path = f"mods/{stem}{suffix}"
        content = output.getvalue()
        (game / path).write_bytes(content)
        contents[path] = content
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "mods", "client")
    decisions = {}
    for index, item in enumerate(plan["items"]):
        candidate = "local" if index == 0 else {
            "provider": "modrinth", "project_id": f"project{index}",
            "version_id": f"version{index}", "version": f"version{index}",
            "download_url": f"https://cdn.example.invalid/{index}.jar", "confidence": "exact",
            "hashes": file_hashes(contents[item["path"]]), "kind": "mod",
            "project_slug": f"slug{index}", "filename": f"{index}.jar",
        }
        decisions[item["id"]] = {
            "mode": "package", "provider_match": candidate,
            "expected_sha256": item["expected_sha256"],
        }
    return service, root, game, plan, decisions, contents


def test_atomic_capture_369_packages_uses_available_star_without_disable(
    tmp_path: Path,
):
    service, root, game, plan, decisions, contents = setup_capture(tmp_path, 369)
    try:
        service.capture_explorer_path("dev", "mods", "client", decisions)
        parsed = [
            line.record for path in root.rglob("*.mcpack")
            for line in McpackParser().parse(path.read_bytes(), path).lines
            if isinstance(line.record, dict)
        ]
        declarations = [record for record in parsed if record.get("type") == "package"]
        activations = [record for record in parsed if record.get("type") == "activation"]
        assert len(declarations) == 369
        assert sum(record["activation"] == "enabled" for record in declarations) == 368
        assert sum(record["activation"] == "available" for record in declarations) == 1
        assert activations == []
        assert {record["source"]["provider"] for record in declarations} == {"local", "modrinth"}
        context = service.instance_context("dev")
        assert len(context.resolution.state.packages) == 369
        available = next(
            package for package in context.resolution.state.packages.values()
            if package.destination == "mods/continents_26.1_v1.1.13.jar"
        )
        assert available.activation.value == "available"
        assert "mods/continents_26.1_v1.1.13.jar" not in context.files
        for path, content in contents.items():
            assert (game / path).read_bytes() == content
            if not path.endswith(".disabled"):
                assert context.files[path] == content
        assert service.explorer_plan("dev", "mods", "client")["items"] == []
    finally:
        service.close()


@pytest.mark.parametrize("modern", [False, True])
def test_folder_capture_rejected_remote_hash_preserves_atomicity(tmp_path: Path, modern: bool):
    service, root, game, plan, decisions, contents = setup_capture(tmp_path, 3, modern=modern)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    bad = decisions[plan["items"][-1]["id"]]["provider_match"]
    bad["hashes"] = {"sha512": "0" * 128}
    try:
        with pytest.raises(MetadataError, match="hashes do not match"):
            service.capture_explorer_path("dev", "mods", "client", decisions)
        after = {
            path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
        }
        assert after == before
        assert all((game / path).read_bytes() == content for path, content in contents.items())
        assert service.instance_context("dev").resolution.state.packages == {}
    finally:
        service.close()


def test_schema_one_folder_capture_preserves_disabled_package(tmp_path: Path):
    service, root, _, _, decisions, _ = setup_capture(tmp_path, 2, modern=False)
    try:
        service.capture_explorer_path("dev", "mods", "client", decisions)
        context = service.instance_context("dev")
        assert sorted(
            package.activation.value for package in context.resolution.state.packages.values()
        ) == ["disabled", "enabled"]
        assert not list(root.rglob("*.mcpack"))
    finally:
        service.close()


def test_single_disabled_import_uses_star_only_with_filename_identity(tmp_path: Path):
    service, root, game, plan, _, contents = setup_capture(tmp_path, 1)
    try:
        item = plan["items"][0]
        service.import_package(
            "dev", item["id"], "client", provider_match="local",
            expected_sha256=item["expected_sha256"],
        )
        records = [
            line.record for path in root.rglob("*.mcpack")
            for line in McpackParser().parse(path.read_bytes(), path).lines
            if isinstance(line.record, dict)
        ]
        declaration = next(record for record in records if record.get("type") == "package")
        assert declaration["activation"] == "available"
        assert not any(record.get("type") == "activation" for record in records)
        line = next(
            line for path in root.rglob("*.mcpack")
            for line in McpackParser().parse(path.read_bytes(), path).lines
            if isinstance(line.record, dict) and line.record.get("type") == "package"
        )
        assert line.expression.startswith("*")
        assert line.record["destination"] == "mods/continents_26.1_v1.1.13.jar"
        context = service.instance_context("dev")
        package = next(iter(context.resolution.state.packages.values()))
        assert package.activation.value == "available"
        assert service.explorer_plan("dev", "mods", "client")["items"] == []
        assert all((game / path).read_bytes() == content for path, content in contents.items())
    finally:
        service.close()


def test_exact_modrinth_import_round_trips_hyphenated_filename_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    root = service.init_layer("client")
    (root / ".pack/layer.mcpack").write_text(
        'layer("client", minecraft="1.21.3", loader="fabric")\n', encoding="utf-8"
    )
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    filename = "-villagers-regeneration-1.0.0+1.21.3.jar"
    destination = f"mods/{filename}"
    content_stream = io.BytesIO()
    with zipfile.ZipFile(content_stream, "w") as archive:
        archive.writestr(
            "fabric.mod.json", json.dumps({"id": "villagers_regeneration", "version": "1.0.0"})
        )
    content = content_stream.getvalue()
    (game / destination).write_bytes(content)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "mods", "client")
    item = plan["items"][0]
    candidate = {
        "provider": "modrinth",
        "project_id": "project-123",
        "project_slug": "villagers-regeneration",
        "version_id": "version-456",
        "version": "1.0.0+1.21.3",
        "filename": filename,
        "download_url": f"https://cdn.example.invalid/{filename}",
        "hashes": file_hashes(content),
        "confidence": "exact",
        "kind": "mod",
    }
    try:
        service.import_package(
            "dev", item["id"], "client", candidate,
            expected_sha256=item["expected_sha256"],
        )
        declarations = []
        lines = []
        for path in service.branch_root("client").rglob("*.mcpack"):
            document = McpackParser().parse(path.read_bytes(), path)
            for line in document.lines:
                if isinstance(line.record, dict) and line.record.get("type") == "package":
                    declarations.append(line.record)
                    lines.append(line.expression)
        assert len(declarations) == 1
        declaration = declarations[0]
        assert declaration["source"] == {
            "provider": "modrinth", "location": "villagers-regeneration",
            "sha256": "",
        }
        assert declaration["version"] == "1.0.0+1.21.3"
        assert declaration["activation"] == "enabled"
        assert declaration.get("destination") is None
        assert lines[0].startswith("modrinth(")
        assert all(
            token not in lines[0]
            for token in ("*", "add(", "kind=", "destination=", "update=")
        )
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network access")),
        )
        context = service.instance_context("dev", prepare=False)
        _, prepared_files = service.artifact_components()[2].prepare(context.resolution)
        assert prepared_files[destination] == content
        assert hashlib.sha256(prepared_files[destination]).hexdigest() == item["expected_sha256"]
    finally:
        service.close()


def test_exact_import_updates_existing_mcpack_line_in_place(tmp_path: Path):
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    root = service.init_layer("client")
    original = io.BytesIO()
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "magic", "version": "1.0"}))
    (root / "original.jar").write_bytes(original.getvalue())
    owner = root / "mods/.pack/mods.mcpack"
    owner.parent.mkdir(parents=True)
    owner_before = (
        b"// header\r\n"
        b'magic = local("original.jar", destination="mods/magic.jar") // keep this note\r\n'
        b"// tail\r\n"
    )
    owner.write_bytes(owner_before)
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    updated = io.BytesIO()
    with zipfile.ZipFile(updated, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "magic", "version": "2.0"}))
    content = updated.getvalue()
    (game / "mods/magic.jar").write_bytes(content)
    service.link("client", "dev", game)
    delta = service.explorer_plan("dev", "mods", "client")["items"][0]
    candidate = {
        "provider": "modrinth",
        "project_id": "project-magic",
        "project_slug": "magic",
        "version_id": "version-2",
        "version": "2.0",
        "filename": "magic.jar",
        "download_url": "https://cdn.example.invalid/magic.jar",
        "hashes": file_hashes(content),
        "confidence": "exact",
        "kind": "mod",
    }
    try:
        service.import_package(
            "dev", delta["id"], "client", candidate,
            expected_sha256=delta["expected_sha256"],
        )
        after = owner.read_bytes()
        assert b"// header\r\n" in after
        assert b'// keep this note\r\n' in after
        assert after.endswith(b"// tail\r\n")
        assert b'magic = modrinth("magic", "2.0") // keep this note\r\n' in after
        assert b"destination=" not in after
        context = service.instance_context("dev", prepare=False)
        _, prepared_files = service.artifact_components()[2].prepare(context.resolution)
        assert prepared_files["mods/magic.jar"] == content
    finally:
        service.close()


@pytest.mark.parametrize("capture_mode", ["single", "folder"])
@pytest.mark.parametrize("fail_after_write", [False, True], ids=["success", "rollback"])
def test_exact_disabled_import_hydrates_cached_destination_for_alias_reconciliation(
    tmp_path: Path,
    capture_mode: str,
    fail_after_write: bool,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path
    service = ProjectService(root, provider_matcher=NoMatch())
    service.init_layer("client")
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    filename = "Continents_26.1_v1.1.13.jar"
    path = f"mods/{filename}.disabled"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "continents", "version": "1.1.13"}))
    content = output.getvalue()
    (game / path).write_bytes(content)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "mods", "client")
    item = plan["items"][0]
    candidate = {
        "provider": "modrinth",
        "project_id": "continents-project",
        "project_slug": "continents",
        "version_id": "continents-version",
        "version": "1.1.13",
        "filename": filename,
        "download_url": f"https://cdn.example.invalid/{filename}",
        "hashes": file_hashes(content),
        "confidence": "exact",
        "kind": "mod",
    }
    try:
        root_before = {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and ".mcpack/locks/" not in path.as_posix()
        }
        cache_root = tmp_path / ".mcpack/cache"
        cache_before = {
            path.relative_to(cache_root): path.read_bytes()
            for path in cache_root.rglob("*") if path.is_file()
        } if cache_root.exists() else {}
        original_append = service._append_operations

        if fail_after_write:
            def append_then_fail(*args, **kwargs):
                original_append(*args, **kwargs)
                raise MetadataError("forced post-write failure")

            monkeypatch.setattr(service, "_append_operations", append_then_fail)

        def capture() -> None:
            if capture_mode == "single":
                service.import_package(
                    "dev", item["id"], "client", candidate,
                    expected_sha256=item["expected_sha256"],
                )
            else:
                service.capture_explorer_path(
                    "dev",
                    "mods",
                    "client",
                    {
                        item["id"]: {
                            "mode": "package",
                            "provider_match": candidate,
                            "expected_sha256": item["expected_sha256"],
                        }
                    },
                )

        if fail_after_write:
            with pytest.raises(MetadataError, match="forced post-write failure"):
                capture()
            assert {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".mcpack/locks/" not in path.as_posix()
            } == root_before
            cache_after = {
                path.relative_to(cache_root): path.read_bytes()
                for path in cache_root.rglob("*") if path.is_file()
            } if cache_root.exists() else {}
            assert cache_after == cache_before
            assert (game / path).read_bytes() == content
            monkeypatch.setattr(service, "_append_operations", original_append)

        capture()
        context = service.instance_context("dev")
        package = next(iter(context.resolution.state.packages.values()))
        assert package.destination == f"mods/{filename}"
        assert package.activation.value == "available"
        assert path in disabled_package_aliases(context.resolution.state.packages)
        assert path not in context.files
        assert (game / path).read_bytes() == content
        assert service.instance_diff(context, enrich_actual=False)["items"] == []
        _, _, builder = service.artifact_components()
        build_root = tmp_path / "built"
        builder.build(context.resolution, build_root)
        assert not (build_root / f"mods/{filename}").exists()

        # Provider-default destinations stay omitted on the star declaration.
        declaration_path = Path(package.source_file)
        document = McpackParser().parse(
            declaration_path.read_bytes(), declaration_path
        )
        line = next(
            line for line in document.lines
            if isinstance(line.record, dict) and line.record.get("id") == package.identity
        )
        assert "destination" not in line.record
        service.close()
        service = ProjectService(root, provider_matcher=NoMatch())
        hydrated = service.instance_context("dev")
        hydrated_package = hydrated.resolution.state.packages[package.identity]
        assert hydrated_package.destination == f"mods/{filename}"
        assert path in disabled_package_aliases(hydrated.resolution.state.packages)
        assert service.instance_diff(hydrated, enrich_actual=False)["items"] == []
    finally:
        service.close()


@pytest.mark.parametrize("activation", ["enabled", "disabled", "available"])
@pytest.mark.parametrize("extension", ["mcpack", "json"])
def test_import_serialization_preserves_activation_routing_and_available_state(
    tmp_path: Path, activation: str, extension: str
):
    service, root, _, _, _, _ = setup_capture(tmp_path, 0)
    destination = f"mods/.pack/{activation}.{extension}"
    (root / ".pack/rules.json").write_text(json.dumps({"operations": [{
        "type": "routing_rule", "id": "original-state",
        "when": {"operation_types": ["package"], "activation": activation},
        "write": {"file": {"path": destination, "path_mode": "layer"}, "section": "imports"},
    }]}))
    if extension == "mcpack":
        (root / destination).parent.mkdir(parents=True)
        (root / destination).write_bytes(b"// retained\r\n")
    operation = {
        "type": "package", "id": "sodium-0.6.13", "kind": "mod", "activation": activation,
        "destination": "mods/sodium-0.6.13.jar",
        "source": {"provider": "modrinth", "location": "magic"}, "version": "v1",
    }
    try:
        plan = service.plan_metadata_writes(
            root, [operation], instance="dev", origin="instance_capture"
        )
        assert [write.path for write in plan.writes] == [root / destination]
        if extension == "json":
            records = json.loads(plan.writes[0].after)["operations"]
            declaration = next(record for record in records if record.get("type") == "package")
            assert declaration["activation"] == activation
            assert not any(record.get("type") == "activation" for record in records)
        else:
            assert plan.writes[0].after.count(b"\n") == plan.writes[0].after.count(b"\r\n")
            document = McpackParser().parse(plan.writes[0].after)
            records = [line.record for line in document.lines if isinstance(line.record, dict)]
            declaration = next(record for record in records if record.get("type") == "package")
            assert declaration["activation"] == (
                "available" if activation == "disabled" else activation
            )
            activation_records = [
                record for record in records if record.get("type") == "activation"
            ]
            assert activation_records == []
            section = next(record for record in records if record.get("type") == "section")
            assert section["id"] == "imports"
            section_line = next(
                line for line in document.lines
                if isinstance(line.record, dict) and line.record.get("type") == "section"
            )
            assert section_line.expression == "#imports"
            assert section["order"] == 1
            if activation == "disabled":
                preview = service.preview_routing(
                    "dev", operation, "client", file=destination, section="imports",
                    origin="instance_capture",
                )
                preview_document = preview["writes"][0]["document"]
                preview_records = preview_document["operations"]
                preview_declaration = next(
                    record for record in preview_records
                    if record.get("type") == "package"
                )
                assert preview_declaration["activation"] == "available"
                assert not any(record.get("type") == "activation" for record in preview_records)
                assert "#imports" in preview_document["text"]
    finally:
        service.close()


def test_folder_capture_post_write_failure_rolls_back_metadata_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root, game, _, decisions, contents = setup_capture(tmp_path, 3)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    apply = service._apply_metadata_plan

    def reject_after_write(*args, **kwargs):
        apply(*args, **kwargs)
        raise MetadataError("injected post-write failure")

    monkeypatch.setattr(service, "_apply_metadata_plan", reject_after_write)
    try:
        with pytest.raises(MetadataError, match="injected post-write failure"):
            service.capture_explorer_path("dev", "mods", "client", decisions)
        after = {
            path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
        }
        assert after == before
        assert all((game / path).read_bytes() == content for path, content in contents.items())
        assert service.instance_context("dev").resolution.state.packages == {}
    finally:
        service.close()
