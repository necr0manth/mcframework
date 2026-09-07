"""Format-independent metadata document frontends."""
from __future__ import annotations

import json
import posixpath
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .errors import MetadataError, UnsafePathError
from .paths import normalize_relative_path


class MetadataParser(Protocol):
    """Codec boundary for metadata documents.

    A future DSL can implement this protocol (or dispatch several extensions
    behind one implementation) without changing typed record parsing, routing,
    resolution, or write planning.
    """

    extensions: tuple[str, ...]

    def read_object(self, path: Path) -> Mapping[str, Any]: ...

    def decode_object(self, content: bytes, source: str | Path) -> Mapping[str, Any]: ...

    def encode_object(
        self, document: Mapping[str, Any], *, destination: str | Path | None = None
    ) -> bytes: ...


class JsonMetadataParser:
    extensions: tuple[str, ...] = (".json",)

    def encode_object(
        self, document: Mapping[str, Any], *, destination: str | Path | None = None
    ) -> bytes:
        """Serialize a document without performing filesystem writes."""
        del destination
        return (json.dumps(dict(document), indent=2) + "\n").encode("utf-8")

    def read_object(self, path: Path) -> Mapping[str, Any]:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise MetadataError(f"cannot read JSON metadata {path}: {exc}") from exc
        return self.decode_object(content, path)

    def decode_object(self, content: bytes, source: str | Path) -> Mapping[str, Any]:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"cannot read JSON metadata {source}: {exc}") from exc
        if not isinstance(data, dict):
            raise MetadataError(f"{source}: top-level JSON value must be an object")
        return data


def _record_paths(raw: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Only domain paths share this coordinate system, not artifact URLs or rules."""
    kind = raw.get("type")
    if kind in ("put", "remove", "patch_properties"):
        return (("path",),)
    if kind == "package":
        return (("destination",),)
    if kind in ("remove_paths", "prune"):
        return (("glob",),)
    if kind == "ignore":
        return (("selector", "path"),) if raw.get("selector") is not None else (("glob",),)
    return ()


def _local_path(value: Any, source: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UnsafePathError("path must be a non-empty string without NUL bytes")
    value = value.replace("\\", "/")
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        raise UnsafePathError(f"absolute path is not allowed: {value!r}")
    parts = list(PurePosixPath(source).parent.parts)
    for part in value.split("/"):
        if part == "..":
            if not parts:
                raise UnsafePathError(f"path escapes layer root: {value!r}")
            parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    return normalize_relative_path("/".join(parts), allow_glob=True)


def _convert_record_paths(
    raw: Mapping[str, Any], source: str, schema: int, mode: str, *, encode: bool
) -> dict[str, Any]:
    result = dict(raw)
    if schema == 1:
        return result
    if schema != 2:
        raise MetadataError(f"unsupported schema {schema!r}")
    if mode not in ("local", "layer"):
        raise MetadataError(f"{source}: path_mode must be local or layer")
    source = normalize_relative_path(source)
    fields = _record_paths(raw)
    for field in fields:
        owner = result
        if len(field) == 2:
            if not isinstance(raw.get(field[0]), Mapping):
                continue  # The operation parser reports malformed selectors.
            owner = dict(raw[field[0]])
            result[field[0]] = owner
        key = field[-1]
        if key not in owner:
            continue  # The operation parser reports missing required fields.
        value = owner[key]
        if encode or mode == "layer":
            value = normalize_relative_path(value, allow_glob=True)
        else:
            value = _local_path(value, source)
        if encode and mode == "local":
            value = posixpath.relpath(value, str(PurePosixPath(source).parent))
        owner[key] = value
    result.pop("path_mode", None)
    if encode and fields and mode == "layer":
        result["path_mode"] = "layer"
    return result


def decode_record_paths(raw: Mapping[str, Any], source: str, schema: int) -> dict[str, Any]:
    """Decode file-local paths to the canonical layer coordinates used by domains."""
    return _convert_record_paths(raw, source, schema, raw.get("path_mode", "local"), encode=False)


def encode_record_paths(
    canonical: Mapping[str, Any], source: str, schema: int, mode: str = "local"
) -> dict[str, Any]:
    """Encode a canonical record for a shard; schema 1 retains legacy layer paths."""
    return _convert_record_paths(canonical, source, schema, mode, encode=True)
