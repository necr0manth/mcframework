"""JSON metadata frontend and direct-payload loader."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import MetadataError, UnsafePathError
from .generations import Generation, generation
from .metadata import JsonMetadataParser, MetadataParser, decode_record_paths
from .model import LayerDefinition, ParentReference, frozen_map
from .operations import DeclarePackage, PutFile, operation_from_json, producer_for_operations
from .paths import normalize_relative_path

METADATA_DIR = ".pack"
MARKER_FILE = "layer.json"


class WorkspaceLoader:
    """Load layer directories using the initial, intentionally small JSON frontend."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        refresh: bool = False,
        parser: MetadataParser | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self._cache: dict[Path, LayerDefinition] = {}
        self._refresh = refresh
        self.parser = parser or JsonMetadataParser()
        self._generations: dict[Path, dict[Path, Generation]] = {}
        self._visited: set[Path] = set()
        self._in_pass = False

    def begin(self) -> None:
        self._visited.clear()
        self._in_pass = True

    def finish(self) -> None:
        """Retain only the graph used by the last successful resolution."""
        self._cache = {root: item for root, item in self._cache.items() if root in self._visited}
        self._generations = {
            root: item for root, item in self._generations.items() if root in self._visited
        }
        self._in_pass = False

    def fresh(self) -> bool:
        return all(
            generation(path) == expected
            for entries in self._generations.values()
            for path, expected in entries.items()
        )

    @staticmethod
    def _scan(
        root: Path,
        schema: int,
        *,
        watch: bool = False,
        metadata_extensions: tuple[str, ...] = (".json",),
    ) -> tuple[list[Path], list[Path], dict[Path, Generation]]:
        # Record directory membership generations before enumerating. A concurrent
        # addition may enter the loaded payload, but cannot become an unwatched hit.
        sources = {root: generation(root)} if watch else {}
        metadata_files: list[Path] = []
        payload_paths: list[Path] = []
        pending = [root]

        def scan_metadata(directory: Path) -> None:
            if watch:
                sources.setdefault(directory, generation(directory))
                resolved = directory.resolve()
                sources.setdefault(resolved, generation(resolved))
            with os.scandir(directory) as metadata_entries:
                for metadata_entry in metadata_entries:
                    metadata = Path(metadata_entry.path)
                    if metadata_entry.is_dir(follow_symlinks=False):
                        if watch:
                            sources[metadata] = generation(metadata)
                            target = metadata.resolve()
                            sources.setdefault(target, generation(target))
                        scan_metadata(metadata)
                    elif (
                        metadata.suffix in metadata_extensions
                        and metadata.is_file()
                        and metadata.name != MARKER_FILE
                    ):
                        if watch:
                            sources[metadata] = generation(metadata)
                            target = metadata.resolve()
                            sources.setdefault(target, generation(target))
                        metadata_files.append(metadata)

        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if watch:
                        sources[path] = generation(path)
                    is_metadata = path.name == METADATA_DIR and (schema == 2 or directory == root)
                    if is_metadata and entry.is_dir():
                        if watch:
                            target = path.resolve()
                            sources.setdefault(target, generation(target))
                        if schema == 2:
                            scan_metadata(path)
                        else:
                            for metadata in path.iterdir():
                                if metadata.suffix not in metadata_extensions:
                                    continue
                                if not metadata.is_file():
                                    continue
                                if watch:
                                    sources[metadata] = generation(metadata)
                                    target = metadata.resolve()
                                    sources.setdefault(target, generation(target))
                                if metadata.name != MARKER_FILE:
                                    metadata_files.append(metadata)
                    else:
                        payload_paths.append(path)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(path)
        metadata_files.sort(key=lambda item: item.relative_to(root).as_posix())
        return metadata_files, sorted(payload_paths), sources

    def metadata_files(self, layer: Path | str) -> tuple[Path, ...]:
        """Discover operation documents using the layer's schema, excluding markers.

        Returned paths retain their spelling within the layer, including supported
        metadata symlinks; callers that write must separately enforce confinement.
        """
        root = self._resolve_layer_path(layer)
        marker = root / METADATA_DIR / MARKER_FILE
        schema = self._schema(marker, self._read_object(marker))
        documents, _, _ = self._scan(
            root, schema, metadata_extensions=self.parser.extensions
        )
        return tuple(documents)

    @staticmethod
    def _schema(marker: Path, document: Mapping[str, Any]) -> int:
        schema = document.get("schema", 1)
        if type(schema) is not int or schema not in (1, 2):
            raise MetadataError(f"{marker}: unsupported schema {schema!r}")
        return schema

    def load(self, layer: Path | str) -> LayerDefinition:
        root = self._resolve_layer_path(layer)
        cached = self._cache.get(root)
        visited = self._in_pass and root in self._visited
        self._visited.add(root)
        previous = self._generations.get(root, {})
        if cached is not None and (
            not self._refresh
            or visited
            or all(generation(path) == expected for path, expected in previous.items())
        ):
            return cached
        marker = root / METADATA_DIR / MARKER_FILE
        if not marker.is_file():
            raise MetadataError(f"layer marker does not exist: {marker}")
        marker_sources = (
            {path: generation(path) for path in (marker, marker.resolve())}
            if self._refresh else {}
        )
        marker_data = self._read_object(marker)
        schema = self._schema(marker, marker_data)
        metadata_files, payload_paths, sources = self._scan(
            root,
            schema,
            watch=self._refresh,
            metadata_extensions=self.parser.extensions,
        )
        sources.update(marker_sources)
        marker_unchanged = all(
            previous.get(path) == stamp for path, stamp in marker_sources.items()
        )
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
        previous_producers = {item.source: item for item in cached.producers} if cached else {}
        for metadata_file in metadata_files:
            source = metadata_file.relative_to(root).as_posix()
            previous_producer = previous_producers.get(source)
            metadata_paths = {metadata_file, metadata_file.resolve()}
            if previous_producer is not None and marker_unchanged and all(
                path in previous and previous[path] == sources.get(path) for path in metadata_paths
            ):
                producers.append(previous_producer)
                continue
            document = self._read_object(metadata_file)
            unknown = set(document) - {"operations"}
            if unknown:
                raise MetadataError(f"{metadata_file}: unsupported keys: {sorted(unknown)}")
            raw_operations = document.get("operations", [])
            if not isinstance(raw_operations, list):
                raise MetadataError(f"{metadata_file}: operations must be an array")
            operations = []
            for index, raw in enumerate(raw_operations):
                if not isinstance(raw, dict):
                    raise MetadataError(f"{metadata_file}: operation {index} must be an object")
                try:
                    operation = operation_from_json(
                        decode_record_paths(raw, source, schema), source=source, index=index
                    )
                    if (
                        isinstance(operation, PutFile)
                        and previous_producer is not None
                        and index < len(previous_producer.operations)
                        and operation == previous_producer.operations[index]
                    ):
                        operation = previous_producer.operations[index]
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
        for file in payload_paths:
            if self._refresh:
                stamp = sources[file]
                if stamp is None:
                    continue
                is_symlink = stat.S_ISLNK(stamp[0])
                is_file = stat.S_ISREG(stamp[0])
            else:
                is_symlink = file.is_symlink()
                is_file = file.is_file()
            if is_symlink:
                raise UnsafePathError(f"payload symlinks are not supported: {file}")
            if is_file:
                relative = normalize_relative_path(file.relative_to(root).as_posix())
                if (
                    cached is not None
                    and relative in cached.payload
                    and file in previous
                    and previous[file] == sources.get(file)
                ):
                    payload[relative] = cached.payload[relative]
                else:
                    payload[relative] = file.read_bytes()
        definition = LayerDefinition(
            identity, str(root), tuple(parents), tuple(producers), frozen_map(payload)
        )
        self._cache[root] = definition
        if self._refresh:
            self._generations[root] = sources
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

    def _read_object(self, path: Path) -> Mapping[str, Any]:
        """Read a metadata object through the configured format adapter.

        Kept as an overridable seam for callers that customized the legacy
        loader; new code should prefer ``parser=`` injection.
        """
        return self.parser.read_object(path)

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
