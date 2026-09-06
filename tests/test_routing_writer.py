import json

from mcframework.operations import RoutingEngine, RoutingRule


def test_routing_engine_matches_type_and_priority():
    low = RoutingRule("low", 1, {"type": "package"}, ".pack/low.json")
    high = RoutingRule("high", 5, {"type": "package"}, "mods/.pack/mods.json")
    assert RoutingEngine([low, high]).destination({"type": "package"}) == (
        "mods/.pack/mods.json",
        None,
    )


def test_routing_engine_supports_local_paths():
    rule = RoutingRule("r", 1, {"type": "ignore"}, "ignore.json", path_mode="local")
    assert RoutingEngine([rule]).destination(
        {"type": "ignore"}, declaration_file="mods/.pack/rules.json"
    ) == ("mods/.pack/ignore.json", None)
