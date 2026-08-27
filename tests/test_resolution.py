from __future__ import annotations

from pathlib import Path

import pytest

from mcframework.errors import MergeConflict, ResolutionError
from mcframework.loader import WorkspaceLoader
from mcframework.model import LayerDefinition, frozen_map
from mcframework.operations import Producer, PutFile, Resource, ResourceDomain
from mcframework.resolver import Resolver


def resolve(tmp_path: Path, layer: str, *, automatic: bool = True):
    return Resolver(WorkspaceLoader(tmp_path), automatic_dependencies=automatic).resolve(layer)


def test_ordered_parents_and_current_layer_win(layer_factory, tmp_path: Path) -> None:
    layer_factory("one", operations=[{"type": "put", "path": "x", "content": "one"}])
    layer_factory("two", operations=[{"type": "put", "path": "x", "content": "two"}])
    layer_factory(
        "top",
        parents=["../one", "../two"],
        operations=[{"type": "put", "path": "x", "content": "top"}],
    )
    result = resolve(tmp_path, "top")
    assert result.state.files["x"] == b"top"
    assert [item.layer for item in result.trace] == ["one", "two", "top"]


def test_shared_ancestor_is_replayed_without_dedup(layer_factory, tmp_path: Path) -> None:
    layer_factory("common", operations=[{"type": "put", "path": "common", "content": "c"}])
    layer_factory("left", parents=["../common"], operations=[])
    layer_factory("right", parents=["../common"], operations=[])
    layer_factory("top", parents=["../left", "../right"], operations=[])
    result = resolve(tmp_path, "top")
    assert [item.layer for item in result.trace] == ["common", "left", "common", "right", "top"]


def test_parent_cycle_fails(layer_factory, tmp_path: Path) -> None:
    layer_factory("a", parents=["../b"], operations=[])
    layer_factory("b", parents=["../a"], operations=[])
    with pytest.raises(ResolutionError, match="cycle"):
        resolve(tmp_path, "a")


def test_same_layer_different_puts_conflict(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "bad",
        metadata_files={
            "a.json": [{"type": "put", "path": "x", "content": "a"}],
            "b.json": [{"type": "put", "path": "x", "content": "b"}],
        },
    )
    with pytest.raises(MergeConflict, match="same-layer"):
        resolve(tmp_path, "bad")


def test_identical_same_layer_writes_merge(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "ok",
        metadata_files={
            "a.json": [{"type": "put", "path": "x", "content": "same"}],
            "b.json": [{"type": "put", "path": "x", "content": "same"}],
        },
    )
    assert resolve(tmp_path, "ok").state.files["x"] == b"same"


def test_payload_conflicts_with_generated_file(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "bad",
        operations=[{"type": "put", "path": "x", "content": "generated"}],
        payload={"x": "payload"},
    )
    with pytest.raises(MergeConflict):
        resolve(tmp_path, "bad")


def test_properties_patches_merge_by_key_and_read_parent(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[], payload={"server.properties": "a=1\nb=2\n"})
    layer_factory(
        "top",
        parents=["../base"],
        metadata_files={
            "a.json": [{"type": "patch_properties", "path": "server.properties", "set": {"a": 3}}],
            "b.json": [
                {
                    "type": "patch_properties",
                    "path": "server.properties",
                    "remove": ["b"],
                    "set": {"c": "4"},
                }
            ],
        },
    )
    assert resolve(tmp_path, "top").state.files["server.properties"] == b"a=3\nc=4\n"


def test_properties_identical_key_writes_merge(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[], payload={"a.properties": "x=1\n"})
    layer_factory(
        "top",
        parents=["../base"],
        metadata_files={
            "a.json": [{"type": "patch_properties", "path": "a.properties", "set": {"x": 2}}],
            "b.json": [{"type": "patch_properties", "path": "a.properties", "set": {"x": 2}}],
        },
    )
    assert resolve(tmp_path, "top").state.files["a.properties"] == b"x=2\n"


