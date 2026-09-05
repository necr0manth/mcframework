"""Bounded, verified reuse of a single current desired-state projection."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from .build import Builder
from .errors import ArtifactError, MergeConflict
from .generations import Generation, generation
from .model import ManifestEntry, Resolution, ResolvedArtifact
from .operations import PutFile

ContentDigests = Mapping[int, tuple[bytes, str]]


def content_digest(content: bytes, known: ContentDigests) -> str:
    item = known.get(id(content))
    if item is not None and item[0] is content:
        return item[1]
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class PreparedState:
    resolution: Resolution
    files: Mapping[str, bytes]
    digests: ContentDigests
    generations: Mapping[Path, Generation]

    def fresh(self) -> bool:
        return all(generation(path) == expected for path, expected in self.generations.items())


class IncrementalPreparer:
    def __init__(self) -> None:
        self.current: PreparedState | None = None

    def prepare(self, resolution: Resolution, builder: Builder) -> PreparedState:
        previous = self.current
        content_state = resolution.state.content
        previous_content = previous.resolution.state.content if previous is not None else None
        files: Mapping[str, bytes]
        generations: dict[Path, Generation] = {}
        if previous is None:
            resolved, files = builder.prepare(resolution)
            for artifact in resolved.artifacts:
                content, stamp = self._read_verified(artifact)
                if content != files[artifact.declaration.destination]:
                    raise ArtifactError("artifact changed during preparation")
                generations[Path(artifact.cache_path)] = stamp
        elif (
            len(content_state.files) == len(previous_content.files)
            and all(
                previous_content.files.get(path) is content
                for path, content in content_state.files.items()
            )
            and len(resolution.active_packages) == len(previous.resolution.artifacts)
            and all(
                package.identity == old.declaration.identity
                and package.source == old.declaration.source
                and package.destination == old.declaration.destination
                for package, old in zip(
                    resolution.active_packages, previous.resolution.artifacts, strict=True
                )
            )
            and previous.fresh()
        ):
            # No materialized impact (e.g. ignore, section, or a still-required mod).
            # Logical resolution has already validated closure and destination conflicts.
            resolved = replace(
                resolution,
                artifacts=tuple(
                    replace(old, declaration=package)
                    for package, old in zip(
                        resolution.active_packages, previous.resolution.artifacts, strict=True
                    )
                ),
                manifest=previous.resolution.manifest,
            )
            files = previous.files
            generations.update(previous.generations)
        else:
            artifacts: list[ResolvedArtifact] = []
            old_artifacts = {
                item.declaration.identity: item for item in previous.resolution.artifacts
            }
            contents = dict(content_state.files)
            entries: list[ManifestEntry] = []
            for path, content in content_state.files.items():
                entries.append(
                    ManifestEntry(path, content_digest(content, previous.digests), len(content))
                )
            for package in resolution.active_packages:
                path = package.destination
                # This check is required even when every byte can be reused.
                if path in contents:
                    raise MergeConflict(
                        f"package destination conflicts with generated file: {path!r}"
                    )
                old = old_artifacts.get(package.identity)
                if (
                    old is not None
                    and old.declaration.source == package.source
                    and old.declaration.destination == path
                    and generation(Path(old.cache_path))
                    == previous.generations[Path(old.cache_path)]
                ):
                    artifact = replace(old, declaration=package)
                    content = previous.files[path]
                    generations[Path(old.cache_path)] = previous.generations[Path(old.cache_path)]
                else:
                    artifact = builder.artifacts.cache.obtain(
                        package, builder.artifacts.registry.provider(package.source.provider)
                    )
                    content, stamp = self._read_verified(artifact)
                    generations[Path(artifact.cache_path)] = stamp
                artifacts.append(artifact)
                contents[path] = content
                entries.append(
                    ManifestEntry(path, artifact.sha256, artifact.size, package.identity)
                )
            manifest = tuple(sorted(entries, key=lambda item: item.path))
            if manifest == previous.resolution.manifest:
                manifest = previous.resolution.manifest
            resolved = replace(resolution, artifacts=tuple(artifacts), manifest=manifest)
            files = contents

        reusable = (
            previous is not None
            and len(files) == len(previous.files)
            and all(previous.files.get(path) is content for path, content in files.items())
        )
        prepared_files = (
            previous.files if reusable and previous is not None else MappingProxyType(files)
        )
        digests: dict[int, tuple[bytes, str]] = {
            id(prepared_files[item.path]): (prepared_files[item.path], item.sha256)
            for item in resolved.manifest
        }
        # Diagnostics include overwritten/excluded PutFile operations, not only final files.
        for trace in resolved.trace:
            for producer in trace.producers:
                for operation in producer.operations:
                    if isinstance(operation, PutFile) and id(operation.content) not in digests:
                        digest = content_digest(
                            operation.content, previous.digests if previous else {}
                        )
                        digests[id(operation.content)] = (operation.content, digest)
        result = PreparedState(
            resolved,
            prepared_files,
            MappingProxyType(digests),
            MappingProxyType(generations),
        )
        self.current = result
        return result

    @staticmethod
    def _read_verified(artifact: ResolvedArtifact) -> tuple[bytes, Generation]:
        path = Path(artifact.cache_path)
        before = generation(path)
        content = path.read_bytes()
        after = generation(path)
        stable_generation = (
            before is not None and after is not None and before[2] >= 0 and after[2] >= 0
        )
        if (
            stable_generation
            and after != before
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            raise ArtifactError("artifact changed during preparation")
        return content, before
