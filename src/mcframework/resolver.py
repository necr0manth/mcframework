"""Ordered recursive layer and package dependency resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .errors import ResolutionError
from .loader import WorkspaceLoader, validate_parameter_value
from .model import (
    Activation,
    LayerApplicationTrace,
    LayerDefinition,
    PackageDeclaration,
    PackState,
    ProducedOperationTrace,
    Resolution,
    frozen_map,
)
from .operations import Operation, PutFile, apply_local_operations


class Resolver:
    def __init__(self, loader: WorkspaceLoader, *, automatic_dependencies: bool = True):
        self.loader = loader
        self.automatic_dependencies = automatic_dependencies
        self._precedence = 0

    def resolve(
        self, layer: str | Path | LayerDefinition, parameters: Mapping[str, Any] | None = None
    ) -> Resolution:
        bound = dict(parameters or {})
        validate_parameter_value(bound)
        definition = layer if isinstance(layer, LayerDefinition) else self.loader.load(layer)
        self._precedence = 0
        state, trace = self._apply(definition, bound, PackState(), ())
        state, active = resolve_dependencies(state, self.automatic_dependencies)
        return Resolution(state, active, tuple(trace))

    def _apply(
        self,
        layer: LayerDefinition,
        parameters: Mapping[str, Any],
        input_state: PackState,
        stack: tuple[str, ...],
    ) -> tuple[PackState, list[LayerApplicationTrace]]:
        canonical = str(Path(layer.root).resolve())
        if canonical in stack:
            cycle = " -> ".join((*stack[stack.index(canonical) :], canonical))
            raise ResolutionError(f"layer parent cycle: {cycle}")
        state = input_state
        trace: list[LayerApplicationTrace] = []
        for reference in layer.parents:
            child_parameters = dict(parameters)
            child_parameters.update(reference.parameters)
            parent = self.loader.resolve_parent(layer, reference)
            state, parent_trace = self._apply(parent, child_parameters, state, (*stack, canonical))
            trace.extend(parent_trace)

        self._precedence += 1
        precedence = self._precedence
        produced: list[ProducedOperationTrace] = []
        operations: list[Operation] = []
        for producer in layer.producers:
            concrete = producer.produce(parameters)
            producer.validate(concrete)
            operations.extend(concrete)
            produced.append(ProducedOperationTrace(producer.name, producer.source, concrete))
        payload_operations = tuple(
            PutFile(path, content) for path, content in layer.payload.items()
        )
        if payload_operations:
            operations.extend(payload_operations)
            produced.append(ProducedOperationTrace("payload", "<payload>", payload_operations))
        state = apply_local_operations(state, tuple(operations), precedence)
        trace.append(
            LayerApplicationTrace(
                layer.identity,
                frozen_map(redact(parameters)),
                tuple(produced),
                str(Path(layer.root).resolve()),
            )
        )
        return state, trace


def resolve_dependencies(
    state: PackState, automatic_dependencies: bool
) -> tuple[PackState, tuple[PackageDeclaration, ...]]:
    packages = dict(state.packages)
    groups = dict(state.groups)
    known_nodes = set(packages) | set(groups)
    for dependency in state.dependencies:
        if dependency.source not in known_nodes:
            raise ResolutionError(f"dependency source is missing: {dependency.source!r}")
        if dependency.target not in known_nodes:
            raise ResolutionError(f"explicit dependency target is missing: {dependency.target!r}")

    active_nodes = {
        identity
        for identity, package in packages.items()
        if package.activation is Activation.ENABLED
    }
    active_nodes.update(
        identity for identity, group in groups.items() if group.activation is Activation.ENABLED
    )
    queue = list(sorted(active_nodes))
    while queue:
        source = queue.pop(0)
        for dependency in state.dependencies:
            if dependency.source != source:
                continue
            target = dependency.target
            activation = (
                packages[target].activation if target in packages else groups[target].activation
            )
            if activation is Activation.DISABLED:
                raise ResolutionError(f"explicit dependency target is disabled: {target!r}")
            if target not in active_nodes:
                active_nodes.add(target)
                queue.append(target)
                if target in packages:
                    packages[target] = replace(packages[target], activation=Activation.TRANSITIVE)
                else:
                    groups[target] = replace(groups[target], activation=Activation.TRANSITIVE)

    if automatic_dependencies:
        auto_queue = [identity for identity in sorted(active_nodes) if identity in packages]
        visited: set[str] = set()
        while auto_queue:
            source_id = auto_queue.pop(0)
            if source_id in visited:
                continue
            visited.add(source_id)
            for logical_id in packages[source_id].automatic_requires:
                candidates = [
                    package
                    for package in packages.values()
                    if package.logical_identity == logical_id
                ]
                if not candidates:
                    continue
                usable = [item for item in candidates if item.activation is not Activation.DISABLED]
                if not usable:
                    raise ResolutionError(
                        f"automatic dependency {logical_id!r} has only disabled candidates"
                    )
                best_precedence = max(item.layer_precedence for item in usable)
                best = [item for item in usable if item.layer_precedence == best_precedence]
                if len(best) != 1:
                    names = ", ".join(sorted(item.identity for item in best))
                    raise ResolutionError(
                        f"automatic dependency {logical_id!r} is ambiguous: {names}"
                    )
                selected = best[0]
                if selected.identity not in active_nodes:
                    active_nodes.add(selected.identity)
                    packages[selected.identity] = replace(
                        selected, activation=Activation.TRANSITIVE
                    )
                    auto_queue.append(selected.identity)

    active = tuple(packages[item] for item in sorted(active_nodes) if item in packages)
    destinations: dict[str, str] = {}
    for package in active:
        previous = destinations.get(package.destination)
        if previous is not None and previous != package.identity:
            raise ResolutionError(
                f"active packages {previous!r} and {package.identity!r} share destination "
                f"{package.destination!r}"
            )
        destinations[package.destination] = package.identity
    return replace(state, packages=frozen_map(packages), groups=frozen_map(groups)), active


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(normalize_secret_key(key) in {"secret", "secretref"} for key in value):
            return "<redacted>"
        return {
            key: "<redacted>" if is_secret_key(key) else redact(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def normalize_secret_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def is_secret_key(value: str) -> bool:
    normalized = normalize_secret_key(value)
    return any(
        marker in normalized
        for marker in (
            "secret",
            "password",
            "passwd",
            "token",
            "apikey",
            "privatekey",
            "credential",
        )
    )
