from __future__ import annotations

from pathlib import Path

import pytest

from mcframework.errors import ResolutionError
from mcframework.loader import WorkspaceLoader
from mcframework.model import Activation
from mcframework.resolver import Resolver


def package(identity: str, *, requires: tuple[str, ...] = (), **fields) -> dict:
    return {
        "type": "package",
        "id": identity,
        "destination": f"mods/{identity}.jar",
        "automatic_requires": list(requires),
        "source": {"provider": "local", "location": identity, "sha256": "0" * 64},
        **fields,
    }


def dependency(source: str, target: str) -> dict:
    return {"type": "dependency", "source": source, "target": target}


def resolve(tmp_path: Path, *, automatic: bool = True):
    return Resolver(WorkspaceLoader(tmp_path), automatic_dependencies=automatic).resolve("pack")


def test_automatic_target_explicit_dependency_is_activated(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "pack",
        operations=[
            package("a", activation="enabled", requires=("b",)),
            package("b"),
            package("c"),
            dependency("b", "c"),
        ],
    )
    result = resolve(tmp_path)
    assert [item.identity for item in result.active_packages] == ["a", "b", "c"]
    assert result.state.packages["a"].activation is Activation.ENABLED
    for identity in ("b", "c"):
        assert result.state.packages[identity].activation is Activation.TRANSITIVE
    assert [item.identity for item in resolve(tmp_path, automatic=False).active_packages] == ["a"]


@pytest.mark.parametrize("order", ["forward", "reverse", "interleaved"])
@pytest.mark.parametrize("automatic_first", [True, False])
def test_alternating_chain_reaches_fixed_point(
    layer_factory, tmp_path: Path, order: str, automatic_first: bool
) -> None:
    identities = ("z", "a", "y", "b", "x", "c", "w")
    operations = []
    for index, identity in enumerate(identities):
        target = identities[index + 1] if index + 1 < len(identities) else None
        automatic = (index % 2 == 0) == automatic_first
        operations.append(
            package(
                identity,
                activation="enabled" if index == 0 else "available",
                requires=(target,) if target and automatic else (),
            )
        )
        if target and not automatic:
            operations.append(dependency(identity, target))
    if order == "reverse":
        operations.reverse()
    elif order == "interleaved":
        operations = operations[::2] + operations[1::2]
    layer_factory("pack", operations=operations)
    result = resolve(tmp_path)
    assert [item.identity for item in result.active_packages] == sorted(identities)
    assert result.state.packages["z"].activation is Activation.ENABLED
    assert all(
        result.state.packages[identity].activation is Activation.TRANSITIVE
        for identity in identities[1:]
    )


@pytest.mark.parametrize("disabled_kind", ["package", "group", "automatic"])
def test_disabled_target_after_automatic_activation(
    layer_factory, tmp_path: Path, disabled_kind: str
) -> None:
    operations = [package("a", activation="enabled", requires=("b",)), package("b")]
    if disabled_kind == "automatic":
        operations += [package("c", requires=("d",)), dependency("b", "c")]
        operations.append(package("d", activation="disabled"))
    else:
        target = (
            package("c", activation="disabled")
            if disabled_kind == "package"
            else {"type": "group", "id": "c", "activation": "disabled"}
        )
        operations += [target, dependency("b", "c")]
    layer_factory("pack", operations=operations)
    result = resolve(tmp_path)
    assert {item.identity for item in result.active_packages} == (
        {"a", "b", "c"} if disabled_kind == "automatic" else {"a", "b"}
    )
    if disabled_kind == "package":
        assert result.state.packages["c"].activation is Activation.DISABLED
    elif disabled_kind == "group":
        assert result.state.groups["c"].activation is Activation.DISABLED
    else:
        assert result.state.packages["d"].activation is Activation.DISABLED


def test_mixed_cycle_through_group_activates_outgoing_dependencies(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "pack",
        operations=[
            package("a", activation="enabled", requires=("b",)),
            package("b"),
            {"type": "group", "id": "g"},
            dependency("b", "g"),
            dependency("g", "a"),
            dependency("g", "c"),
            package("c", requires=("c", "b", "d")),
            package("d"),
            package("unused", requires=("unused",)),
        ],
    )
    result = resolve(tmp_path)
    assert [item.identity for item in result.active_packages] == ["a", "b", "c", "d"]
    assert result.state.groups["g"].activation is Activation.TRANSITIVE
    assert result.state.packages["a"].activation is Activation.ENABLED
    assert result.state.packages["unused"].activation is Activation.AVAILABLE


@pytest.mark.parametrize("candidate_state", ["available", "disabled", "ambiguous"])
def test_mixed_chain_preserves_automatic_candidate_selection(
    layer_factory, tmp_path: Path, candidate_state: str
) -> None:
    layer_factory("base", operations=[package("old", logical_id="lib")])
    operations = [
        package("a", activation="enabled", requires=("b",)),
        package("b"),
        dependency("b", "c"),
        package("c", requires=("lib", "absent")),
        package(
            "new",
            logical_id="lib",
            activation="disabled" if candidate_state == "disabled" else "available",
        ),
        dependency("new", "tail"),
        package("tail"),
    ]
    if candidate_state == "ambiguous":
        operations.append(package("other", logical_id="lib"))
    layer_factory("pack", parents=["../base"], operations=operations)
    if candidate_state == "ambiguous":
        with pytest.raises(
            ResolutionError, match="automatic dependency 'lib' is ambiguous: new, other"
        ):
            resolve(tmp_path)
    else:
        result = resolve(tmp_path)
        expected = (
            ["a", "b", "c", "old"]
            if candidate_state == "disabled"
            else ["a", "b", "c", "new", "tail"]
        )
        assert [item.identity for item in result.active_packages] == expected
