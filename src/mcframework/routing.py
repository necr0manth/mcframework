"""Format-independent write policy; it never mutates resolved pack content."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any

from .errors import MetadataError, ResolutionError, UnsafePathError
from .metadata import metadata_local_root, normalize_metadata_destination
from .model import frozen_map
from .paths import matches, normalize_relative_path

_PACKAGE_FIELDS = {"id", "kind", "provider", "origin", "tags", "categories", "activation"}
_PREDICATES = {
    "type", "operation_types", "path", "destination", "glob", "state", "activation",
    "origin", "provider", "tags", "categories", "package",
    *(f"package.{key}" for key in _PACKAGE_FIELDS),
}


def _known_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ResolutionError(f"{label}: unsupported fields: {sorted(unknown)}")


def _validate_predicate(key: str, value: Any, label: str) -> None:
    if key == "package":
        if not isinstance(value, Mapping):
            raise ResolutionError(f"{label}: package must be an object")
        _known_fields(value, _PACKAGE_FIELDS, label)
        for name, expected in value.items():
            _validate_predicate(name, expected, label)
    elif key in {"path", "destination"} and isinstance(value, Mapping):
        _known_fields(value, {"exact", "prefix", "subtree", "glob"}, label)
        if len(value) != 1:
            raise ResolutionError(f"{label}: path predicate needs exactly one matcher")
        for expected in value.values():
            if not isinstance(expected, str) or not expected:
                raise ResolutionError(f"{label}: path matcher must be a non-empty string")
            normalize_relative_path(expected, allow_glob=True)
    elif not (
        isinstance(value, str) and value
        or isinstance(value, (list, tuple)) and value
        and all(isinstance(item, str) and item for item in value)
    ):
        raise ResolutionError(f"{label}: {key} must be a string or non-empty string array")


def _relative_components(value: str) -> list[str]:
    if (
        not isinstance(value, str) or not value or "\x00" in value
        or value.replace("\\", "/").startswith("/")
        or PureWindowsPath(value).drive or ":" in value
    ):
        raise ResolutionError(f"routing path must be repository-relative: {value!r}")
    return value.replace("\\", "/").split("/")


@dataclass(frozen=True)
class RoutingRule:
    """A typed metadata record consumed only by the write-policy engine."""

    identity: str
    priority: int = 0
    when: Mapping[str, Any] = field(default_factory=frozen_map)
    file: str = "local-edits.json"
    section: str | None = None
    path_mode: str = "local"
    source_file: str | None = None

    def resolve_file(self, declaration_file: str | None = None) -> str:
        """Resolve local ``..`` segments without ever escaping the layer root."""
        if self.path_mode not in {"layer", "local"}:
            raise ResolutionError(f"invalid routing path_mode {self.path_mode!r}")
        parts: list[str] = []
        if self.path_mode == "local":
            base = self.source_file or declaration_file
            if not base:
                raise ResolutionError("local routing path requires declaration file")
            try:
                parts = metadata_local_root(base).split("/")
            except (MetadataError, UnsafePathError) as exc:
                raise ResolutionError(str(exc)) from exc
        for part in _relative_components(self.file):
            if part == "..":
                if not parts:
                    raise ResolutionError(f"routing path escapes layer: {self.file!r}")
                parts.pop()
            elif part not in {"", "."}:
                parts.append(part)
        return normalize_metadata_destination(normalize_relative_path("/".join(parts)))

    def matches(
        self, operation: Mapping[str, Any], *, context: Mapping[str, Any] | None = None
    ) -> bool:
        """Match persisted fields and separate, ephemeral write provenance."""
        context = context or {}
        package = dict(operation.get("package", {}))
        for key in _PACKAGE_FIELDS - {"origin"}:
            if key in operation:
                package[key] = operation[key]
        source = operation.get("source")
        if isinstance(source, Mapping) and "provider" in source:
            package.setdefault("provider", source["provider"])
        for key in _PACKAGE_FIELDS:
            if key in context:
                package[key] = context[key]
        package.update(context.get("package", {}))
        # Origin describes the write intent, not the artifact download source.
        package.setdefault("origin", context.get("origin", "user"))
        for key, expected in self.when.items():
            if key == "package":
                if not all(_matches_value(name, package.get(name), value)
                           for name, value in expected.items()):
                    return False
                continue
            if key == "operation_types":
                actual = operation.get("type")
            elif key.startswith("package."):
                actual = package.get(key.removeprefix("package."))
            elif key in {"origin", "provider", "tags", "categories", "activation"}:
                actual = package.get(key, operation.get("state") if key == "activation" else None)
            else:
                actual = operation.get(key)
            if not _matches_value(key, actual, expected):
                return False
        return True


def _matches_value(key: str, actual: Any, expected: Any) -> bool:
    if key in {"path", "destination"} and isinstance(expected, Mapping):
        if not isinstance(actual, str):
            return False
        matcher, value = next(iter(expected.items()))
        if matcher == "exact":
            return bool(actual == value)
        if matcher == "prefix":
            return actual.startswith(value)
        if matcher == "subtree":
            return actual == value.rstrip("/") or actual.startswith(value.rstrip("/") + "/")
        return matches(actual, value)
    wanted = expected if isinstance(expected, (list, tuple)) else (expected,)
    if key.rsplit(".", 1)[-1] in {"tags", "categories"}:
        return isinstance(actual, (list, tuple, set)) and set(wanted).issubset(actual)
    return actual in wanted


class RoutingEngine:
    """Select one highest-priority destination independently of input order."""

    def __init__(self, rules: Iterable[RoutingRule] = ()) -> None:
        self.rules = tuple(rules)

    def destination(
        self, operation: Mapping[str, Any], *, declaration_file: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[str, str | None] | None:
        applicable = [rule for rule in self.rules if rule.matches(operation, context=context)]
        if not applicable:
            return None
        highest = max(rule.priority for rule in applicable)
        winners = [rule for rule in applicable if rule.priority == highest]
        destinations = {(rule.resolve_file(declaration_file), rule.section) for rule in winners}
        if len(destinations) != 1:
            identities = ", ".join(sorted(rule.identity for rule in winners))
            raise ResolutionError(f"ambiguous routing rules for operation: {identities}")
        return next(iter(destinations))


def routing_rule_from_json(
    raw: Mapping[str, Any], *, source: str, index: int = 0
) -> RoutingRule:
    """Decode an already-parsed record; the policy core does not depend on JSON."""
    label = f"{source}: routing_rule[{index}]"
    _known_fields(raw, {"type", "id", "priority", "when", "write"}, label)
    if raw.get("type") != "routing_rule":
        raise ResolutionError(f"{label}: type must be routing_rule")
    identity = raw.get("id")
    if not isinstance(identity, str) or not identity:
        raise ResolutionError(f"{label}: id must be a non-empty string")
    priority = raw.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ResolutionError(f"{label}: priority must be an integer")
    when = raw.get("when", {})
    if not isinstance(when, Mapping):
        raise ResolutionError(f"{label}: when must be an object")
    _known_fields(when, _PREDICATES, label)
    for key, value in when.items():
        _validate_predicate(key, value, label)
    write = raw.get("write", {})
    if not isinstance(write, Mapping):
        raise ResolutionError(f"{label}: write must be an object")
    _known_fields(write, {"file", "path", "path_mode", "section"}, label)
    if "file" in write and "path" in write:
        raise ResolutionError(f"{label}: specify file or path, not both")
    file_spec = write.get("file", write.get("path", "local-edits.json"))
    if isinstance(file_spec, Mapping):
        _known_fields(file_spec, {"path", "path_mode"}, label)
        if "path_mode" in write:
            raise ResolutionError(f"{label}: path_mode belongs inside file when file is an object")
        path = file_spec.get("path")
        path_mode = file_spec.get("path_mode", "local")
    else:
        path, path_mode = file_spec, write.get("path_mode", "local")
    if not isinstance(path, str) or not path:
        raise ResolutionError(f"{label}: destination file must be a string")
    if not isinstance(path_mode, str) or path_mode not in {"layer", "local"}:
        raise ResolutionError(f"{label}: path_mode must be layer or local")
    section = write.get("section")
    if section is not None and (not isinstance(section, str) or not section):
        raise ResolutionError(f"{label}: section must be a non-empty string")
    rule = RoutingRule(identity, priority, frozen_map(when), path, section, path_mode, source)
    rule.resolve_file()
    return rule


def merge_routing_rules(*layers: Iterable[RoutingRule]) -> tuple[RoutingRule, ...]:
    """Child id replaces parent id; duplicate ids within one layer are invalid."""
    merged: dict[str, RoutingRule] = {}
    for rules in layers:
        own: set[str] = set()
        for rule in rules:
            if rule.identity in own:
                raise ResolutionError(f"duplicate routing rule id in layer: {rule.identity!r}")
            own.add(rule.identity)
            merged[rule.identity] = rule
    return tuple(sorted(merged.values(), key=lambda rule: (-rule.priority, rule.identity)))
