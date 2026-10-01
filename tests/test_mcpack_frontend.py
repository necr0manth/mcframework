from pathlib import Path

import pytest

from mcframework.artifacts import LocalArtifactProvider
from mcframework.errors import MetadataError
from mcframework.loader import WorkspaceLoader
from mcframework.mcpack import (
    McpackDocument,
    McpackParser,
    format_operation,
    format_section,
    pin_provider_selector,
)
from mcframework.operations import AddPruneSelector, PutFile
from mcframework.resolver import Resolver


def test_parse_literals_comments_provider_aliases_and_operation_mappings():
    text = """#magic(title="Magic", order=20, members=[other_mod])
other_mod = *modrinth("example-mod", version="v1") // retained comment
put("config/a.txt", "hello // there") // trailing
patch_properties("server.properties", set={"online-mode": True}, remove=["old"])
route("mods", when={"type": "package"}, file="mods/.pack/mods.mcpack", priority=50)
"""
    doc = McpackParser().parse(text, "ops.mcpack")
    assert doc.lines[0].record["id"] == "magic"
    assert doc.lines[0].record["order"] == 1
    assert doc.lines[0].record["members"] == ["other_mod"]
    package = doc.lines[1].record
    assert package["type"] == "package"
    assert package["id"] == "other_mod"
    assert package["activation"] == "available"
    assert package["version"] == "v1"
    assert "alias" not in package
    assert "logical_id" not in package
    assert "automatic_requires" not in package
    assert package["section"] == "magic"
    assert doc.lines[2].record["type"] == "put"
    assert doc.lines[2].record["content"] == "hello // there"
    assert doc.lines[3].record["set"] == {"online-mode": True}
    assert doc.lines[4].record["type"] == "routing_rule"


def test_section_header_accepts_positional_metadata():
    record = McpackParser().parse('#magic("Magic", 20, [other_mod])\n#second section\n')
    record = record.lines[0].record
    assert record["title"] == "Magic"
    assert record["order"] == 1
    assert record["members"] == ["other_mod"]
    assert McpackParser().parse("#create\n#other section\n").lines[1].record == {
        "type": "section",
        "id": "other section",
        "title": "other section",
        "members": [],
        "order": 2,
        "_line_id": McpackParser().parse("#create\n#other section\n").lines[1].line_id,
        "_line_number": 2,
        "_expression": "#other section",
    }


def test_parentheses_in_plain_section_titles_are_not_legacy_metadata():
    doc = McpackParser().parse("#Magic (client)\n#Magic(v2)\n")
    assert [line.record["id"] for line in doc.lines] == [
        "Magic (client)",
        "Magic(v2)",
    ]
    assert [line.record["title"] for line in doc.lines] == [
        "Magic (client)",
        "Magic(v2)",
    ]
    assert (
        format_section({"type": "section", "id": "Magic (client)", "title": "Magic (client)"})
        == "#Magic (client)"
    )


def test_package_references_map_to_activation_and_dependency_records():
    doc = McpackParser().parse(
        'owner = modrinth("owner")\nlibrary = *modrinth("library")\n'
        "dependency(owner, library)\nremove(library)\n"
    )
    assert doc.lines[2].record["source"] == "owner"
    assert doc.lines[2].record["target"] == "library"
    assert doc.lines[3].record == {
        "type": "activation",
        "id": "library",
        "state": "disabled",
        "_line_id": doc.lines[3].line_id,
        "_line_number": 4,
        "_expression": "remove(library)",
    }


@pytest.mark.parametrize(
    "expression",
    [
        'sodium = *modrinth("sodium", "")',
        'sodium = *modrinth("sodium", version="")',
    ],
)
def test_empty_provider_version_is_preserved_as_an_explicit_selector(expression):
    record = McpackParser().parse(expression).lines[0].record
    assert record["version"] == ""
    assert record["activation"] == "available"


def test_omitted_provider_version_remains_unselected():
    record = McpackParser().parse('sodium = *modrinth("sodium")').lines[0].record
    assert "version" not in record


