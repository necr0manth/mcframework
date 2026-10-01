"""Exact-hash artifact providers and content-addressed cache."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import ArtifactError, UpdateError
from .model import (
    Activation,
    ArtifactSource,
    PackageDeclaration,
    Resolution,
    ResolvedArtifact,
    frozen_map,
)
from .paths import atomic_write, normalize_relative_path

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ArtifactProvider(Protocol):
    def fetch(self, package: PackageDeclaration) -> bytes: ...


@dataclass(frozen=True)
class PackageUpdate:
    source: ArtifactSource
    version: str | None = None
    artifact_url: str | None = None
    filename: str | None = None
    sha256: str | None = None
    logical_identity: str | None = None
    automatic_requires: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PackageSelection:
    """One exact provider file chosen for a declaration."""

    selector: str
    download_url: str
    filename: str
    sha256: str | None = None
    logical_identity: str | None = None
    automatic_requires: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ExactMatchCacheSeed:
    artifact: ResolvedArtifact
    selection_path: Path
    previous_selection: bytes | None
    seeded_selection: bytes


@runtime_checkable
class PackageSelectingProvider(Protocol):
    def select(self, package: PackageDeclaration) -> PackageSelection: ...


@runtime_checkable
class UpdateCapableProvider(Protocol):
    def find_update(self, package: PackageDeclaration) -> PackageUpdate | None: ...


class LocalArtifactProvider:
    def __init__(self, base_directory: Path | str):
        self.base_directory = Path(base_directory).resolve()

    def fetch(self, package: PackageDeclaration) -> bytes:
        path = Path(package.source.location)
        if not path.is_absolute():
            declaration_path = self._declaration_root(package) / path
            workspace_path = self.base_directory / path
            # Layer-relative is the authored-operation contract. Keep the older
            # workspace-relative lookup as a fallback for generated declarations
            # that store an already workspace-relative artifact path.
            path = declaration_path if declaration_path.is_file() else workspace_path
        path = path.resolve()
        if not path.is_file():
            raise ArtifactError(
                f"local artifact does not exist: {path} (declaration: {package.source_file})"
            )
        return path.read_bytes()

    def _declaration_root(self, package: PackageDeclaration) -> Path:
        """Use the owning layer as the base for local package source paths."""
        if package.source_file is not None:
            source_file = Path(package.source_file)
            if not source_file.is_absolute():
                source_file = self.base_directory / source_file
            parent = source_file.resolve().parent
            fallback = None
            while parent != parent.parent:
                if parent.name == ".pack":
                    fallback = fallback or parent.parent
                metadata = parent / ".pack"
                if any(
                    (metadata / marker).is_file()
                    for marker in ("layer.mcpack", "layer.json")
                ):
                    return parent
                parent = parent.parent
            if fallback is not None:
                return fallback
        return self.base_directory


class DirectUrlArtifactProvider:
    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout

    def fetch(self, package: PackageDeclaration) -> bytes:
        validate_sha256(package.source.sha256)
        try:
            with urllib.request.urlopen(
                package.artifact_url or package.source.location, timeout=self.timeout
            ) as response:
                return bytes(response.read())
        except OSError as exc:
            raise ArtifactError(
                f"cannot download exact artifact for {package.identity!r}: {exc}"
            ) from exc


class ArtifactRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ArtifactProvider] = {}

    def register(self, name: str, provider: ArtifactProvider) -> None:
        self._providers[name] = provider

    def provider(self, name: str) -> ArtifactProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ArtifactError(f"artifact provider is not registered: {name!r}") from exc


class ArtifactCache:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()

    def obtain(self, package: PackageDeclaration, provider: ArtifactProvider) -> ResolvedArtifact:
        if not package.source.sha256:
            return self.store(package, provider.fetch(package))
        expected = validate_sha256(package.source.sha256)
        hash_root = (self.root / "sha256").resolve()
        path = (hash_root / expected[:2] / expected).resolve()
        if not path.is_relative_to(hash_root):
            raise ArtifactError("artifact cache path escapes the cache root")
        if path.is_file():
            content = path.read_bytes()
            actual = hashlib.sha256(content).hexdigest()
            if actual == expected:
                return ResolvedArtifact(package, str(path), actual, len(content))
            path.unlink()
        content = provider.fetch(package)
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise ArtifactError(
                f"hash mismatch for {package.identity!r}: expected {expected}, got {actual}"
            )
        return self.store(package, content, expected=expected)

    def store(
        self, package: PackageDeclaration, content: bytes, *, expected: str | None = None
    ) -> ResolvedArtifact:
        actual = hashlib.sha256(content).hexdigest()
        if expected is not None and actual != validate_sha256(expected):
            raise ArtifactError(
                f"hash mismatch for {package.identity!r}: expected {expected}, got {actual}"
            )
        hash_root = (self.root / "sha256").resolve()
        path = (hash_root / actual[:2] / actual).resolve()
        if not path.is_relative_to(hash_root):
            raise ArtifactError("artifact cache path escapes the cache root")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != actual:
            atomic_write(path, content)
        declaration = replace(package, source=replace(package.source, sha256=actual))
        return ResolvedArtifact(declaration, str(path), actual, len(content))

    def load_selection(self, package: PackageDeclaration) -> PackageSelection | None:
        if not package.version:
            return None
        path = self._selection_path(package, package.version)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("selection data is not an object")
            requirements = raw.get("automatic_requires")
            if requirements is not None and (
                not isinstance(requirements, list)
                or not all(isinstance(item, str) for item in requirements)
            ):
                raise ValueError("selection dependency metadata is invalid")
            selection = PackageSelection(
                raw["selector"],
                raw["download_url"],
                raw["filename"],
                raw["sha256"],
                raw.get("logical_identity"),
                tuple(requirements) if requirements is not None else None,
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ArtifactError(
                f"cached provider selection is invalid for {package.identity!r}"
            ) from exc
        if (
            not isinstance(selection.selector, str)
            or selection.selector != package.version
            or not isinstance(selection.download_url, str)
            or urllib.parse.urlsplit(selection.download_url).scheme != "https"
            or not urllib.parse.urlsplit(selection.download_url).netloc
            or not isinstance(selection.filename, str)
            or not selection.filename
            or not isinstance(selection.sha256, str)
            or SHA256_PATTERN.fullmatch(selection.sha256) is None
            or not isinstance(selection.logical_identity, (str, type(None)))
            or (isinstance(selection.logical_identity, str) and not selection.logical_identity)
            or not isinstance(selection.automatic_requires, (tuple, type(None)))
            or (
                selection.automatic_requires is not None
                and not all(isinstance(item, str) for item in selection.automatic_requires)
            )
        ):
            raise ArtifactError(f"cached provider selection is invalid for {package.identity!r}")
        return selection

    def store_selection(
        self, package: PackageDeclaration, selection: PackageSelection, sha256: str
    ) -> None:
        if not isinstance(selection.download_url, str):
            raise ArtifactError("provider selection is missing its exact download URL")
        download = urllib.parse.urlsplit(selection.download_url)
        if (
            not isinstance(selection.selector, str)
            or not selection.selector
            or download.scheme != "https"
            or not download.netloc
            or not isinstance(selection.filename, str)
            or not selection.filename
        ):
            raise ArtifactError("provider selection is missing its exact file details")
        record = {
            "selector": selection.selector,
            "download_url": selection.download_url,
            "filename": selection.filename,
            "sha256": validate_sha256(sha256),
            "logical_identity": selection.logical_identity,
            "automatic_requires": list(selection.automatic_requires)
            if selection.automatic_requires is not None
            else None,
        }
        path = self._selection_path(package, selection.selector)
        atomic_write(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))

    def seed_exact_match(
        self, package: PackageDeclaration, selection: PackageSelection, content: bytes
    ) -> ExactMatchCacheSeed:
        """Cache confirmed bytes and provider selection for an imported exact match."""
        if package.version != selection.selector:
            raise ArtifactError("exact match selector does not match the package declaration")
        actual = hashlib.sha256(content).hexdigest()
        if selection.sha256 is not None and validate_sha256(selection.sha256) != actual:
            raise ArtifactError("exact match hash does not match imported package bytes")
        selection = replace(selection, sha256=actual)
        path = self._selection_path(package, selection.selector)
        previous = path.read_bytes() if path.is_file() else None
        if previous is not None:
            existing = self.load_selection(package)
            if existing is None or (
                existing.selector,
                existing.download_url,
                existing.filename,
                existing.sha256,
            ) != (
                selection.selector,
                selection.download_url,
                selection.filename,
                selection.sha256,
            ):
                raise ArtifactError(
                    "conflicting exact provider selection is already cached for this package"
                )
        artifact = self.store(package, content, expected=actual)
        if previous is None:
            self.store_selection(package, selection, actual)
        seeded = path.read_bytes()
        return ExactMatchCacheSeed(artifact, path, previous, seeded)

    def restore_exact_match_seed(self, seed: ExactMatchCacheSeed) -> None:
        """Restore the provider selection before-image after an import rollback."""
        root = (self.root / "selections").resolve()
        path = seed.selection_path.resolve()
        if not path.is_relative_to(root):
            raise ArtifactError("provider selection rollback path escapes the cache")
        current = path.read_bytes() if path.is_file() else None
        if current != seed.seeded_selection:
            raise ArtifactError("provider selection changed before import rollback")
        if seed.previous_selection is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, seed.previous_selection)

    def _selection_path(self, package: PackageDeclaration, selector: str) -> Path:
        key = json.dumps(
            [
                package.source.provider,
                package.source.location,
                selector,
                package.kind,
                package.minecraft,
                package.loader,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(key).hexdigest()
        root = (self.root / "selections").resolve()
        path = (root / digest[:2] / f"{digest}.json").resolve()
        if not path.is_relative_to(root):
            raise ArtifactError("provider selection cache path escapes the cache root")
        return path


class ArtifactResolver:
    def __init__(self, registry: ArtifactRegistry, cache: ArtifactCache):
        self.registry = registry
        self.cache = cache

    def resolve(self, resolution: Resolution) -> Resolution:
        """Compatibility adapter for callers holding the composed resolution."""
        if not resolution.state.packages and not resolution.active_packages:
            return replace(resolution, artifacts=self.resolve_packages(()))
        if resolution.artifacts and {
            artifact.declaration.identity for artifact in resolution.artifacts
        } == {package.identity for package in resolution.active_packages}:
            verified = self.resolve_packages(resolution.active_packages)
            resolved = _resolved_packages(resolution, verified)
            packages = dict(resolved.state.packages)
            self.hydrate_cached_disabled_destinations(packages)
            return replace(
                resolved,
                state=replace(resolved.state, packages=frozen_map(packages)),
            )
        from .resolver import resolve_package_dependencies

        packages = dict(resolution.state.packages)
        artifacts: dict[str, ResolvedArtifact] = {}
        catalog, active = resolve_package_dependencies(
            replace(resolution.state, packages=frozen_map(packages)).package_catalog,
            resolution.automatic_dependencies,
        )
        for _attempt in range(len(packages) + 1):
            pending = tuple(item for item in active if item.identity not in artifacts)
            if not pending:
                break
            for artifact in self.resolve_packages(pending):
                artifacts[artifact.declaration.identity] = artifact
                packages[artifact.declaration.identity] = artifact.declaration
            catalog = replace(catalog, packages=frozen_map(packages))
            catalog, active = resolve_package_dependencies(
                catalog, resolution.automatic_dependencies
            )
            packages = dict(catalog.packages)
        else:
            raise ArtifactError("package dependency closure did not converge")

        final_artifacts = tuple(
            replace(artifacts[package.identity], declaration=package)
            for package in active
            if package.identity in artifacts
        )
        self.hydrate_cached_disabled_destinations(packages)
        state = replace(
            resolution.state,
            packages=frozen_map(packages),
            groups=catalog.groups,
        )
        return replace(
            resolution,
            state=state,
            active_packages=active,
            artifacts=final_artifacts,
        )

    def hydrate_cached_disabled_destinations(
        self, packages: dict[str, PackageDeclaration]
    ) -> None:
        """Hydrate cached destinations for inactive JAR packages without resolving them."""
        for identity, package in packages.items():
            if (
                package.activation not in {Activation.AVAILABLE, Activation.DISABLED}
                or package.kind != "mod"
                or package.destination
                or not package.version
            ):
                continue
            selection = self.cache.load_selection(package)
            if selection is None:
                continue
            filename = posixpath.basename(selection.filename.replace("\\", "/"))
            if not filename:
                raise ArtifactError(
                    f"cached provider returned an invalid filename for {package.identity!r}"
                )
            packages[identity] = replace(
                package,
                filename=selection.filename,
                destination=normalize_relative_path(
                    f"{package_kind_directory(package.kind)}/{filename}"
                ),
            )

    def resolve_packages(
        self, packages: tuple[PackageDeclaration, ...]
    ) -> tuple[ResolvedArtifact, ...]:
        """Resolve package artifacts without access to layer, content, or web state."""
        results = []
        for package in packages:
            provider = self.registry.provider(package.source.provider)
            if isinstance(provider, PackageSelectingProvider):
                selection = self.cache.load_selection(package) or provider.select(package)
                selected = replace(
                    package,
                    version=selection.selector,
                    artifact_url=selection.download_url,
                    filename=selection.filename,
                    logical_identity=(
                        package.logical_identity
                        if package.logical_identity_explicit
                        else selection.logical_identity or package.logical_identity
                    ),
                    automatic_requires=(
                        package.automatic_requires
                        if package.automatic_requires_explicit
                        else selection.automatic_requires or package.automatic_requires
                    ),
                )
                if not selected.destination:
                    filename = posixpath.basename(selection.filename.replace("\\", "/"))
                    if not filename:
                        raise ArtifactError(
                            f"provider returned an invalid filename for {package.identity!r}"
                        )
                    selected = replace(
                        selected,
                        destination=normalize_relative_path(
                            f"{package_kind_directory(selected.kind)}/{filename}"
                        ),
                    )
                if (
                    package.source.sha256
                    and selection.sha256
                    and validate_sha256(package.source.sha256) != validate_sha256(selection.sha256)
                ):
                    raise ArtifactError(
                        f"provider hash conflicts with package hash for {package.identity!r}"
                    )
                expected = selection.sha256 or package.source.sha256
                if expected:
                    try:
                        artifact = self.cache.obtain(
                            replace(selected, source=replace(selected.source, sha256=expected)),
                            provider,
                        )
                    except (ArtifactError, OSError) as fetch_error:
                        if self.cache.load_selection(package) is None:
                            raise
                        refreshed = provider.select(package)
                        if (
                            refreshed.selector != selection.selector
                            or refreshed.filename != selection.filename
                        ):
                            raise ArtifactError(
                                f"provider selection drift for pinned package {package.identity!r}"
                            ) from fetch_error
                        selected = replace(selected, artifact_url=refreshed.download_url)
                        if refreshed.sha256 is not None and validate_sha256(
                            refreshed.sha256
                        ) != validate_sha256(expected):
                            raise ArtifactError(
                                f"provider hash changed for pinned package {package.identity!r}"
                            ) from fetch_error
                        artifact = self.cache.store(
                            selected,
                            provider.fetch(selected),
                            expected=expected,
                        )
                        selection = replace(
                            refreshed,
                            logical_identity=selection.logical_identity,
                            automatic_requires=selection.automatic_requires,
                        )
                else:
                    artifact = self.cache.store(selected, provider.fetch(selected))
                self.cache.store_selection(selected, selection, artifact.sha256)
                results.append(artifact)
            else:
                results.append(self.cache.obtain(package, provider))
        return tuple(results)


def explicit_update(
    package: PackageDeclaration, registry: ArtifactRegistry
) -> PackageDeclaration | None:
    if package.update_policy.value == "fixed":
        raise UpdateError(f"package {package.identity!r} has fixed update policy")
    provider = registry.provider(package.source.provider)
    if not isinstance(provider, UpdateCapableProvider):
        raise UpdateError(f"provider {package.source.provider!r} does not expose update capability")
    update = provider.find_update(package)
    if update is None:
        return None
    digest_value = update.sha256 or update.source.sha256
    if digest_value:
        try:
            digest = validate_sha256(digest_value)
        except ArtifactError as exc:
            raise UpdateError("update provider returned an invalid SHA-256") from exc
    elif update.artifact_url is not None:
        digest = ""
    else:
        raise UpdateError("update provider returned neither a SHA-256 nor a download URL")
    return replace(
        package,
        source=replace(update.source, sha256=digest),
        version=update.version,
        artifact_url=update.artifact_url,
        filename=update.filename,
        logical_identity=update.logical_identity or package.logical_identity,
        automatic_requires=update.automatic_requires or package.automatic_requires,
    )


def package_kind_directory(kind: str) -> str:
    return {
        "mod": "mods",
        "resourcepack": "resourcepacks",
        "shaderpack": "shaderpacks",
        "datapack": "datapacks",
    }[kind]


def _resolved_packages(
    resolution: Resolution, artifacts: tuple[ResolvedArtifact, ...]
) -> Resolution:
    packages = dict(resolution.state.packages)
    for artifact in artifacts:
        packages[artifact.declaration.identity] = artifact.declaration
    active = tuple(packages[artifact.declaration.identity] for artifact in artifacts)
    state = replace(resolution.state, packages=frozen_map(packages))
    return replace(resolution, state=state, active_packages=active, artifacts=artifacts)


def validate_sha256(value: str) -> str:
    """Require the one canonical representation used as a cache key."""
    if SHA256_PATTERN.fullmatch(value) is None:
        raise ArtifactError("SHA-256 must be exactly 64 lowercase hexadecimal digits")
    return value
