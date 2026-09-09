from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from mcframework.artifacts import ArtifactCache, ArtifactRegistry, ArtifactResolver
from mcframework.build import Builder
from mcframework.errors import MetadataError, UnsafePathError
from mcframework.loader import WorkspaceLoader
from mcframework.metadata import (
    JsonMetadataParser,
    decode_record_paths,
    encode_record_paths,
    normalize_metadata_destination,
)
from mcframework.project import ProjectService
from mcframework.resolver import Resolver


@pytest.mark.parametrize("record", [
    {"type": "put", "path": "config/a.txt", "content": "hello"},
    {"type": "remove", "path": "config/a.txt"},
    {"type": "patch_properties", "path": "config/a.txt", "set": {"path": "unchanged"}},
    {"type": "package", "destination": "mods/a.jar", "source": {"location": "../a.jar"}},
    {"type": "remove_paths", "glob": "config/**/*.txt"},
    {"type": "prune", "glob": "logs/**"},
    {"type": "ignore", "glob": "logs/**"},
    {"type": "ignore", "selector": {"kind": "glob", "path": "logs/**"}},
    {"type": "routing_rule", "write": {"file": {"path": "../mods.json"}}},
])
@pytest.mark.parametrize("source", [".pack/operations.json", "mods/.pack/mods.json"])
@pytest.mark.parametrize("mode", ["layer", "local"])
def test_record_path_roundtrip_does_not_mutate_input(record: dict, source: str, mode: str):
    original = deepcopy(record)
    encoded = encode_record_paths(record, source, 2, mode)
    assert decode_record_paths(encoded, source, 2) == record
    assert record == original
    if record["type"] == "package":
        assert encoded["source"] == {"location": "../a.jar"}
    if record["type"] == "routing_rule":
        assert encoded == record


def test_local_paths_default_to_declaring_file_and_preserve_globs():
    assert decode_record_paths(
        {"type": "remove", "path": "../a.txt"}, "config/.pack/remove.json", 2
    ) == {"type": "remove", "path": "config/a.txt"}
    assert decode_record_paths(
        {"type": "remove_paths", "glob": r"..\..\config\**\[a-z]?.txt"},
        "mods/.pack/remove.json", 2,
    )["glob"] == "config/**/[a-z]?.txt"
    assert encode_record_paths(
        {"type": "remove", "path": "config/a.txt"}, "config/.pack/remove.json", 2
    ) == {"type": "remove", "path": "../a.txt"}


def test_local_paths_use_nearest_pack_directory_for_deep_metadata():
    source = "mods/.pack/groups/optional/mods.json"
    assert decode_record_paths(
        {"type": "package", "destination": "example.jar"}, source, 2
    ) == {"type": "package", "destination": "mods/.pack/example.jar"}
    assert encode_record_paths(
        {"type": "package", "destination": "mods/.pack/example.jar"}, source, 2
    ) == {"type": "package", "destination": "example.jar"}
    assert decode_record_paths(
        {"type": "package", "destination": "../example.jar"}, source, 2
    ) == {"type": "package", "destination": "mods/example.jar"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mods/mods", "mods/.pack/mods.json"),
        ("mods/mods.json", "mods/.pack/mods.json"),
        ("mods/.pack/mods", "mods/.pack/mods.json"),
        ("mods/.pack/mods.json", "mods/.pack/mods.json"),
        ("ignore", ".pack/ignore.json"),
        ("config/.pack/nested/remove.meta", "config/.pack/nested/remove.meta"),
    ],
)
def test_metadata_destination_shorthand_preserves_explicit_paths(value: str, expected: str):
    assert normalize_metadata_destination(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "../mods/mods",
        "/mods/mods",
        "C:/mods/mods",
        r"C:\mods\mods",
        "mods/.pack",
        "mods/.pack/sub/.pack/file.json",
    ],
)
def test_metadata_destination_shorthand_rejects_unsafe_or_directory_paths(value: str):
    with pytest.raises(UnsafePathError):
        normalize_metadata_destination(value)


@pytest.mark.parametrize("path", ["../../../escape", "/etc/passwd", "C:/mods/a.jar",
                                  r"C:\mods\a.jar", r"\\server\share", "", "a\x00b"])
