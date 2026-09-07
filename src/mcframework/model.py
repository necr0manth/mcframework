"""Immutable domain objects shared by resolution, artifacts, and sync."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .paths import matches, normalize_relative_path

if TYPE_CHECKING:
    from .operations import MetadataRecord, Producer


def frozen_map(values: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(values or {}))


class Activation(StrEnum):
    AVAILABLE = "available"
    ENABLED = "enabled"
    DISABLED = "disabled"
    TRANSITIVE = "transitive"


class UpdatePolicy(StrEnum):
    FIXED = "fixed"
    LATEST_COMPATIBLE = "latest_compatible"


class SelectorKind(StrEnum):
    GLOB = "glob"
    EXACT = "exact"
    SUBTREE = "subtree"


@dataclass(frozen=True)
class IgnoreSelector:
    """A sync-ignore selector with explicit literal versus legacy-glob semantics."""

    kind: SelectorKind
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            normalize_relative_path(self.value, allow_glob=self.kind is SelectorKind.GLOB),
        )

    def contains(self, path: str) -> bool:
        candidate = normalize_relative_path(path)
        if self.kind is SelectorKind.GLOB:
            return matches(candidate, self.value)
        if self.kind is SelectorKind.EXACT:
            return candidate == self.value
        return candidate == self.value or candidate.startswith(self.value + "/")

    def document(self) -> dict[str, str]:
        return {"kind": self.kind.value, "path": self.value}


@dataclass(frozen=True)
class ParentReference:
    path: str
    parameters: Mapping[str, Any] = field(default_factory=frozen_map)


@dataclass(frozen=True)
class LayerDefinition:
    identity: str
    root: str
    parents: tuple[ParentReference, ...]
    producers: tuple[Producer, ...]
    payload: Mapping[str, bytes]


@dataclass(frozen=True)
class ArtifactSource:
    provider: str
    location: str
    sha256: str


@dataclass(frozen=True)
class PackageDeclaration:
    identity: str
    logical_identity: str
    kind: str
    source: ArtifactSource
    destination: str
    activation: Activation = Activation.AVAILABLE
    alias: str | None = None
    version: str | None = None
    update_policy: UpdatePolicy = UpdatePolicy.FIXED
    automatic_requires: tuple[str, ...] = ()
    layer_precedence: int = 0
    source_file: str | None = None
    source_index: int | None = None


@dataclass(frozen=True)
class Dependency:
    source: str
    target: str


@dataclass(frozen=True)
class Group:
    identity: str
    name: str | None = None
    description: str | None = None
    activation: Activation = Activation.AVAILABLE


@dataclass(frozen=True)
class Section:
    identity: str
    title: str
    members: tuple[str, ...] = ()
    order: int = 0


@dataclass(frozen=True)
class PackState:
    files: Mapping[str, bytes] = field(default_factory=frozen_map)
    exclusions: tuple[str, ...] = ()
    packages: Mapping[str, PackageDeclaration] = field(default_factory=frozen_map)
    dependencies: tuple[Dependency, ...] = ()
    groups: Mapping[str, Group] = field(default_factory=frozen_map)
    sections: Mapping[str, Section] = field(default_factory=frozen_map)
    prune: tuple[str, ...] = ()
    ignore: tuple[IgnoreSelector, ...] = ()

    @property
    def content(self) -> ContentState:
        """Files and exclusions consumed by the build/materializer subsystem."""
        return ContentState(self.files, self.exclusions, self.prune)

    @property
    def package_catalog(self) -> PackageState:
        """Package/dependency data consumed by resolver and artifact planning."""
        return PackageState(self.packages, self.dependencies, self.groups, self.sections)

    @property
    def web_policy(self) -> WebPolicyState:
        """Web/instance policy, intentionally excluded from materialized payloads."""
        return WebPolicyState(self.ignore)


@dataclass(frozen=True)
class ContentState:
    """The build-facing portion of a resolved pack state."""

    files: Mapping[str, bytes] = field(default_factory=frozen_map)
    exclusions: tuple[str, ...] = ()
    prune: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageState:
    """Package graph and UI grouping metadata used during resolution."""

    packages: Mapping[str, PackageDeclaration] = field(default_factory=frozen_map)
    dependencies: tuple[Dependency, ...] = ()
    groups: Mapping[str, Group] = field(default_factory=frozen_map)
    sections: Mapping[str, Section] = field(default_factory=frozen_map)


@dataclass(frozen=True)
class WebPolicyState:
    """Policies used by web/instance-diff code, not by build output."""

    ignore: tuple[IgnoreSelector, ...] = ()


@dataclass(frozen=True)
class ProducedOperationTrace:
    producer: str
    source: str
    operations: tuple[MetadataRecord, ...]


@dataclass(frozen=True)
class LayerApplicationTrace:
    layer: str
    parameters: Mapping[str, Any]
    producers: tuple[ProducedOperationTrace, ...]
    root: str = ""


@dataclass(frozen=True)
class ResolvedArtifact:
    declaration: PackageDeclaration
    cache_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    size: int
    package: str | None = None


@dataclass(frozen=True)
class Resolution:
    state: PackState
    active_packages: tuple[PackageDeclaration, ...]
    trace: tuple[LayerApplicationTrace, ...]
    artifacts: tuple[ResolvedArtifact, ...] = ()
    manifest: tuple[ManifestEntry, ...] = ()


def with_activation(package: PackageDeclaration, activation: Activation) -> PackageDeclaration:
    return replace(package, activation=activation)
