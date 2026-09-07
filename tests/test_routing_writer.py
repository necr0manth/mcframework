import json
from pathlib import Path

from mcframework.operations import RoutingEngine, RoutingRule
from mcframework.project import ProjectService


def test_routing_engine_matches_type_and_priority():
    low = RoutingRule("low", 1, {"type": "package"}, ".pack/low.json", path_mode="layer")
    high = RoutingRule("high", 5, {"type": "package"}, "mods/.pack/mods.json", path_mode="layer")
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
    layer = tmp_path / "layer"
    marker = layer / ".pack/layer.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"schema": 2, "id": "layer"}')
    path = layer / "mods/.pack/mods.json"
    service = ProjectService(tmp_path)
    plan = service.plan_metadata_writes(
        layer,
        [{"type": "package", "id": "sodium"}],
        destination="mods/.pack/mods.json",
        section="Mods",
    )
    assert not path.exists()
    plan.apply()
    document = json.loads(path.read_text())
    assert document["operations"][0] == {"type": "package", "id": "sodium"}
    assert document["operations"][1] == {
        "type": "section",
        "id": "Mods",
        "title": "Mods",
        "members": ["sodium"],
        "order": 0,
    }


def test_routed_section_updates_existing_members_without_duplicates(tmp_path: Path):
    layer = tmp_path / "layer"
    marker = layer / ".pack/layer.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"schema": 2, "id": "layer"}')
    path = layer / ".pack/mods.json"
    path.write_text(
        json.dumps(
            {
                "operations": [
                    {"type": "section", "id": "Mods", "title": "My Mods", "members": ["a"]}
                ]
            }
        )
    )
    service = ProjectService(tmp_path)
    plan = service.plan_metadata_writes(
        layer,
        [{"type": "package", "id": "a"}, {"type": "package", "id": "b"}],
        destination=".pack/mods.json",
        section="Mods",
    )
    plan.apply()
    section = next(
        item for item in json.loads(path.read_text())["operations"] if item["type"] == "section"
    )
    assert section["title"] == "My Mods"
    assert section["members"] == ["a", "b"]