def test_properties_same_key_conflict_and_missing_precondition(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("base", operations=[], payload={"a.properties": "x=1\n"})
    layer_factory(
        "conflict",
        parents=["../base"],
        metadata_files={
            "a.json": [{"type": "patch_properties", "path": "a.properties", "set": {"x": 2}}],
            "b.json": [{"type": "patch_properties", "path": "a.properties", "set": {"x": 3}}],
        },
    )
    with pytest.raises(MergeConflict):
        resolve(tmp_path, "conflict")
    layer_factory(
        "missing", operations=[{"type": "patch_properties", "path": "none", "set": {"x": 1}}]
    )
    with pytest.raises(ResolutionError, match="missing"):
        resolve(tmp_path, "missing")


def test_remove_and_persistent_remove_paths(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[], payload={"a": "a", "drop/x": "x"})
    layer_factory(
        "middle",
        parents=["../base"],
        operations=[{"type": "remove", "path": "a"}, {"type": "remove_paths", "glob": "drop/**"}],
    )
    layer_factory("top", parents=["../middle"], operations=[], payload={"drop/y": "y", "keep": "k"})
    result = resolve(tmp_path, "top")
    assert result.state.files == {"keep": b"k"}
    assert result.state.exclusions == ("drop/**",)


def test_same_layer_put_and_exclusion_conflict(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "bad",
        operations=[
            {"type": "put", "path": "drop/x", "content": "x"},
            {"type": "remove_paths", "glob": "drop/**"},
        ],
    )
    with pytest.raises(MergeConflict, match="excludes"):
        resolve(tmp_path, "bad")


def test_effect_domain_rejects_escape(tmp_path: Path) -> None:
    producer = Producer(
        "bad", "test", (PutFile("outside/x", b"x"),), (ResourceDomain("file", ("config/**",)),)
    )
    layer = LayerDefinition("bad", str(tmp_path), (), (producer,), frozen_map())
    with pytest.raises(ResolutionError, match="outside may_write"):
        Resolver(WorkspaceLoader(tmp_path)).resolve(layer)


def test_glob_effect_domain_accepts_literal_glob_characters_in_path() -> None:
    domain = ResourceDomain("file", (".idea/**",))
    resource = Resource("file", (".idea/shelf/Uncommitted_[Changes]/mod?.jar",))

    assert domain.contains(resource)


def test_trace_redacts_secrets(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[])
    result = Resolver(WorkspaceLoader(tmp_path)).resolve(
        "base", {"token_secret": "value", "nested": {"$secret": "ref"}, "public": 1}
    )
    assert result.trace[0].parameters == {
        "token_secret": "<redacted>",
        "nested": "<redacted>",
        "public": 1,
    }


@pytest.mark.parametrize(
    "metadata_files",
    [
        {
            "a-activation.json": [{"type": "activation", "id": "mod", "state": "enabled"}],
            "z-package.json": [
                {
                    "type": "package",
                    "id": "mod",
                    "destination": "mods/mod.jar",
                    "source": {
                        "provider": "local",
                        "location": "mod.jar",
                        "sha256": "0" * 64,
                    },
                }
            ],
        },
        {
            "a-package.json": [
                {
                    "type": "package",
                    "id": "mod",
                    "destination": "mods/mod.jar",
                    "source": {
                        "provider": "local",
                        "location": "mod.jar",
                        "sha256": "0" * 64,
                    },
                }
            ],
            "z-activation.json": [{"type": "activation", "id": "mod", "state": "enabled"}],
        },
    ],
)
def test_same_layer_package_and_activation_are_rejected_independent_of_order(
    layer_factory, tmp_path: Path, metadata_files
) -> None:
    layer_factory("pack", metadata_files=metadata_files)
    with pytest.raises(ResolutionError) as error:
        resolve(tmp_path, "pack")
    assert str(error.value) == (
        "same-layer activation cannot target package absent from immutable parent state: "
        "'mod'; set activation on the declaration"
    )


def test_package_declaration_carries_composite_activation(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "pack",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "activation": "enabled",
                "destination": "mods/mod.jar",
                "source": {
                    "provider": "local",
                    "location": "mod.jar",
                    "sha256": "0" * 64,
                },
            }
        ],
    )
    assert [package.identity for package in resolve(tmp_path, "pack").active_packages] == ["mod"]
