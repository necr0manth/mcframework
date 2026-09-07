import pytest

from mcframework.errors import ResolutionError
from mcframework.operations import merge_routing_rules, routing_rule_from_json


def test_layer_and_local_destinations_are_repository_relative():
    local = routing_rule_from_json(
        {
            "type": "routing_rule",
            "id": "x",
            "write": {"file": {"path": "out.json", "path_mode": "local"}},
        },
        source="mods/.pack/rules.json",
    )
    assert local.resolve_file() == "mods/.pack/out.json"


def test_local_path_requires_source_file():
    from mcframework.operations import RoutingRule

    with pytest.raises(ResolutionError):
        RoutingRule("x", file="out.json", path_mode="local").resolve_file(None)


def test_child_rule_replaces_parent_and_priority_orders():
    parent = routing_rule_from_json(
        {"type": "routing_rule", "id": "a", "priority": 1}, source="p.json"
    )
    child = routing_rule_from_json(
        {"type": "routing_rule", "id": "a", "priority": 5}, source="c.json"
    )
    other = routing_rule_from_json(
        {"type": "routing_rule", "id": "b", "priority": 3}, source="c.json"
    )
    merged = merge_routing_rules((parent,), (child, other))
    assert [r.identity for r in merged] == ["a", "b"]
    assert merged[0].priority == 5
