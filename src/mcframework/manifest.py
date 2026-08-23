"""Deterministic resolution manifest generation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from .errors import MergeConflict
from .model import ManifestEntry, Resolution
from .operations import operation_to_json


def materialized_files(resolution: Resolution) -> tuple[Resolution, Mapping[str, bytes]]:
    files = dict(resolution.state.files)
    entries = [entry_for(path, content) for path, content in files.items()]
    for artifact in resolution.artifacts:
        path = artifact.declaration.destination
        if path in files:
            raise MergeConflict(f"package destination conflicts with generated file: {path!r}")
        content = Path(artifact.cache_path).read_bytes()
        files[path] = content
        entries.append(
            ManifestEntry(path, artifact.sha256, artifact.size, artifact.declaration.identity)
        )
    entries.sort(key=lambda item: item.path)
    return replace(resolution, manifest=tuple(entries)), dict(sorted(files.items()))


def entry_for(path: str, content: bytes) -> ManifestEntry:
    return ManifestEntry(path, hashlib.sha256(content).hexdigest(), len(content))


def manifest_document(resolution: Resolution) -> dict[str, object]:
    return {
        "schema": 1,
        "files": [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size": entry.size,
                **({"package": entry.package} if entry.package else {}),
            }
            for entry in resolution.manifest
        ],
        "artifacts": [
            {
                "declaration": artifact.declaration.identity,
                "logical_identity": artifact.declaration.logical_identity,
                "provider": artifact.declaration.source.provider,
                "version": artifact.declaration.version,
                "sha256": artifact.sha256,
                "destination": artifact.declaration.destination,
            }
            for artifact in sorted(resolution.artifacts, key=lambda item: item.declaration.identity)
        ],
        "external_parents": [],
        "layers": [
            {
                "id": trace.layer,
                "parameters": dict(trace.parameters),
                "producers": [
                    {
                        "name": producer.producer,
                        "source": producer.source,
                        "operations": [operation_to_json(item) for item in producer.operations],
                    }
                    for producer in trace.producers
                ],
            }
            for trace in resolution.trace
        ],
    }


def manifest_bytes(resolution: Resolution) -> bytes:
    return (
        json.dumps(manifest_document(resolution), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