@pytest.mark.parametrize("mode", ["layer", "local"])
def test_path_codec_rejects_unsafe_paths(path: str, mode: str):
    with pytest.raises(UnsafePathError):
        decode_record_paths(
            {"type": "remove", "path": path, "path_mode": mode}, "mods/.pack/remove.json", 2
        )


@pytest.mark.parametrize("mode", ["absolute", None, [], 2])
def test_invalid_path_mode_is_metadata_error(mode):
    with pytest.raises(MetadataError, match="path_mode"):
        decode_record_paths({"type": "remove", "path": "a", "path_mode": mode}, ".pack/a.json", 2)


def test_schema_one_keeps_existing_path_semantics():
    record = {"type": "remove", "path": "config/a.txt"}
    assert decode_record_paths(record, ".pack/remove.json", 1) == record
    assert encode_record_paths(record, ".pack/remove.json", 1) == record


def test_schema_two_local_and_layer_paths_build_identically(tmp_path: Path):
    root = tmp_path / "layer"
    metadata = root / "config/sub/.pack"
    metadata.mkdir(parents=True)
    (root / ".pack").mkdir()
    parser = JsonMetadataParser()
    (root / ".pack/layer.json").write_bytes(parser.encode_object({"schema": 2, "id": "layer"}))
    canonical = {"type": "put", "path": "config/sub/a.txt", "content": "hello"}
    builder = Builder(ArtifactResolver(ArtifactRegistry(), ArtifactCache(tmp_path / "cache")))
    for mode in ("local", "layer"):
        document = {"operations": [encode_record_paths(
            canonical, "config/sub/.pack/files.json", 2, mode
        )]}
        (metadata / "files.json").write_bytes(parser.encode_object(document))
        resolution = Resolver(WorkspaceLoader(tmp_path)).resolve(root)
        assert builder.prepare(resolution)[1] == {"config/sub/a.txt": b"hello"}
        assert json.loads(parser.encode_object(document)) == document


def test_writer_encodes_new_record_relative_to_chosen_shard(tmp_path: Path):
    root = tmp_path / "layer"
    (root / ".pack").mkdir(parents=True)
    (root / ".pack/layer.json").write_text('{"schema": 2, "id": "layer"}')
    service = ProjectService(tmp_path)
    plan = service.plan_metadata_writes(
        root, [{"type": "put", "path": "config/sub/a.txt", "content": "hello"}],
        destination="config/sub/.pack/files.json",
    )
    service._apply_metadata_plan(plan, root)
    document = json.loads((root / "config/sub/.pack/files.json").read_text())
    assert document["operations"] == [{"type": "put", "path": "../a.txt", "content": "hello"}]
    builder = Builder(ArtifactResolver(ArtifactRegistry(), ArtifactCache(tmp_path / "cache")))
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve(root)
    assert builder.prepare(resolution)[1] == {"config/sub/a.txt": b"hello"}


@pytest.mark.parametrize("mode", [None, "local", "layer"])
def test_writer_preserves_existing_package_source_and_path_mode(tmp_path: Path, mode):
    root = tmp_path / "layer"
    (root / ".pack").mkdir(parents=True)
    (root / ".pack/layer.json").write_text('{"schema": 2, "id": "layer"}')
    source = root / "mods/.pack/magic.json"
    source.parent.mkdir(parents=True)
    record = {
        "type": "package", "id": "magic", "activation": "available",
        "destination": "mods/magic.jar" if mode == "layer" else "../magic.jar",
        "source": {"provider": "local", "location": "../artifact.jar", "sha256": "0" * 64},
    }
    if mode is not None:
        record["path_mode"] = mode
    source.write_text(json.dumps({"operations": [record]}))
    service = ProjectService(tmp_path)
    plan = service.plan_metadata_writes(
        root, [{"type": "package", "id": "magic", "destination": "mods/updated.jar"}],
        destination="mods/.pack/other.json",
    )
    assert [write.path for write in plan.writes] == [source]
    service._apply_metadata_plan(plan, root)
    actual = json.loads(source.read_text())["operations"][0]
    assert actual.get("path_mode") == mode
    assert actual["destination"] == (
        "mods/updated.jar" if mode == "layer" else "../updated.jar"
    )
    assert actual["source"] == record["source"]
    resolved = Resolver(WorkspaceLoader(tmp_path)).resolve(root)
    assert resolved.state.packages["magic"].destination == "mods/updated.jar"
