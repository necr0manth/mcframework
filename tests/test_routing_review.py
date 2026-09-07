"""Independent cross-boundary regressions for routed metadata editing."""

import json
from pathlib import Path

import pytest

from mcframework.errors import MetadataError, UnsafePathError
from mcframework.loader import WorkspaceLoader
from mcframework.project import ProjectService
from mcframework.resolver import Resolver


def _rule(identity: str, path: str, when: dict) -> dict:
    return {
        "type": "routing_rule", "id": identity, "when": when,
        "write": {"file": {"path": path, "path_mode": "layer"}},
    }


def test_refresh_redecodes_unchanged_records_when_schema_changes(layer_factory, tmp_path: Path):
    layer = layer_factory("pack", operations=[{"type": "put", "path": "x", "content": "x"}])
    loader = WorkspaceLoader(tmp_path, refresh=True)
    assert Resolver(loader).resolve(layer).state.files == {"x": b"x"}
    marker = layer / ".pack/layer.json"
    raw = json.loads(marker.read_text())
    raw["schema"] = 2
    marker.write_text(json.dumps(raw))
    warm = Resolver(loader).resolve(layer)
    cold = Resolver(WorkspaceLoader(tmp_path)).resolve(layer)
    assert warm.state.files == cold.state.files


def test_explicit_destination_can_resolve_ambiguous_rules(layer_factory, tmp_path: Path):
    layer = layer_factory("pack", operations=[
        _rule("one", ".pack/a.json", {"operation_types": ["ignore"]}),
        _rule("two", ".pack/b.json", {"operation_types": ["ignore"]}),
    ])
    service = ProjectService(tmp_path)
    plan = service.plan_metadata_writes(
        layer, [{"type": "ignore", "glob": "logs/**"}],
        destination=".pack/manual.json",
    )
    assert [item.path.relative_to(layer).as_posix() for item in plan.writes] == [
        ".pack/manual.json"
    ]


def test_explicit_section_can_resolve_section_only_ambiguity(layer_factory, tmp_path: Path):
    first = _rule("one", ".pack/packages.json", {"operation_types": ["package"]})
    second = _rule("two", ".pack/packages.json", {"operation_types": ["package"]})
    first["write"]["section"] = "First"
    second["write"]["section"] = "Second"
    layer = layer_factory("pack", operations=[first, second])
    plan = ProjectService(tmp_path).plan_metadata_writes(layer, [{
        "type": "package", "id": "a", "destination": "mods/a.jar",
        "source": {"provider": "local", "location": "a.jar", "sha256": "0" * 64},
    }], section="Chosen")
    assert len(plan.writes) == 1
    document = json.loads(plan.writes[0].after)
    assert document["operations"][-1]["type"] == "section"
    assert document["operations"][-1]["id"] == "Chosen"


def test_disabled_activation_rule_matches_requested_not_previous_state(
    layer_factory, tmp_path: Path,
):
    layer_factory("base", operations=[{
        "type": "package", "id": "a", "destination": "mods/a.jar", "activation": "enabled",
        "source": {"provider": "local", "location": "a.jar", "sha256": "0" * 64},
    }])
    layer = layer_factory("pack", parents=["../base"], operations=[
        _rule("disabled", ".pack/custom-disabled.json", {
            "operation_types": ["activation"], "activation": ["disabled"],
        }),
    ])
    plan = ProjectService(tmp_path).plan_metadata_writes(
        layer, [{"type": "activation", "id": "a", "state": "disabled"}],
    )
    assert [item.path.relative_to(layer).as_posix() for item in plan.writes] == [
        ".pack/custom-disabled.json"
    ]


def test_multi_file_section_edit_rolls_back_all_shards_after_validation_failure(
    layer_factory, tmp_path: Path, monkeypatch,
):
    packages = [{
        "type": "package", "id": identity, "destination": f"mods/{identity}.jar",
        "activation": "available",
        "source": {"provider": "local", "location": f"{identity}.jar", "sha256": "0" * 64},
    } for identity in ("a", "b")]
    layer_factory("base", operations=[*packages, {
        "type": "section", "id": "Mods", "title": "Mods", "members": ["a", "b"],
    }])
    layer = layer_factory("pack", parents=["../base"], operations=[
        _rule("a", "mods/.pack/a.json", {"operation_types": ["activation"],
                                         "package": {"id": "a"}}),
        _rule("b", "resourcepacks/.pack/b.json", {"operation_types": ["activation"],
                                                  "package": {"id": "b"}}),
    ])
    marker = layer / ".pack/layer.json"
    document = json.loads(marker.read_text())
    marker.write_text(json.dumps({**document, "schema": 2}))
    service = ProjectService(tmp_path)
    service.link("pack", "dev", tmp_path.parent / (tmp_path.name + "-game"))
    original = service.instance_context
    calls = 0

    def fail_after_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert (layer / "mods/.pack/a.json").is_file()
            assert (layer / "resourcepacks/.pack/b.json").is_file()
            raise MetadataError("injected validation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "instance_context", fail_after_write)
    before = {path.relative_to(layer): path.read_bytes()
              for path in layer.rglob("*") if path.is_file()}
    with pytest.raises(MetadataError, match="injected validation"):
        service.set_section_activation("dev", "Mods", "disabled", "pack")
    assert before == {path.relative_to(layer): path.read_bytes()
                      for path in layer.rglob("*") if path.is_file()}
    assert not (layer / "mods").exists()
    assert not (layer / "resourcepacks").exists()


def test_plan_rechecks_layer_confinement_before_apply(layer_factory, tmp_path: Path):
    layer = layer_factory("pack", operations=[])
    marker = layer / ".pack/layer.json"
    document = json.loads(marker.read_text())
    marker.write_text(json.dumps({**document, "schema": 2}))
    plan = ProjectService(tmp_path).plan_metadata_writes(
        layer, [{"type": "ignore", "glob": "logs/**"}],
        destination="mods/.pack/ignore.json",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (layer / "mods").mkdir()
    (layer / "mods/.pack").symlink_to(outside, target_is_directory=True)
    with pytest.raises((MetadataError, UnsafePathError)):
        plan.apply()
    assert not (outside / "ignore.json").exists()
