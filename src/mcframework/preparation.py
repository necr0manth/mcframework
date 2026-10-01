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
from .model import ManifestEntry, PackageDeclaration, Resolution, ResolvedArtifact, frozen_map
from .operations import PutFile
from .resolver import resolve_package_dependencies

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
        generations: dict[Path, Generation] = {}
        if previous is None:
            resolved, files = builder.prepare(resolution)
            for artifact in resolved.artifacts:
                content, stamp = self._read_verified(artifact)
                if content != files[artifact.declaration.destination]:
                    raise ArtifactError("artifact changed during preparation")
                generations[Path(artifact.cache_path)] = stamp
        else:
            old_artifacts = {
                item.declaration.identity: item for item in previous.resolution.artifacts
            }
            packages = dict(resolution.state.packages)
            # Provider-derived automatic requirements and selected metadata remain
            # useful while the source selector is still the same pinned selection.
            for identity, package in tuple(packages.items()):
                old = old_artifacts.get(identity)
                if old is not None and _same_selection(package, old.declaration):
                    packages[identity] = _merge_selection(old.declaration, package)
            catalog, active = resolve_package_dependencies(
                replace(resolution.state, packages=frozen_map(packages)).package_catalog,
                resolution.automatic_dependencies,
            )
            artifacts: dict[str, ResolvedArtifact] = {}
            package_contents: dict[str, bytes] = {}
            for _attempt in range(len(packages) + 1):
                pending = tuple(item for item in active if item.identity not in artifacts)
                if not pending:
                    break
                for package in pending:
                    old = old_artifacts.get(package.identity)
                    old_stamp = (
                        previous.generations.get(Path(old.cache_path)) if old is not None else None
                    )
                    if (
                        old is not None
                        and old_stamp is not None
                        and generation(Path(old.cache_path)) == old_stamp
                        and _same_selection(package, old.declaration)
                    ):
                        artifact = replace(
                            old, declaration=_merge_selection(old.declaration, package)
                        )
                        content = previous.files[artifact.declaration.destination]
                        generations[Path(old.cache_path)] = old_stamp
                    else:
                        artifact = builder.artifacts.resolve_packages((package,))[0]
                        content, stamp = self._read_verified(artifact)
                        generations[Path(artifact.cache_path)] = stamp
                    artifacts[package.identity] = artifact
                    package_contents[package.identity] = content
                    packages[package.identity] = artifact.declaration
                catalog = replace(catalog, packages=frozen_map(packages))
                catalog, active = resolve_package_dependencies(
                    catalog, resolution.automatic_dependencies
                )
                packages = dict(catalog.packages)
            else:
                raise ArtifactError("package dependency closure did not converge")

            resolved_artifacts = tuple(
                replace(artifacts[package.identity], declaration=package)
                for package in active
                if package.identity in artifacts
            )
            builder.artifacts.hydrate_cached_disabled_destinations(packages)
            resolved_state = replace(
                resolution.state,
                packages=frozen_map(packages),
                groups=catalog.groups,
            )
            resolved = replace(
                resolution,
                state=resolved_state,
                active_packages=active,
                artifacts=resolved_artifacts,
            )
            contents = dict(content_state.files)
            entries: list[ManifestEntry] = []
            for path, content in content_state.files.items():
                entries.append(
                    ManifestEntry(path, content_digest(content, previous.digests), len(content))
                )
            for package in active:
                path = package.destination
                # This check is required even when every byte can be reused.
                if path in contents:
                    raise MergeConflict(
                        f"package destination conflicts with generated file: {path!r}"
                    )
                artifact = artifacts[package.identity]
                content = package_contents[package.identity]
                contents[path] = content
                entries.append(
                    ManifestEntry(path, artifact.sha256, artifact.size, package.identity)
                )
            manifest = tuple(sorted(entries, key=lambda item: item.path))
            if manifest == previous.resolution.manifest:
                manifest = previous.resolution.manifest
            resolved = replace(resolved, manifest=manifest)
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


def _same_selection(current: PackageDeclaration, old: PackageDeclaration) -> bool:
    if (
        current.identity != old.identity
        or current.source.provider != old.source.provider
        or current.source.location != old.source.location
        or current.kind != old.kind
        or current.version != old.version
        or (current.destination and current.destination != old.destination)
        or (current.source.sha256 and current.source.sha256 != old.source.sha256)
        or current.minecraft != old.minecraft
        or current.loader != old.loader
    ):
        return False
    if current.logical_identity_explicit and current.logical_identity != old.logical_identity:
        return False
    if current.automatic_requires_explicit and current.automatic_requires != old.automatic_requires:
        return False
    if current.source.provider == "local":
        return bool(current.source.sha256 and current.source.sha256 == old.source.sha256)
    if current.source.provider == "direct_url":
        return bool(current.source.sha256 and current.source.sha256 == old.source.sha256)
    return bool(current.version)


def _merge_selection(
    selected: PackageDeclaration, current: PackageDeclaration
) -> PackageDeclaration:
    return replace(
        selected,
        identity=current.identity,
        logical_identity=(
            current.logical_identity
            if current.logical_identity_explicit
            else selected.logical_identity
        ),
        kind=current.kind,
        destination=current.destination or selected.destination,
        activation=current.activation,
        alias=current.alias,
        update_policy=current.update_policy,
        automatic_requires=(
            current.automatic_requires
            if current.automatic_requires_explicit
            else selected.automatic_requires
        ),
        layer_precedence=current.layer_precedence,
        source_file=current.source_file,
        source_index=current.source_index,
        minecraft=current.minecraft or selected.minecraft,
        loader=current.loader or selected.loader,
        logical_identity_explicit=current.logical_identity_explicit,
        automatic_requires_explicit=current.automatic_requires_explicit,
    )
