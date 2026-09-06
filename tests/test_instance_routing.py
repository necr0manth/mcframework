from pathlib import Path

from mcframework.local_state import LocalState


def test_instance_routing_rules_are_untracked_and_round_trip(tmp_path: Path):
    state = LocalState(tmp_path)
    rules = [{"id": "deps", "type": "routing_rule", "priority": 50}]
    state.write_routing_rules("demo", rules)

    assert state.routing_rules("demo") == tuple(rules)
    assert state.routing_rules_path("demo") == tmp_path / ".mcpack" / "routing" / "demo.json"

