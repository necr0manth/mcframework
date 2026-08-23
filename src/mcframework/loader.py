"""JSON metadata frontend and direct-payload loader."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import MetadataError, UnsafePathError
from .model import LayerDefinition, ParentReference, frozen_map
from .operations import DeclarePackage, operation_from_json, producer_for_operations
from .paths import normalize_relative_path

METADATA_DIR = ".pack"
MARKER_FILE = "layer.json"


class WorkspaceLoader:
    """Load layer directories using the initial, intentionally small JSON frontend."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self._cache: dict[Path, LayerDefinition] = {}

    def load(self, layer: Path | str) -> LayerDefinition:
        root = self._resolve_layer_path(layer)
        cached = self._cache.get(root)
        if cached is not None:
            return cached
        marker = root / METADATA_DIR / MARKER_FILE
        if not marker.is_file():
            raise MetadataError(f"layer marker does not exist: {marker}")
        marker_data = self._read_object(marker)
        schema = marker_data.get("schema", 1)
        if schema != 1:
            raise MetadataError(f"{marker}: unsupported schema {schema!r}")
        identity = marker_data.get("id")
        if not isinstance(identity, str) or not identity:
            raise MetadataError(f"{marker}: id must be a non-empty string")
        raw_parents = marker_data.get("parents", [])
        if not isinstance(raw_parents, list):
            raise MetadataError(f"{marker}: parents must be an array")
        parents: list[ParentReference] = []
        for index, raw in enumerate(raw_parents):
            if isinstance(raw, str):
                parents.append(ParentReference(raw))
                continue
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                raise MetadataError(f"{marker}: parent {index} must contain a string path")
            parameters = raw.get("parameters", {})
            validate_parameter_value(parameters, f"{marker}: parent {index} parameters")
            if not isinstance(parameters, dict):
                raise MetadataError(f"{marker}: parent {index} parameters must be an object")
            parents.append(ParentReference(raw["path"], frozen_map(parameters)))

        producers = []
        metadata_root = root / METADATA_DIR
        for metadata_file in sorted(metadata_root.glob("*.json"), key=lambda item: item.name):
            if metadata_file.name == MARKER_FILE:
                continue
            document = self._read_object(metadata_file)
            unknown = set(document) - {"operations"}
            if unknown:
                raise MetadataError(f"{metadata_file}: unsupported keys: {sorted(unknown)}")
            raw_operations = document.get("operations", [])
            if not isinstance(raw_operations, list):
                raise MetadataError(f"{metadata_file}: operations must be an array")
            operations = []
            source = f"{METADATA_DIR}/{metadata_file.name}"
            for index, raw in enumerate(raw_operations):
                if not isinstance(raw, dict):
                    raise MetadataError(f"{metadata_file}: operation {index} must be an object")
                try:
                    operation = operation_from_json(raw, source=source, index=index)
                    if isinstance(operation, DeclarePackage):
                        operation = replace(
                            operation,
                            package=replace(operation.package, source_file=str(metadata_file)),
                        )
                    operations.append(operation)
                except (TypeError, ValueError) as exc:
                    raise MetadataError(
                        f"{metadata_file}: invalid operation {index}: {exc}"
                    ) from exc
            producers.append(producer_for_operations(metadata_file.stem, source, tuple(operations)))

        payload: dict[str, bytes] = {}
        for file in sorted(root.rglob("*")):
            if file == metadata_root or metadata_root in file.parents:
                continue
            if file.is_symlink():
                raise UnsafePathError(f"payload symlinks are not supported: {file}")
            if file.is_file():
                relative = normalize_relative_path(file.relative_to(root).as_posix())
                payload[relative] = file.read_bytes()
        definition = LayerDefinition(
            identity, str(root), tuple(parents), tuple(producers), frozen_map(payload)
        )
        self._cache[root] = definition
        return definition

    def resolve_parent(self, owner: LayerDefinition, reference: ParentReference) -> LayerDefinition:
        path = Path(reference.path)
        if not path.is_absolute():
            path = Path(owner.root) / path
        return self.load(path)

    def _resolve_layer_path(self, layer: Path | str) -> Path:
        path = Path(layer)
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if not path.is_dir():
            raise MetadataError(f"layer directory does not exist: {path}")
        return path

    @staticmethod
    def _read_object(path: Path) -> Mapping[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"cannot read JSON metadata {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise MetadataError(f"{path}: top-level JSON value must be an object")
        return data


def validate_parameter_value(value: Any, label: str = "parameters") -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            validate_parameter_value(item, label)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            validate_parameter_value(item, label)
        return
    raise MetadataError(f"{label} contains a non-serializable value: {value!r}")
