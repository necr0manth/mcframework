from pathlib import Path

from mcframework.local_state import LocalState


def test_instance_routing_rules_are_untracked_and_round_trip(tmp_path: Path):
    state = LocalState(tmp_path)
    rules = [{"id": "deps", "type": "routing_rule", "priority": 50}]
    state.write_routing_rules("demo", rules)

    assert state.routing_rules("demo") == tuple(rules)
    assert state.routing_rules_path("demo") == tmp_path / ".mcpack" / "routing" / "demo.json"
    assert '"operations"' in state.routing_rules_path("demo").read_text(encoding="utf-8")


def test_legacy_instance_routing_envelope_is_still_readable(tmp_path: Path):
    state = LocalState(tmp_path)
    path = state.routing_rules_path("demo")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema": 1, "routing_rules": [{"id": "deps", "priority": 50}]}',
        encoding="utf-8",
    )

    assert state.routing_rules("demo") == (
        {"type": "routing_rule", "id": "deps", "priority": 50},
    )
