import json

import pytest

from mcframework.errors import MetadataError, ResolutionError
from mcframework.local_state import LocalState
from mcframework.operations import (
    RoutingEngine,
    merge_routing_rules,
    operation_to_json,
    routing_rule_from_json,
)


def rule(identity="rule", *, source="mods/.pack/rules.json", **fields):
    return routing_rule_from_json(
        {"type": "routing_rule", "id": identity, **fields}, source=source
    )


def test_local_default_uses_actual_rule_file_not_writer_fallback():
    entry = rule(write={"file": "magic.json"})
    assert entry.path_mode == "local"
    assert RoutingEngine([entry]).destination(
        {"type": "package"}, declaration_file=".pack/local-edits.json"
    ) == ("mods/.pack/magic.json", None)


def test_deep_local_rule_uses_nearest_pack_and_destination_shorthand():
    entry = rule(source="mods/.pack/groups/optional/rules.json", write={"file": "magic"})
    assert entry.resolve_file() == "mods/.pack/magic.json"
    assert rule(
        write={"file": {"path": "mods/mods", "path_mode": "layer"}}
    ).resolve_file() == "mods/.pack/mods.json"


def test_local_parent_segments_stay_inside_layer():
    assert rule(write={"file": "../../resourcepacks/.pack/packs.json"}).resolve_file() == (
        "resourcepacks/.pack/packs.json"
    )
    with pytest.raises(ResolutionError, match="escapes layer"):
        rule(write={"file": "../../../outside.json"})


@pytest.mark.parametrize("path", ["C:/mods.json", "C:mods.json", "/mods.json", "\\\\host\\x.json"])
@pytest.mark.parametrize("mode", ["local", "layer"])
def test_os_absolute_paths_never_allowed(path, mode):
    with pytest.raises((MetadataError, ResolutionError)):
        rule(write={"file": {"path": path, "path_mode": mode}})


def test_explicit_layer_path_and_roundtrip():
    entry = rule(
        priority=5,
        when={"operation_types": ["package"], "package": {"categories": ["magic"]}},
        write={"file": {"path": "mods/.pack/magic.json", "path_mode": "layer"}, "section": "Magic"},
    )
    assert entry.resolve_file() == "mods/.pack/magic.json"
    restored = routing_rule_from_json(
        json.loads(json.dumps(operation_to_json(entry))), source=entry.source_file
    )
    assert restored == entry


def test_highest_priority_independent_of_iteration_order():
    low = rule("low", priority=-2, write={"file": "low.json"})
    high = rule("high", priority=20, write={"file": "high.json"})
    for entries in ([low, high], [high, low]):
        assert RoutingEngine(entries).destination({"type": "package"}) == (
            "mods/.pack/high.json", None
        )


def test_equal_priority_conflicting_destinations_fail_but_identical_coalesce():
    first = rule("a", write={"file": "a.json"})
    same = rule("b", write={"file": "a.json"})
    assert RoutingEngine([first, same]).destination({"type": "package"}) == (
        "mods/.pack/a.json", None
    )
    different = rule("c", write={"file": "a.json", "section": "Other"})
    with pytest.raises(ResolutionError, match="ambiguous.*a, c"):
        RoutingEngine([different, first]).destination({"type": "package"})


def test_duplicate_in_same_layer_fails_child_override_still_works():
    parent = rule(priority=9)
    child = rule(priority=2)
    assert merge_routing_rules([parent], [child]) == (child,)
    with pytest.raises(ResolutionError, match="duplicate"):
        merge_routing_rules([parent, child])


@pytest.mark.parametrize("fields", [
    {"typo": True},
    {"kind": "routing_rule"},
    {"priority": True},
    {"when": {"package.knd": ["mod"]}},
    {"when": {"package": {"knd": ["mod"]}}},
    {"when": {"package": {"kind": []}}},
    {"when": {"origin": 1}},
    {"when": {"path": {"subtree": "config", "glob": "**"}}},
    {"write": {"sectionn": "Mods"}},
    {"write": {"file": {"path": "a.json", "mode": "local"}}},
    {"write": {"file": {"path": "a.json"}, "path_mode": "local"}},
])
def test_invalid_rules_fail_loudly(fields):
    with pytest.raises(ResolutionError):
        rule(**fields)


def test_nested_package_predicates_match_real_serialized_operation_and_context():
    entry = rule(when={
        "operation_types": ["package"],
        "package": {"kind": ["mod"], "provider": ["modrinth"],
                    "origin": ["automatic_dependency"], "categories": ["library"]},
    })
    operation = {"type": "package", "kind": "mod", "source": {"provider": "modrinth"}}
    context = {"origin": "automatic_dependency", "package": {"categories": ["library", "utility"]}}
    assert entry.matches(operation, context=context)
    assert not entry.matches(operation)
    assert not entry.matches({**operation, "source": {"provider": "local"}}, context=context)


def test_dotted_predicates_and_tags_use_same_context_without_persisting_it():
    entry = rule(when={"package.kind": ["mod"], "tags": ["magic"], "origin": "user"})
    operation = {"type": "activation", "id": "magic", "state": "off"}
    assert entry.matches(operation, context={"package": {"kind": "mod", "tags": ["magic"]}})
    assert operation == {"type": "activation", "id": "magic", "state": "off"}
    assert not rule(when={"origin": "automatic_dependency"}).matches(
        {"type": "package", "source": {"origin": "automatic_dependency"}}
    )


def test_path_predicate_is_usable_for_non_package_operations():
    entry = rule(when={"operation_types": ["remove"], "path": {"subtree": "config/sub"}})
    assert entry.matches({"type": "remove", "path": "config/sub/settings.cfg"})
    assert not entry.matches({"type": "remove", "path": "config/submarine/settings.cfg"})


def test_local_state_validates_before_write_and_on_read(tmp_path):
    state = LocalState(tmp_path)
    valid = {"id": "mods", "when": {"package.kind": "mod"}, "write": {"file": "mods.json"}}
    state.write_routing_rules("dev", [valid])
    assert state.routing_rules("dev") == ({"type": "routing_rule", **valid},)
    before = state.routing_rules_path("dev").read_bytes()
    with pytest.raises(MetadataError, match="unsupported"):
        state.write_routing_rules("dev", [{**valid, "when": {"oops": "mod"}}])
    assert state.routing_rules_path("dev").read_bytes() == before
    state.routing_rules_path("dev").write_text(json.dumps({
        "schema": 1, "routing_rules": [{**valid, "when": {"oops": "mod"}}]
    }))
    with pytest.raises(MetadataError, match="unsupported"):
        state.routing_rules("dev")
