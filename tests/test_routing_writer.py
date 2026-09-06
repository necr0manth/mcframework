import json
from pathlib import Path

from mcframework.operations import RoutingEngine, RoutingRule
from mcframework.project import ProjectService


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


def test_routed_section_is_materialized_as_normal_section_record(tmp_path: Path):
    path = tmp_path / "mods" / ".pack" / "mods.json"
    service = ProjectService.__new__(ProjectService)
    service._append_operations_to_path(
        path,
        [({"type": "package", "id": "sodium"}, "Mods")],
    )
    document = json.loads(path.read_text())
    assert document["operations"][0] == {"type": "package", "id": "sodium"}
    assert document["operations"][1] == {
        "type": "section",
        "id": "Mods",
        "title": "Mods",
        "members": ["sodium"],
    }


def test_routed_section_updates_existing_members_without_duplicates(tmp_path: Path):
    path = tmp_path / "mods.json"
    path.write_text(json.dumps({"operations": [
        {"type": "section", "id": "Mods", "title": "My Mods", "members": ["a"]}
    ]}))
    service = ProjectService.__new__(ProjectService)
    service._append_operations_to_path(
        path,
        [({"type": "package", "id": "a"}, "Mods"), ({"type": "package", "id": "b"}, "Mods")],
    )
    section = next(item for item in json.loads(path.read_text())["operations"] if item["type"] == "section")
    assert section["title"] == "My Mods"
    assert section["members"] == ["a", "b"]
