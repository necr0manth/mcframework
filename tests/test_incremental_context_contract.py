from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mcframework.errors import FrameworkError
from mcframework.model import Activation
from mcframework.project import ProjectService


def package(
    project: Path,
    identity: str,
    *,
    activation: str = "available",
    requires: tuple[str, ...] = (),
    destination: str | None = None,
) -> dict[str, Any]:
    source = project / f"{identity}.jar"
    content = f"verified artifact {identity}".encode()
    source.write_bytes(content)
    return {
        "type": "package",
        "id": identity,
        "activation": activation,
        "automatic_requires": list(requires),
        "destination": destination or f"mods/{identity}.jar",
        "source": {
            "provider": "local",
            "location": str(source),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }


def linked_service(project: Path) -> ProjectService:
    service = ProjectService(project)
    service.link("client", "dev", project.parent / f"{project.name}-game")
    return service


def assert_cold_equivalent(service: ProjectService, project: Path) -> None:
    warm = service.instance_context("dev")
    cold_service = ProjectService(project)
    cold = cold_service.instance_context("dev")
    assert warm == cold
    assert service.bootstrap("dev", include_delta=False) == cold_service.bootstrap(
        "dev", include_delta=False
    )


def test_warm_mutations_preserve_shared_mixed_dependency_closure(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "base",
        operations=[
            package(tmp_path, "first", activation="enabled", requires=("shared",)),
            package(tmp_path, "second", activation="enabled", requires=("shared",)),
            package(tmp_path, "shared"),
            package(tmp_path, "tail"),
            {"type": "dependency", "source": "shared", "target": "tail"},
        ],
        payload={"config/settings.txt": "unchanged"},
    )
    layer_factory("client", parents=["../base"], operations=[])
    service = linked_service(tmp_path)
    initial = service.instance_context("dev")
    assert set(initial.files) == {
        "config/settings.txt",
        "mods/first.jar",
        "mods/second.jar",
        "mods/shared.jar",
        "mods/tail.jar",
    }

    service.set_activation("dev", "first", "available", "client")
    assert_cold_equivalent(service, tmp_path)
    assert (
        service.instance_context("dev").resolution.state.packages["shared"].activation
        is Activation.TRANSITIVE
    )

    service.set_activation("dev", "second", "available", "client")
    assert_cold_equivalent(service, tmp_path)
    assert set(service.instance_context("dev").files) == {"config/settings.txt"}

    service.set_activation("dev", "first", "enabled", "client")
    assert_cold_equivalent(service, tmp_path)
    assert set(service.instance_context("dev").files) == {
        "config/settings.txt",
        "mods/first.jar",
        "mods/shared.jar",
        "mods/tail.jar",
    }
    service.add_ignore("dev", "config/settings.txt", "exact", "client")
    assert_cold_equivalent(service, tmp_path)
    identity = next(
        rule["identity"] for rule in service.ignore_rules("dev", "client") if rule["own"]
    )
    service.remove_ignore("dev", identity, "client")
    assert_cold_equivalent(service, tmp_path)
    assert len(initial.files) == 5


def test_returned_context_mutable_values_do_not_poison_subsequent_contexts(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[], payload={"config/settings.txt": "original"})
    service = linked_service(tmp_path)
    parameters = {"mode": "normal", "nested": {"values": ["keep"]}}
    service.state.write_parameters("dev", parameters)
    first = service.instance_context("dev")
    second = service.instance_context("dev")
    first.files["config/settings.txt"] = b"changed by caller"
    first.files["injected.txt"] = b"not a source file"
    first.parameters["mode"] = "poisoned"
    first.parameters["nested"]["values"].append("injected")
    first.resolution.trace[0].parameters["nested"]["values"].append("trace injection")

    assert second.files == {"config/settings.txt": b"original"}
    assert second.parameters == parameters
    assert second.resolution.trace[0].parameters == parameters
    assert_cold_equivalent(service, tmp_path)
    logical_only = service.instance_context("dev", prepare=False)
    assert logical_only.files == {}
    assert service.instance_context("dev").files == {"config/settings.txt": b"original"}


def test_reused_artifact_declaration_tracks_effective_activation(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory(
        "base",
        operations=[
            package(tmp_path, "parent", activation="enabled", requires=("library",)),
            package(tmp_path, "library"),
        ],
    )
    layer_factory("client", parents=["../base"], operations=[])
    service = linked_service(tmp_path)
    initial = service.instance_context("dev")
    original = next(
        item for item in initial.resolution.artifacts if item.declaration.identity == "library"
    )
    assert original.declaration.activation is Activation.TRANSITIVE

    for requested, expected in (
        ("enabled", Activation.ENABLED),
        ("available", Activation.TRANSITIVE),
    ):
        service.set_activation("dev", "library", requested, "client")
        current = service.instance_context("dev")
        artifact = next(
            item for item in current.resolution.artifacts if item.declaration.identity == "library"
        )
        assert artifact.declaration.activation is expected
        assert artifact.declaration == current.resolution.state.packages["library"]
        assert artifact.cache_path == original.cache_path
        assert current.files == initial.files
        assert_cold_equivalent(service, tmp_path)
    assert original.declaration.activation is Activation.TRANSITIVE


@pytest.mark.parametrize("collision", ["package", "payload"])
def test_failed_activation_rolls_back_and_does_not_poison_next_context(
    layer_factory, tmp_path: Path, collision: str
) -> None:
    operations = [package(tmp_path, "candidate", destination="mods/shared.jar")]
    if collision == "package":
        operations.append(
            package(tmp_path, "current", activation="enabled", destination="mods/shared.jar")
        )
    layer_factory(
        "base",
        operations=operations,
        payload={"mods/shared.jar": "payload owner"} if collision == "payload" else {},
    )
    layer_factory("client", parents=["../base"], operations=[])
    service = linked_service(tmp_path)
    previous = service.instance_context("dev")
    declaration = tmp_path / "base/.pack/operations.json"
    before = declaration.read_bytes()
    with pytest.raises(FrameworkError, match="conflict|destination"):
        service.set_activation("dev", "candidate", "enabled", "client")
    assert declaration.read_bytes() == before
    assert not (tmp_path / "client/.pack/local-edits.json").exists()
    assert service.instance_context("dev") == previous
    assert_cold_equivalent(service, tmp_path)

    service.add_ignore("dev", "mods/shared.jar", "exact", "client")
    assert_cold_equivalent(service, tmp_path)
    if collision == "package":
        service.set_activation("dev", "current", "available", "client")
        service.set_activation("dev", "candidate", "enabled", "client")
        assert_cold_equivalent(service, tmp_path)
        assert (
            service.instance_context("dev").files["mods/shared.jar"]
            == b"verified artifact candidate"
        )


def test_link_change_during_read_cannot_make_old_context_permanently_fresh(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[], payload={"config.txt": "old"})
    layer_factory("other", operations=[], payload={"config.txt": "new"})
    service = linked_service(tmp_path)
    assert service.instance_context("dev").files["config.txt"] == b"old"
    original = service.state.links
    changed = False

    def links():
        nonlocal changed
        records = original()
        if not changed:
            changed = True
            service.state.write_links(
                {"dev": replace(records["dev"], branch="other", effective_layer="other")}
            )
        return records

    monkeypatch.setattr(service.state, "links", links)
    service.instance_context("dev")
    assert service.instance_context("dev").files["config.txt"] == b"new"
    assert_cold_equivalent(service, tmp_path)


def test_payload_added_during_enumeration_remains_watched_for_later_edits(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer = layer_factory("client", operations=[], payload={"trigger.txt": "old"})
    service = linked_service(tmp_path)
    service.instance_context("dev")
    (layer / "trigger.txt").write_text("changed")
    added = layer / "new.txt"
    original = os.scandir
    inserted = False

    @contextmanager
    def scandir(path):
        nonlocal inserted
        with original(path) as entries:
            yield entries
        if Path(path) == layer and not inserted:
            inserted = True
            added.write_text("first")

    monkeypatch.setattr(os, "scandir", scandir)
    service.instance_context("dev")
    assert inserted
    added.write_text("second")
    assert service.instance_context("dev").files["new.txt"] == b"second"
    assert_cold_equivalent(service, tmp_path)