def test_loader_package_source_index_is_physical_line_number(tmp_path: Path):
    layer = tmp_path / "client"
    metadata = layer / ".pack"
    metadata.mkdir(parents=True)
    (metadata / "layer.mcpack").write_text('layer("client")\n', encoding="utf-8")
    source = metadata / "packages.mcpack"
    source.write_text(
        '\nmod = direct_url("https://example.org/mod.jar", '
        '"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", '
        'destination="mods/mod.jar")\n',
        encoding="utf-8",
    )
    loaded = WorkspaceLoader(tmp_path).load(layer)
    declaration = loaded.producers[0].operations[0].package
    assert declaration.source_file == str(source)
    assert declaration.source_index == 2
    assert declaration.alias is None
    assert declaration.version is None


def test_edit_api_preserves_newlines_indentation_comments_and_checks_original():
    source = '  put("a.txt", "before")  // note\r\n\r\n#items\r\n'
    doc = McpackDocument.parse(source, "ops.mcpack")
    line = doc.lines[0]
    updated = doc.replace_expression(line.line_id, line.expression, 'put("a.txt", "after")')
    assert updated == b'  put("a.txt", "after")  // note\r\n\r\n#items\r\n'
    with pytest.raises(MetadataError, match="changed"):
        doc.replace_expression(line.line_id, "stale", "x()")


@pytest.mark.parametrize(
    ("expression", "selector", "expected"),
    [
        ('alias = *modrinth("slug")', "v1", 'alias = *modrinth("slug", "v1")'),
        ('modrinth("slug", version = "")', "v2", 'modrinth("slug", version = "v2")'),
        ("curseforge('slug', 123, kind='mod')", "456", "curseforge('slug', 456, kind='mod')"),
        ('curseforge(project="slug")', "exact.jar", 'curseforge(project="slug", file="exact.jar")'),
    ],
)
def test_pin_provider_selector_preserves_expression_style(expression, selector, expected):
    assert pin_provider_selector(expression, selector) == expected


def test_pin_provider_selector_with_document_keeps_comment_and_line_format():
    original = '  pack = *modrinth("slug")   // note\r\n'
    doc = McpackDocument.parse(original, "mods.mcpack")
    line = doc.lines[0]
    changed = pin_provider_selector(line.expression, "v1")
    assert doc.replace_expression(line.line_id, line.expression, changed) == (
        b'  pack = *modrinth("slug", "v1")   // note\r\n'
    )


