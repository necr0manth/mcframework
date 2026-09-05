"""Typed operations, effect domains, and same-layer delta merging."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .errors import MergeConflict, ResolutionError
from .model import (
    Activation,
    ArtifactSource,
    Dependency,
    Group,
    IgnoreSelector,
    PackageDeclaration,
    PackState,
    Section,
    SelectorKind,
    UpdatePolicy,
    frozen_map,
)
from .paths import matches, normalize_relative_path


@dataclass(frozen=True)
class Resource:
    kind: str
    identity: tuple[str, ...]

    def label(self) -> str:
        return f"{self.kind}({', '.join(self.identity)})"


@dataclass(frozen=True)
class ResourceDomain:
    kind: str
    identity: tuple[str, ...]

    def contains(self, resource: Resource) -> bool:
        if self.kind != resource.kind or len(self.identity) != len(resource.identity):
            return False
        for allowed, concrete in zip(self.identity, resource.identity, strict=True):
            if allowed == concrete:
                continue
            if any(char in allowed for char in "*?["):
                if not matches(concrete, allowed):
                    return False
            else:
                return False
        return True


class Operation(Protocol):
    def writes(self) -> tuple[Resource, ...]: ...

    def reads(self) -> tuple[Resource, ...]: ...


@dataclass(frozen=True)
class PutFile:
    path: str
    content: bytes

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("file", (self.path,)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class RemoveFile:
    path: str

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("file", (self.path,)),)

    def reads(self) -> tuple[Resource, ...]:
        return (Resource("file", (self.path,)),)


@dataclass(frozen=True)
class RemovePaths:
    pattern: str

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("file_glob", (self.pattern,)),)

    def reads(self) -> tuple[Resource, ...]:
        return (Resource("file_glob", (self.pattern,)),)


@dataclass(frozen=True)
class PatchProperties:
    path: str
    set_values: Mapping[str, str] = field(default_factory=frozen_map)
    remove_keys: tuple[str, ...] = ()

    def writes(self) -> tuple[Resource, ...]:
        return tuple(
            Resource("property", (self.path, key))
            for key in (*self.set_values.keys(), *self.remove_keys)
        )

    def reads(self) -> tuple[Resource, ...]:
        return (Resource("file", (self.path,)),)


@dataclass(frozen=True)
class DeclarePackage:
    package: PackageDeclaration

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("package", (self.package.identity,)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class SetPackageActivation:
    identity: str
    activation: Activation

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("activation", (self.identity,)),)

    def reads(self) -> tuple[Resource, ...]:
        return (Resource("package", (self.identity,)),)


@dataclass(frozen=True)
class AddDependency:
    dependency: Dependency

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("dependency", (self.dependency.source, self.dependency.target)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class DeclareGroup:
    group: Group

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("group", (self.group.identity,)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class DeclareSection:
    section: Section

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("section", (self.section.identity,)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class AddPruneSelector:
    pattern: str

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("sync", ("prune", self.pattern)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class AddIgnoreSelector:
    selector: IgnoreSelector

    @property
    def pattern(self) -> str:
        """Compatibility accessor for callers that only handled legacy globs."""
        return self.selector.value

    def writes(self) -> tuple[Resource, ...]:
        return (Resource("sync", ("ignore", self.selector.kind.value, self.selector.value)),)

    def reads(self) -> tuple[Resource, ...]:
        return ()


@dataclass(frozen=True)
class Producer:
    name: str
    source: str
    operations: tuple[Operation, ...]
    may_write: tuple[ResourceDomain, ...]
    may_read: tuple[ResourceDomain, ...] | None = None

    def produce(self, parameters: Mapping[str, Any]) -> tuple[Operation, ...]:
        # JSON metadata is static in the initial frontend. The parameter argument is
        # retained so bounded parametric producers can be added without changing the core.
        del parameters
        return self.operations

    def validate(self, operations: Iterable[Operation]) -> None:
        for operation in operations:
            for resource in operation.writes():
                if not any(domain.contains(resource) for domain in self.may_write):
                    raise ResolutionError(
                        f"producer {self.name!r} writes outside may_write: {resource.label()}"
                    )
            if self.may_read is not None:
                for resource in operation.reads():
                    if not any(domain.contains(resource) for domain in self.may_read):
                        raise ResolutionError(
                            f"producer {self.name!r} reads outside may_read: {resource.label()}"
                        )


def operation_from_json(
    raw: Mapping[str, Any], *, source: str, index: int, layer_precedence: int = 0
) -> Operation:
    kind = required_string(raw, "type", source)
    if kind == "put":
        path = normalize_relative_path(required_string(raw, "path", source))
        if "content_base64" in raw:
            try:
                content = base64.b64decode(
                    required_string(raw, "content_base64", source), validate=True
                )
            except ValueError as exc:
                raise ResolutionError(f"{source}: invalid content_base64") from exc
        else:
            content = required_string(raw, "content", source).encode("utf-8")
        return PutFile(path, content)
    if kind == "remove":
        return RemoveFile(normalize_relative_path(required_string(raw, "path", source)))
    if kind == "remove_paths":
        return RemovePaths(
            normalize_relative_path(required_string(raw, "glob", source), allow_glob=True)
        )
    if kind == "patch_properties":
        path = normalize_relative_path(required_string(raw, "path", source))
        values = raw.get("set", {})
        removed = raw.get("remove", [])
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, (str, int, float, bool))
            for key, value in values.items()
        ):
            raise ResolutionError(f"{source}: patch_properties.set must be an object of scalars")
        if not isinstance(removed, list) or not all(isinstance(item, str) for item in removed):
            raise ResolutionError(f"{source}: patch_properties.remove must be a string array")
        return PatchProperties(
            path,
            frozen_map({key: str(value) for key, value in values.items()}),
            tuple(removed),
        )
    if kind == "package":
        package_id = required_string(raw, "id", source)
        source_raw = raw.get("source")
        if not isinstance(source_raw, dict):
            raise ResolutionError(f"{source}: package source must be an object")
        provider = required_string(source_raw, "provider", source)
        location = required_string(source_raw, "location", source)
        sha256 = required_string(source_raw, "sha256", source).lower()
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ResolutionError(f"{source}: package sha256 must be 64 lowercase hex digits")
        activation = parse_activation(raw.get("activation", "available"), source)
        try:
            policy = UpdatePolicy(str(raw.get("update_policy", "fixed")).lower())
        except ValueError as exc:
            raise ResolutionError(f"{source}: invalid update_policy") from exc
        auto = raw.get("automatic_requires", [])
        if not isinstance(auto, list) or not all(isinstance(item, str) for item in auto):
            raise ResolutionError(f"{source}: automatic_requires must be a string array")
        return DeclarePackage(
            PackageDeclaration(
                identity=package_id,
                logical_identity=str(raw.get("logical_id", package_id)),
                kind=str(raw.get("kind", "mod")),
                source=ArtifactSource(provider, location, sha256),
                destination=normalize_relative_path(required_string(raw, "destination", source)),
                activation=activation,
                alias=str(raw["alias"]) if "alias" in raw else None,
                version=str(raw["version"]) if "version" in raw else None,
                update_policy=policy,
                automatic_requires=tuple(auto),
                layer_precedence=layer_precedence,
                source_file=source,
                source_index=index,
            )
        )
    if kind == "activation":
        return SetPackageActivation(
            required_string(raw, "id", source), parse_activation(raw.get("state"), source)
        )
    if kind == "dependency":
        return AddDependency(
            Dependency(
                required_string(raw, "source", source),
                required_string(raw, "target", source),
            )
        )
    if kind == "group":
        group_id = required_string(raw, "id", source)
        return DeclareGroup(
            Group(
                group_id,
                str(raw["name"]) if "name" in raw else None,
                str(raw["description"]) if "description" in raw else None,
                parse_activation(raw.get("activation", "available"), source),
            )
        )
    if kind == "section":
        members = raw.get("members", [])
        if not isinstance(members, list) or not all(isinstance(item, str) for item in members):
            raise ResolutionError(f"{source}: section members must be a string array")
        return DeclareSection(
            Section(
                required_string(raw, "id", source),
                required_string(raw, "title", source),
                tuple(members),
                int(raw.get("order", 0)),
            )
        )
    if kind == "prune":
        pattern = normalize_relative_path(required_string(raw, "glob", source), allow_glob=True)
        return AddPruneSelector(pattern)
    if kind == "ignore":
        selector_raw = raw.get("selector")
        if selector_raw is None:
            selector = IgnoreSelector(
                SelectorKind.GLOB,
                normalize_relative_path(required_string(raw, "glob", source), allow_glob=True),
            )
        else:
            if not isinstance(selector_raw, dict) or set(selector_raw) != {"kind", "path"}:
                raise ResolutionError(
                    f"{source}: ignore.selector must contain exactly kind and path"
                )
            try:
                selector_kind = SelectorKind(required_string(selector_raw, "kind", source))
            except ValueError as exc:
                raise ResolutionError(f"{source}: invalid ignore selector kind") from exc
            selector = IgnoreSelector(
                selector_kind,
                normalize_relative_path(
                    required_string(selector_raw, "path", source),
                    allow_glob=selector_kind is SelectorKind.GLOB,
                ),
            )
        return AddIgnoreSelector(selector)
    raise ResolutionError(f"{source}: unsupported operation type {kind!r}")


def required_string(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ResolutionError(f"{source}: {key!r} must be a non-empty string")
    return value


def parse_activation(value: Any, source: str) -> Activation:
    try:
        result = Activation(str(value).lower())
    except ValueError as exc:
        raise ResolutionError(f"{source}: invalid activation {value!r}") from exc
    if result is Activation.TRANSITIVE:
        raise ResolutionError(f"{source}: transitive activation is resolver-owned")
    return result


def producer_for_operations(name: str, source: str, operations: tuple[Operation, ...]) -> Producer:
    return Producer(
        name=name,
        source=source,
        operations=operations,
        may_write=tuple(
            ResourceDomain(resource.kind, resource.identity)
            for operation in operations
            for resource in operation.writes()
        ),
        may_read=tuple(
            ResourceDomain(resource.kind, resource.identity)
            for operation in operations
            for resource in operation.reads()
        ),
    )


def apply_local_operations(
    parent: PackState, operations: tuple[Operation, ...], precedence: int
) -> PackState:
    """Merge operations as an unordered local delta, then apply it to parent."""
    validate_same_layer(operations)
    files = dict(parent.files)
    exclusions = list(parent.exclusions)
    packages = dict(parent.packages)
    dependencies = list(parent.dependencies)
    groups = dict(parent.groups)
    sections = dict(parent.sections)
    prune = list(parent.prune)
    ignore = list(parent.ignore)

    # Every structured patch reads the unmodified parent state.
    patched: dict[str, bytes] = {}
    grouped_patches: dict[str, list[PatchProperties]] = {}
    for operation in operations:
        if isinstance(operation, PatchProperties):
            grouped_patches.setdefault(operation.path, []).append(operation)
    for path, patches in grouped_patches.items():
        if path not in parent.files:
            raise ResolutionError(f"cannot patch missing inherited properties file {path!r}")
        properties = parse_properties(parent.files[path], path)
        for patch in patches:
            for key in patch.remove_keys:
                properties.pop(key, None)
            properties.update(patch.set_values)
        patched[path] = render_properties(properties)

    activation_operations = tuple(
        operation for operation in operations if isinstance(operation, SetPackageActivation)
    )
    for operation in activation_operations:
        if operation.identity not in parent.packages:
            raise ResolutionError(
                f"same-layer activation cannot target package absent from immutable "
                f"parent state: {operation.identity!r}; set activation on the declaration"
            )

    for operation in operations:
        if isinstance(operation, RemovePaths):
            exclusions.append(operation.pattern)
            for path in tuple(files):
                if matches(path, operation.pattern):
                    files.pop(path)
        elif isinstance(operation, RemoveFile):
            files.pop(operation.path, None)
        elif isinstance(operation, PutFile):
            if not any(matches(operation.path, pattern) for pattern in exclusions):
                files[operation.path] = operation.content
        elif isinstance(operation, PatchProperties):
            if not any(matches(operation.path, pattern) for pattern in exclusions):
                files[operation.path] = patched[operation.path]
        elif isinstance(operation, DeclarePackage):
            packages[operation.package.identity] = replace(
                operation.package, layer_precedence=precedence
            )
        elif isinstance(operation, SetPackageActivation):
            continue
        elif isinstance(operation, AddDependency):
            if operation.dependency not in dependencies:
                dependencies.append(operation.dependency)
        elif isinstance(operation, DeclareGroup):
            groups[operation.group.identity] = operation.group
        elif isinstance(operation, DeclareSection):
            sections[operation.section.identity] = operation.section
        elif isinstance(operation, AddPruneSelector) and operation.pattern not in prune:
            prune.append(operation.pattern)
        elif isinstance(operation, AddIgnoreSelector) and operation.selector not in ignore:
            ignore.append(operation.selector)
    # Activation operations read only the immutable parent state. Applying their
    # already-validated delta last makes the local merge independent of source order.
    for operation in activation_operations:
        packages[operation.identity] = replace(
            packages[operation.identity], activation=operation.activation
        )
    return PackState(
        frozen_map(files),
        tuple(exclusions),
        frozen_map(packages),
        tuple(dependencies),
        frozen_map(groups),
        frozen_map(sections),
        tuple(prune),
        tuple(ignore),
    )


def validate_same_layer(operations: tuple[Operation, ...]) -> None:
    seen: dict[Resource, object] = {}
    exact_files: list[tuple[str, Operation]] = []
    globs: list[RemovePaths] = []
    for operation in operations:
        if isinstance(operation, RemovePaths):
            globs.append(operation)
        if isinstance(operation, (PutFile, RemoveFile, PatchProperties)):
            exact_files.append((operation.path, operation))
        for resource in operation.writes():
            effect = write_effect(operation, resource)
            if resource not in seen:
                seen[resource] = effect
            elif seen[resource] != effect:
                raise MergeConflict(f"incompatible same-layer writes to {resource.label()}")
    for path, _operation in exact_files:
        for glob in globs:
            if matches(path, glob.pattern):
                raise MergeConflict(
                    f"same layer both writes {path!r} and excludes it with {glob.pattern!r}"
                )
    # File-level operations conflict even when their fine-grained resources differ.
    for index, (path, operation) in enumerate(exact_files):
        for other_path, other in exact_files[index + 1 :]:
            if path != other_path or operation == other:
                continue
            if isinstance(operation, PatchProperties) and isinstance(other, PatchProperties):
                continue
            raise MergeConflict(f"incompatible same-layer file operations for {path!r}")


def write_effect(operation: Operation, resource: Resource) -> object:
    """Describe the semantic value written to one concrete resource."""
    if isinstance(operation, PatchProperties) and resource.kind == "property":
        key = resource.identity[1]
        if key in operation.set_values:
            return ("set", operation.set_values[key])
        return ("remove",)
    return operation


def parse_properties(content: bytes, path: str) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResolutionError(f"properties file {path!r} is not UTF-8") from exc
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        separator = next((char for char in ("=", ":") if char in line), None)
        if separator is None:
            key, value = line, ""
        else:
            key, value = line.split(separator, 1)
        key = key.strip()
        if not key:
            raise ResolutionError(f"malformed properties file {path!r} at line {line_number}")
        result[key] = value.strip()
    return result


def render_properties(properties: Mapping[str, str]) -> bytes:
    return "".join(f"{key}={properties[key]}\n" for key in sorted(properties)).encode("utf-8")


def operation_to_json(
    operation: Operation, *, file_digest: Callable[[bytes], str] | None = None
) -> dict[str, Any]:
    """Stable diagnostic representation used by manifests and CLI output."""
    if isinstance(operation, PutFile):
        return {
            "type": "put",
            "path": operation.path,
            "sha256": (file_digest or _sha)(operation.content),
        }
    if isinstance(operation, RemoveFile):
        return {"type": "remove", "path": operation.path}
    if isinstance(operation, RemovePaths):
        return {"type": "remove_paths", "glob": operation.pattern}
    if isinstance(operation, PatchProperties):
        return {
            "type": "patch_properties",
            "path": operation.path,
            "set": dict(operation.set_values),
            "remove": list(operation.remove_keys),
        }
    if isinstance(operation, DeclarePackage):
        return {"type": "package", "id": operation.package.identity}
    if isinstance(operation, SetPackageActivation):
        return {"type": "activation", "id": operation.identity, "state": operation.activation}
    if isinstance(operation, AddDependency):
        return {
            "type": "dependency",
            "source": operation.dependency.source,
            "target": operation.dependency.target,
        }
    if isinstance(operation, DeclareGroup):
        return {"type": "group", "id": operation.group.identity}
    if isinstance(operation, DeclareSection):
        return {"type": "section", "id": operation.section.identity}
    if isinstance(operation, AddPruneSelector):
        return {"type": "prune", "glob": operation.pattern}
    if isinstance(operation, AddIgnoreSelector):
        if operation.selector.kind is SelectorKind.GLOB:
            return {"type": "ignore", "glob": operation.selector.value}
        return {"type": "ignore", "selector": operation.selector.document()}
    raise TypeError(f"unsupported operation: {operation!r}")


def _sha(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()
