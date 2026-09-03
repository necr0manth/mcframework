from __future__ import annotations

import copy
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

import mcframework.project as project_module
from mcframework.errors import MetadataError
from mcframework.project import ProjectService


class NoNetworkMatcher:
    def match(self, content: bytes) -> None:
        raise AssertionError("rename regressions must not query package providers")


@pytest.fixture
def imported_instance(layer_factory, tmp_path: Path):
    layer_factory("base", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"extra","version":"1.0"}')
    content = output.getvalue()
    (game / "mods/extra.jar").write_bytes(content)
    service = ProjectService(tmp_path, provider_matcher=NoNetworkMatcher())
    service.link("base", "old", game)
    item = service.instance_diff(service.instance_context("old"), enrich_actual=False)["items"][0]
    service.import_package("old", item["id"])
    return service, content


def metadata(service: ProjectService, instance: str) -> Path:
    return service.state.feature_root(instance) / ".pack/local-edits.json"


def state_files(service: ProjectService) -> dict[str, bytes]:
    return {
        path.relative_to(service.state.root).as_posix(): path.read_bytes()
        for path in service.state.root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("clear_before_rename", [False, True])
def test_imported_package_resolves_after_rename_without_cache(
    imported_instance, clear_before_rename: bool
) -> None:
    service, content = imported_instance
    before = service.instance_context("old")
    operation = json.loads(metadata(service, "old").read_text())["operations"][0]
    original = service.project / operation["source"]["location"]
    cache = service.state.root / "cache"
    assert cache.is_dir()
    if clear_before_rename:
        shutil.rmtree(cache)

    service.rename_instance("old", "new")
    if cache.exists():
        shutil.rmtree(cache)
    assert not original.exists()
    assert not service.state.feature_root("old").exists()
    assert before.instance.path.is_dir()

    refreshed = service.instance_context("new")
    assert refreshed.instance.path == before.instance.path
    assert refreshed.files["mods/extra.jar"] == content
    assert service.instance_diff(refreshed, enrich_actual=False)["items"] == []
    package = refreshed.resolution.state.packages["extra"]
    assert package.source.location.startswith(".mcpack/features/new/")
    assert package.source_file == str(metadata(service, "new"))
    assert package.source_index == before.resolution.state.packages["extra"].source_index
    assert refreshed.resolution.trace[-1].layer == "local-new"
    marker = json.loads((service.state.feature_root("new") / ".pack/layer.json").read_text())
    assert marker == {
        "schema": 1,
        "id": "local-new",
        "parents": [str(service.branch_root("base"))],
        "features": [],
    }


def test_only_owned_local_sources_are_rewritten_and_metadata_is_preserved(
    imported_instance,
) -> None:
    service, _ = imported_instance
    old = service.state.feature_root("old")
    new = service.state.feature_root("new")
    original = json.loads(metadata(service, "old").read_text())["operations"][0]
    owned = service.project / original["source"]["location"]
    locations = [
        ("absolute-owned", "local", str(owned), str(new / owned.relative_to(old))),
        ("relative-external", "local", "shared/old/artifact.jar", "shared/old/artifact.jar"),
        ("absolute-external", "local", str(service.project.parent / "external.jar"), None),
        ("prefix-sibling", "local", ".mcpack/features/old-other/artifact.jar", None),
        ("parent-escape", "local", ".mcpack/features/old/../other/artifact.jar", None),
        ("remote", "direct_url", "https://example.invalid/.mcpack/features/old/artifact.jar", None),
        ("other-provider", "custom", original["source"]["location"], None),
    ]
    operations = []
    expected = []
    for identity, provider, location, renamed in locations:
        operation = copy.deepcopy(original)
        operation.update(
            id=identity,
            destination=f"mods/{identity}.jar",
            activation="disabled",
            version="1.2.3",
            logical_id=f"logical-{identity}",
            provenance={"note": "preserve raw metadata", "original": location},
        )
        operation["source"].update(provider=provider, location=location, provenance={"id": 7})
        operations.append(operation)
        changed = copy.deepcopy(operation)
        changed["source"]["location"] = renamed or location
        expected.append(changed)
    other_metadata = old / ".pack/other-packages.json"
    other_metadata.write_text(json.dumps({"operations": operations}), encoding="utf-8")
    unchanged = old / ".pack/untouched.json"
    unchanged.write_text('  {"operations": []}\n', encoding="utf-8")
    unchanged_bytes = unchanged.read_bytes()

    service.rename_instance("old", "new")

    assert json.loads((new / ".pack/other-packages.json").read_text()) == {"operations": expected}
    assert (new / ".pack/untouched.json").read_bytes() == unchanged_bytes
    package = service.instance_context("new").resolution.state.packages["absolute-owned"]
    assert package.version == "1.2.3"


@pytest.mark.parametrize(
    "reference", ["owned-alias", "external-alias", "external-target", "symlink-parent"]
)
def test_rename_respects_symlink_source_ownership(imported_instance, reference: str) -> None:
    service, content = imported_instance
    old = service.state.feature_root("old")
    document = json.loads(metadata(service, "old").read_text())
    operation = document["operations"][0]
    owned = service.project / operation["source"]["location"]
    if reference == "owned-alias":
        alias = old / ".pack/artifacts/alias.jar"
        target = owned
        location = alias.relative_to(service.project).as_posix()
    elif reference == "external-alias":
        alias = service.project / "external-alias.jar"
        target = owned
        location = alias.relative_to(service.project).as_posix()
    elif reference == "external-target":
        alias = old / ".pack/artifacts/alias.jar"
        target = service.project / "external.jar"
        target.write_bytes(content)
        location = alias.relative_to(service.project).as_posix()
    else:
        alias = old / ".pack/link"
        target = service.project / "external/subdir"
        target.mkdir(parents=True)
        (target.parent / "external.jar").write_bytes(content)
        location = ".mcpack/features/old/.pack/link/../external.jar"
    try:
        alias.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    resolved_source = (service.project / location).resolve()
    if reference == "symlink-parent":
        # Windows collapses '..' before following the link; POSIX follows it first.
        assert resolved_source in {target.parent / "external.jar", old / ".pack/external.jar"}
        resolved_source.write_bytes(content)
    operation["source"]["location"] = location
    if reference != "owned-alias":
        operation["activation"] = "disabled"
    metadata(service, "old").write_text(json.dumps(document), encoding="utf-8")

    service.rename_instance("old", "new")

    renamed_document = json.loads(metadata(service, "new").read_text())
    renamed = renamed_document["operations"][0]["source"]["location"]
    if reference != "external-alias" and resolved_source.is_relative_to(old):
        expected_source = service.state.feature_root("new") / resolved_source.relative_to(old)
        assert renamed == expected_source.relative_to(service.project).as_posix()
        assert expected_source.read_bytes() == content
        if reference == "owned-alias":
            shutil.rmtree(service.state.root / "cache")
            assert service.instance_context("new").files["mods/extra.jar"] == content
    else:
        assert renamed == location


@pytest.mark.parametrize("failure", ["identity", "metadata", "instances", "links", "optional"])
def test_rename_failure_restores_all_state(imported_instance, monkeypatch, failure: str) -> None:
    service, content = imported_instance
    log = service.state.web_log_path("old")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"existing log\n")
    before = state_files(service)
    error = OSError(f"injected {failure} failure")
    if failure == "metadata":
        original_write = project_module.atomic_write
        target = metadata(service, "new")

        def fail_metadata(path, data):
            original_write(path, data)
            if path == target:
                raise error

        monkeypatch.setattr(project_module, "atomic_write", fail_metadata)
    elif failure == "optional":
        original_rename = service._rename_optional

        def fail_optional(source, destination):
            if source == log:
                raise error
            original_rename(source, destination)

        monkeypatch.setattr(service, "_rename_optional", fail_optional)
    else:
        owner = service if failure == "identity" else service.state
        name = "_rewrite_overlay_identity" if failure == "identity" else f"write_{failure}"
        original_method = getattr(owner, name)

        def fail_after_write(*args, **kwargs):
            original_method(*args, **kwargs)
            raise error

        monkeypatch.setattr(owner, name, fail_after_write)

    with pytest.raises(OSError, match=f"injected {failure} failure") as raised:
        service.rename_instance("old", "new")

    assert raised.value is error
    assert state_files(service) == before
    assert not service.state.feature_root("new").exists()
    assert not service.state.parameters_path("new").exists()
    assert not service.state.web_log_path("new").exists()
    shutil.rmtree(service.state.root / "cache")
    assert service.instance_context("old").files["mods/extra.jar"] == content


@pytest.mark.parametrize("destination", ["overlay", "parameters", "log"])
def test_rename_rejects_existing_destination_without_changes(
    imported_instance, destination
) -> None:
    service, _ = imported_instance
    if destination == "overlay":
        path = service.state.feature_root("new") / "existing.txt"
    elif destination == "parameters":
        path = service.state.parameters_path("new")
    else:
        path = service.state.web_log_path("new")
        source = service.state.web_log_path("old")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"old log")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"must not overwrite")
    before = state_files(service)

    with pytest.raises(MetadataError, match="destination already exists"):
        service.rename_instance("old", "new")

    assert state_files(service) == before