@pytest.mark.parametrize(
    "record",
    [
        {
            "type": "package",
            "id": "mod",
            "activation": "enabled",
            "source": {"provider": "modrinth", "location": "slug", "sha256": ""},
            "version": "v1",
            "destination": "mods/mod.jar",
            "logical_id": "mod-id",
            "alias": "modid",
            "automatic_requires": ["lib"],
        },
        {
            "type": "package",
            "id": "curseforge:slug",
            "activation": "available",
            "source": {"provider": "curseforge", "location": "slug", "sha256": ""},
            "version": "1234",
        },
        {
            "type": "package",
            "id": "local:artifacts/a.jar",
            "activation": "enabled",
            "source": {"provider": "local", "location": "artifacts/a.jar", "sha256": ""},
        },
        {
            "type": "package",
            "id": "download",
            "activation": "enabled",
            "source": {
                "provider": "direct_url",
                "location": "https://example.org/a.jar",
                "sha256": "a" * 64,
            },
            "destination": "mods/a.jar",
        },
        {"type": "activation", "id": "old-package-id", "state": "disabled"},
        {"type": "dependency", "source": "mod", "target": "lib"},
        {"type": "group", "id": "performance", "name": "Performance", "activation": "enabled"},
        {
            "type": "routing_rule",
            "id": "mods",
            "when": {"type": "package"},
            "write": {
                "file": {"path": "mods/.pack/mods.mcpack", "path_mode": "layer"},
                "section": "Magic",
            },
            "priority": 50,
        },
        {"type": "put", "path": "a.bin", "content": b"\x00\x01"},
        {"type": "remove", "path": "config/old.txt"},
        {"type": "remove_paths", "glob": "server-only/**"},
        {
            "type": "patch_properties",
            "path": "server.properties",
            "set": {"online-mode": True},
            "remove": ["old"],
        },
        {"type": "prune", "glob": "mods/*.jar"},
        {"type": "ignore", "selector": {"kind": "subtree", "path": "cache"}},
        {"type": "layer", "id": "client", "minecraft": "26.3", "loader": "neoforge"},
        {"type": "parent", "path": "../base", "parameters": {"side": "client"}},
    ],
)
def test_format_operation_round_trips_normalized_operation_kind(record):
    expression = format_operation(record)
    parsed = McpackParser().parse(expression).lines[0].record
    assert parsed["type"] == record["type"]
    kind = record["type"]
    if kind == "package":
        assert parsed["id"] == record["id"]
        assert parsed["activation"] == record["activation"]
        assert parsed["source"]["provider"] == record["source"]["provider"]
        assert parsed["source"]["location"] == record["source"]["location"]
        assert parsed.get("version") == record.get("version")
        assert parsed.get("destination") == record.get("destination")
        assert parsed.get("logical_id") == record.get("logical_id")
        assert parsed.get("alias") == record.get("alias")
        assert parsed.get("automatic_requires") == record.get("automatic_requires")
        if record["source"]["provider"] == "direct_url":
            assert parsed["source"]["sha256"] == record["source"]["sha256"]
    elif kind == "activation":
        assert parsed["id"] == record["id"]
        assert parsed["state"] == record["state"]
    elif kind == "dependency":
        assert parsed["source"] == record["source"]
        assert parsed["target"] == record["target"]
    elif kind == "group":
        assert parsed["id"] == record["id"]
        assert parsed["name"] == record["name"]
        assert parsed["activation"] == record["activation"]
    elif kind == "routing_rule":
        assert parsed["id"] == record["id"]
        assert parsed["when"] == record["when"]
        assert parsed["write"] == record["write"]
        assert parsed["priority"] == record["priority"]
    elif kind == "put":
        import base64

        assert base64.b64decode(parsed["content_base64"]) == record["content"]
    elif kind in {"remove", "patch_properties"}:
        for key in ("path", "set", "remove"):
            if key in record:
                assert parsed[key] == record[key]
    elif kind in {"remove_paths", "prune"}:
        assert parsed["glob"] == record["glob"]
    elif kind == "ignore":
        assert parsed["selector"] == record["selector"]
    elif kind == "layer":
        for key in ("id", "minecraft", "loader"):
            assert parsed.get(key) == record.get(key)
    elif kind == "parent":
        assert parsed["path"] == record["path"]
        assert parsed["parameters"] == record["parameters"]


def test_format_section_emits_compact_header_and_infers_order_from_file():
    record = {
        "type": "section",
        "id": "magic",
        "title": "Magic",
        "order": 20,
        "members": ["mod", "old-package-id"],
    }
    header = format_section(record)
    assert header == "#magic"
    parsed = McpackParser().parse(f"#before\n{header}\n").lines[1].record
    assert parsed["id"] == "magic"
    assert parsed["title"] == "magic"
    assert parsed["order"] == 2
    assert parsed["members"] == []


def test_section_edit_replaces_only_header_expression_and_keeps_comment_and_neighbors():
    original = '#old(title="Legacy", order=50)  // keep\r\n#other section\r\n'
    document = McpackDocument.parse(original, "ops.mcpack")
    section = document.lines[0]
    assert section.record["order"] == 1
    replacement = format_section({"type": "section", "id": "new", "title": "Create"})
    changed = document.replace_section(section.line_id, section.expression, replacement)
    assert changed == b"#new  // keep\r\n#other section\r\n"


def test_format_section_keeps_identity_over_legacy_title_and_order():
    assert (
        format_section({"type": "section", "id": "create", "title": "Create", "order": 1})
        == "#create"
    )


