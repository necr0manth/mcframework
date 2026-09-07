from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from mcframework.loader import WorkspaceLoader
from mcframework.metadata import JsonMetadataParser
from mcframework.project import ProjectService


def write_document(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class ToyMetadataFrontend:
    """A second source syntax proving loader/writer domain independence."""

    extensions = (".json", ".meta")
    _json = JsonMetadataParser()

    def read_object(self, path: Path) -> Mapping[str, Any]:
        return self.decode_object(path.read_bytes(), path)

    def decode_object(self, content: bytes, source: str | Path) -> Mapping[str, Any]:
        if Path(source).suffix == ".meta":
            prefix, payload = content.decode("utf-8").split("\n", 1)
            assert prefix == "metadata-v1"
            return json.loads(payload)
        return self._json.decode_object(content, source)

    def encode_object(
        self, document: Mapping[str, Any], *, destination: str | Path | None = None
    ) -> bytes:
        if destination is not None and Path(destination).suffix == ".meta":
            return ("metadata-v1\n" + json.dumps(dict(document)) + "\n").encode()
        return self._json.encode_object(document, destination=destination)


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("refresh", [False, True])
def test_schema_controls_nested_metadata_and_payload(tmp_path: Path, schema: int, refresh: bool):
    root = tmp_path / "layer"
    write_document(root / ".pack/layer.json", {"schema": schema, "id": "layer"})
    write_document(root / ".pack/root.json", {"operations": []})
    nested = root / "mods/.pack/mods.json"
    write_document(nested, {"operations": [{"type": "ignore", "glob": "logs/**"}]})
    control = root / "mods/.pack/cache/internal.json"
    write_document(control, {"not": "an operation document"})
    loader = WorkspaceLoader(tmp_path, refresh=refresh)
    layer = loader.load(root)
    expected = [".pack/root.json"]
    if schema == 2:
        expected.append("mods/.pack/mods.json")
        assert not layer.payload
    else:
        assert layer.payload == {
            "mods/.pack/mods.json": nested.read_bytes(),
            "mods/.pack/cache/internal.json": control.read_bytes(),
        }
    assert [item.source for item in layer.producers] == expected
    assert [item.relative_to(root).as_posix() for item in loader.metadata_files(root)] == expected


@pytest.mark.parametrize("mutation", ["add_document", "add_directory", "edit", "delete"])
def test_nested_metadata_refresh_matches_cold_load(tmp_path: Path, mutation: str):
    root = tmp_path / "layer"
    write_document(root / ".pack/layer.json", {"schema": 2, "id": "layer"})
    document = root / "mods/.pack/mods.json"
    write_document(document, {"operations": []})
    loader = WorkspaceLoader(tmp_path, refresh=True)
    initial = loader.load(root)
    assert loader.fresh()
    if mutation == "delete":
        document.unlink()
    else:
        if mutation == "add_document":
            document = document.with_name("new.json")
        elif mutation == "add_directory":
            document = root / "config/sub/.pack/ignore.json"
        write_document(document, {"operations": [{"type": "ignore", "glob": "logs/**"}]})
    assert not loader.fresh()
    loader.begin()
    refreshed = loader.load(root)
    assert refreshed is not initial
    assert refreshed == WorkspaceLoader(tmp_path).load(root)
    assert loader.fresh()


@pytest.mark.parametrize("schema", [1, 2])
def test_nested_payload_generation_follows_schema(tmp_path: Path, schema: int):
    root = tmp_path / "layer"
    write_document(root / ".pack/layer.json", {"schema": schema, "id": "layer"})
    payload = root / "mods/.pack/cache/payload.txt"
    payload.parent.mkdir(parents=True)
    payload.write_text("before", encoding="utf-8")
    loader = WorkspaceLoader(tmp_path, refresh=True)
    loader.load(root)
    payload.write_text("after", encoding="utf-8")
    assert loader.fresh() is (schema == 2)
    loader.begin()
    assert loader.load(root) == WorkspaceLoader(tmp_path).load(root)


@pytest.mark.parametrize("directory_link", [False, True])
def test_nested_metadata_symlink_generations(tmp_path: Path, directory_link: bool):
    root = tmp_path / "layer"
    write_document(root / ".pack/layer.json", {"schema": 2, "id": "layer"})
    external = tmp_path / "external"
    external.mkdir()
    document = external / "mods.json"
    write_document(document, {"operations": []})
    linked = root / "mods/.pack"
    linked.parent.mkdir()
    if not directory_link:
        linked.mkdir()
        linked = linked / "mods.json"
    try:
        linked.symlink_to(
            external if directory_link else document, target_is_directory=directory_link
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 or isinstance(exc, PermissionError):
            pytest.skip("creating symlinks requires permission")
        raise
    loader = WorkspaceLoader(tmp_path, refresh=True)
    loader.load(root)
    changed = external / "added.json" if directory_link else document
    write_document(changed, {"operations": [{"type": "ignore", "glob": "logs/**"}]})
    assert not loader.fresh()
    loader.begin()
    assert loader.load(root) == WorkspaceLoader(tmp_path).load(root)
    assert loader.fresh()


def test_configured_frontend_drives_discovery_and_write_planning(tmp_path: Path):
    root = tmp_path / "layer"
    write_document(root / ".pack/layer.json", {"schema": 2, "id": "layer"})
    frontend = ToyMetadataFrontend()
    source = root / "mods/.pack/mods.meta"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        frontend.encode_object(
            {
                "operations": [
                    {
                        "type": "package",
                        "id": "example",
                        "destination": "mods/example.jar",
                        "path_mode": "layer",
                        "source": {
                            "provider": "local",
                            "location": "artifact.jar",
                            "sha256": "0" * 64,
                        },
                    }
                ]
            },
            destination=source,
        )
    )

    loader = WorkspaceLoader(tmp_path, parser=frontend)
    assert loader.metadata_files(root) == (source,)
    assert loader.load(root).producers[0].operations[0].package.identity == "example"

    service = ProjectService(tmp_path, metadata_parser=frontend)
    plan = service.plan_metadata_writes(
        root,
        [{"type": "ignore", "glob": "logs/**"}],
        destination=".pack/ignore.meta",
    )
    plan.apply()
    assert frontend.read_object(root / ".pack/ignore.meta")["operations"][0]["type"] == "ignore"
