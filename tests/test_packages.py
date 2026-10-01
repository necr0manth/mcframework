from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mcframework.errors import ResolutionError
from mcframework.loader import WorkspaceLoader
from mcframework.model import Activation
from mcframework.resolver import Resolver

HASH = hashlib.sha256(b"artifact").hexdigest()


def package(
    identity: str,
    *,
    logical_id: str | None = None,
    activation: str = "available",
    destination: str | None = None,
    automatic_requires: list[str] | None = None,
) -> dict:
    return {
        "type": "package",
        "id": identity,
        "logical_id": logical_id or identity,
        "activation": activation,
        "destination": destination or f"mods/{identity}.jar",
        "automatic_requires": automatic_requires or [],
        "source": {"provider": "local", "location": identity, "sha256": HASH},
    }


def resolve(tmp_path: Path, layer: str, *, automatic: bool = True):
    return Resolver(WorkspaceLoader(tmp_path), automatic_dependencies=automatic).resolve(layer)


def test_explicit_dependency_activates_transitively(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "pack",
        operations=[
            package("root", activation="enabled"),
            package("library"),
            {"type": "dependency", "source": "root", "target": "library"},
        ],
    )
    result = resolve(tmp_path, "pack")
    assert [item.identity for item in result.active_packages] == ["library", "root"]
    assert result.state.packages["library"].activation is Activation.TRANSITIVE


def test_later_disable_overrides_explicit_dependency_and_missing_dependency_fails(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "disabled",
        operations=[
            package("root", activation="enabled"),
            package("library", activation="disabled"),
            {"type": "dependency", "source": "root", "target": "library"},
        ],
    )
    result = resolve(tmp_path, "disabled")
    assert [item.identity for item in result.active_packages] == ["root"]
    layer_factory(
        "missing",
        operations=[
            package("root", activation="enabled"),
            {"type": "dependency", "source": "root", "target": "none"},
        ],
    )
    with pytest.raises(ResolutionError, match="missing"):
        resolve(tmp_path, "missing")


def test_enabled_group_activates_package_and_nested_group(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "pack",
        operations=[
            {"type": "group", "id": "all", "activation": "enabled"},
            {"type": "group", "id": "core"},
            package("library"),
            {"type": "dependency", "source": "all", "target": "core"},
            {"type": "dependency", "source": "core", "target": "library"},
            {"type": "section", "id": "main", "title": "Main", "members": ["library"], "order": 1},
        ],
    )
    result = resolve(tmp_path, "pack")
    assert [item.identity for item in result.active_packages] == ["library"]
    assert result.state.groups["core"].activation is Activation.TRANSITIVE
    assert result.state.sections["main"].members == ("library",)


def test_automatic_dependency_absent_is_ignored(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "pack", operations=[package("root", activation="enabled", automatic_requires=["absent"])]
    )
    assert [item.identity for item in resolve(tmp_path, "pack").active_packages] == ["root"]


def test_automatic_dependency_activates_highest_precedence(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[package("old", logical_id="lib")])
    layer_factory(
        "top",
        parents=["../base"],
        operations=[
            package("root", activation="enabled", automatic_requires=["lib"]),
            package("new", logical_id="lib"),
        ],
    )
    result = resolve(tmp_path, "top")
    assert [item.identity for item in result.active_packages] == ["new", "root"]


def test_automatic_dependency_can_be_disabled(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "pack",
        operations=[
            package("root", activation="enabled", automatic_requires=["lib"]),
            package("lib", logical_id="lib"),
        ],
    )
    assert [
        item.identity for item in resolve(tmp_path, "pack", automatic=False).active_packages
    ] == ["root"]


def test_automatic_dependency_ambiguity_fails_and_disabled_dependency_is_skipped(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "ambiguous",
        operations=[
            package("root", activation="enabled", automatic_requires=["lib"]),
            package("a", logical_id="lib"),
            package("b", logical_id="lib"),
        ],
    )
    with pytest.raises(ResolutionError, match="ambiguous"):
        resolve(tmp_path, "ambiguous")
    layer_factory(
        "disabled",
        operations=[
            package("root", activation="enabled", automatic_requires=["lib"]),
            package("a", logical_id="lib", activation="disabled"),
        ],
    )
    result = resolve(tmp_path, "disabled")
    assert [item.identity for item in result.active_packages] == ["root"]


def test_two_active_same_destination_fails_but_same_logical_id_does_not(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "bad",
        operations=[
            package("a", logical_id="same", activation="enabled", destination="mods/same.jar"),
            package("b", logical_id="same", activation="enabled", destination="mods/same.jar"),
        ],
    )
    with pytest.raises(ResolutionError, match="share destination"):
        resolve(tmp_path, "bad")
    layer_factory(
        "ok",
        operations=[
            package("a", logical_id="same", activation="enabled"),
            package("b", logical_id="same", activation="enabled"),
        ],
    )
    assert len(resolve(tmp_path, "ok").active_packages) == 2


def test_later_activation_overrides_declaration_state(layer_factory, tmp_path: Path) -> None:
    layer_factory("base", operations=[package("a", activation="enabled")])
    layer_factory(
        "top",
        parents=["../base"],
        operations=[{"type": "activation", "id": "a", "state": "disabled"}],
    )
    result = resolve(tmp_path, "top")
    assert result.state.packages["a"].activation is Activation.DISABLED
    assert result.active_packages == ()