def test_package_formatter_omits_defaults_but_keeps_selected_destination():
    record = {
        "type": "package",
        "id": "sodium",
        "activation": "enabled",
        "kind": "mod",
        "update_policy": "fixed",
        "automatic_requires": [],
        "source": {"provider": "modrinth", "location": "sodium", "sha256": ""},
    }
    assert format_operation(record) == 'sodium = modrinth("sodium")'

    record["destination"] = "mods/sodium-neoforge.jar"
    formatted = format_operation(record)
    assert formatted == ('sodium = modrinth("sodium", destination="mods/sodium-neoforge.jar")')
    assert "\n" not in formatted


def test_parser_rejects_expressions_duplicate_arguments_and_duplicate_sections():
    for text in ("put(path(), 'x')\n", "put('x', content='a', content='b')\n", "#same\n#same\n"):
        with pytest.raises(MetadataError):
            McpackParser().parse(text)


def test_loader_reads_mcpack_marker_and_operation_files(tmp_path: Path):
    layer = tmp_path / "client"
    metadata = layer / ".pack"
    metadata.mkdir(parents=True)
    (metadata / "layer.mcpack").write_text(
        'layer("client", minecraft="26.3", loader="neoforge")\nparent("../base")\n',
        encoding="utf-8",
    )
    (metadata / "ops.mcpack").write_text(
        'put("config/a.txt", "hello")\nprune("mods/*.jar")\n', encoding="utf-8"
    )
    loaded = WorkspaceLoader(tmp_path).load(layer)
    assert loaded.identity == "client"
    assert loaded.minecraft == "26.3"
    assert loaded.loader == "neoforge"
    assert loaded.parents[0].path == "../base"
    assert isinstance(loaded.producers[0].operations[0], PutFile)
    assert loaded.producers[0].operations[1] == AddPruneSelector("mods/*.jar")


def test_mcpack_same_layer_package_activation_and_later_disable_precedence(tmp_path: Path):
    parent = tmp_path / "base"
    parent_metadata = parent / ".pack"
    parent_metadata.mkdir(parents=True)
    (parent_metadata / "layer.mcpack").write_text('layer("base")\n', encoding="utf-8")
    (parent_metadata / "packages.mcpack").write_text(
        'library = *modrinth("library", destination="mods/library.jar")\n'
        "add(library)\n"
        '*modrinth("anonymous", destination="mods/anonymous.jar")\n'
        'add(modrinth("anonymous"))\n',
        encoding="utf-8",
    )

    child = tmp_path / "client"
    child_metadata = child / ".pack"
    child_metadata.mkdir(parents=True)
    (child_metadata / "layer.mcpack").write_text(
        'layer("client")\nparent("../base")\n', encoding="utf-8"
    )
    (child_metadata / "packages.mcpack").write_text(
        'disable(library)\ndisable(modrinth("anonymous"))\n', encoding="utf-8"
    )

    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("base")
    assert {package.identity for package in resolution.active_packages} == {
        "library",
        "modrinth:anonymous",
    }

    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("client")
    assert resolution.active_packages == ()
    assert {package.identity for package in resolution.state.packages.values()} == {
        "library",
        "modrinth:anonymous",
    }


def test_mcpack_local_package_source_is_relative_to_its_layer(tmp_path: Path):
    layer = tmp_path / "client"
    metadata = layer / ".pack"
    metadata.mkdir(parents=True)
    source = metadata / "source.jar"
    source.write_bytes(b"layer-local source")
    (metadata / "layer.mcpack").write_text('layer("client")\n', encoding="utf-8")
    (metadata / "packages.mcpack").write_text(
        'library = *local(".pack/source.jar", destination="mods/library.jar")\n',
        encoding="utf-8",
    )

    loaded = WorkspaceLoader(tmp_path).load(layer)
    package = loaded.producers[0].operations[0].package

    assert LocalArtifactProvider(tmp_path).fetch(package) == b"layer-local source"


def test_loader_retains_legacy_json_marker_support(tmp_path: Path):
    layer = tmp_path / "legacy"
    metadata = layer / ".pack"
    metadata.mkdir(parents=True)
    (metadata / "layer.json").write_text('{"schema":1,"id":"legacy"}', encoding="utf-8")
    loaded = WorkspaceLoader(tmp_path).load(layer)
    assert loaded.identity == "legacy"
