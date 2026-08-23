"""Exact-hash artifact providers and content-addressed cache."""

from __future__ import annotations

import hashlib
import re
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import ArtifactError, UpdateError
from .model import ArtifactSource, PackageDeclaration, Resolution, ResolvedArtifact
from .paths import atomic_write

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ArtifactProvider(Protocol):
    def fetch(self, package: PackageDeclaration) -> bytes: ...


@dataclass(frozen=True)
class PackageUpdate:
    source: ArtifactSource
    version: str | None = None


@runtime_checkable
class UpdateCapableProvider(Protocol):
    def find_update(self, package: PackageDeclaration) -> PackageUpdate | None: ...


class LocalArtifactProvider:
    def __init__(self, base_directory: Path | str):
        self.base_directory = Path(base_directory).resolve()

    def fetch(self, package: PackageDeclaration) -> bytes:
        validate_sha256(package.source.sha256)
        path = Path(package.source.location)
        if not path.is_absolute():
            path = self.base_directory / path
        path = path.resolve()
        if not path.is_file():
            raise ArtifactError(f"local artifact does not exist: {path}")
        return path.read_bytes()


class DirectUrlArtifactProvider:
    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout

    def fetch(self, package: PackageDeclaration) -> bytes:
        validate_sha256(package.source.sha256)
        try:
            with urllib.request.urlopen(package.source.location, timeout=self.timeout) as response:
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
        atomic_write(path, content)
        return ResolvedArtifact(package, str(path), actual, len(content))


class ArtifactResolver:
    def __init__(self, registry: ArtifactRegistry, cache: ArtifactCache):
        self.registry = registry
        self.cache = cache

    def resolve(self, resolution: Resolution) -> Resolution:
        artifacts = tuple(
            self.cache.obtain(package, self.registry.provider(package.source.provider))
            for package in resolution.active_packages
        )
        return replace(resolution, artifacts=artifacts)


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
    try:
        digest = validate_sha256(update.source.sha256)
    except ArtifactError as exc:
        raise UpdateError("update provider returned an invalid SHA-256") from exc
    return replace(
        package,
        source=replace(update.source, sha256=digest),
        version=update.version,
    )


def validate_sha256(value: str) -> str:
    """Require the one canonical representation used as a cache key."""
    if SHA256_PATTERN.fullmatch(value) is None:
        raise ArtifactError("SHA-256 must be exactly 64 lowercase hexadecimal digits")
    return value
