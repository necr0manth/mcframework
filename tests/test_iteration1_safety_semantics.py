from __future__ import annotations

import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.local_state import InstanceRecord
from mcframework.package_inspect import (
    JarMetadata,
    ModrinthMatcher,
    ProviderMatch,
    confirmed_provider_match,
    file_hashes,
    inspect_jar,
)
from mcframework.project import ProjectService


def outside(project: Path, suffix: str = "game") -> Path:
    return project.parent / f"{project.name}-{suffix}"


def jar(entries: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class NoMatch:
    def match(self, content: bytes) -> ProviderMatch | None:
        del content
        return None


class ExactMatch:
    def __init__(self) -> None:
        self.calls = 0

    def match(self, content: bytes) -> ProviderMatch | None:
        self.calls += 1
        return ProviderMatch(
            "modrinth",
            "project-id",
            "version-id",
            "2.0.0",
            "https://cdn.example.invalid/mod.jar",
            file_hashes(content),
        )


class FakeResponse:
    def __init__(self, value: object):
        self.content = json.dumps(value).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.content


def test_instance_registration_rejects_project_intersections_and_delete_is_defensive(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    service = ProjectService(tmp_path)
    with pytest.raises(MetadataError, match="intersects protected"):
        service.create_instance("inside", tmp_path / "game")
    with pytest.raises(MetadataError, match="intersects protected"):
        service.create_instance("ancestor", tmp_path.parent)
    with pytest.raises(MetadataError, match="intersects protected"):
        service.link("client", "linked", tmp_path / ".mcpack/game")

    overlay = service.state.feature_root("danger")
    overlay.mkdir(parents=True)
    sentinel = overlay / "sentinel.txt"
    sentinel.write_text("must survive")
    service.state.write_instances({"danger": InstanceRecord("danger", overlay)})
    with pytest.raises(MetadataError, match="refusing to remove local layer"):
        service.delete_instance("danger")
    assert sentinel.read_text() == "must survive"


def test_target_layer_is_exact_current_composition_whitelist(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "activation": "available",
                "destination": "mods/mod.jar",
                "source": {"provider": "local", "location": "unused", "sha256": "0" * 64},
            }
        ],
    )
    layer_factory("unrelated", operations=[])
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", outside(tmp_path))

    with pytest.raises(MetadataError, match="not writable"):
        service.set_activation("dev", "mod", "disabled", "unrelated")
    service.set_activation("dev", "mod", "disabled", "client")
    assert service.instance_context("dev", prepare=False).resolution.state.packages[
        "mod"
    ].activation.value == "disabled"


def test_group_section_handlers_are_typed_atomic_and_hierarchical(
    layer_factory, tmp_path: Path
) -> None:
    artifacts: dict[str, str] = {}
    operations: list[dict[str, object]] = []
    for package_id in ("a", "b"):
        content = f"jar-{package_id}".encode()
        artifact = tmp_path / f"{package_id}.jar"
        artifact.write_bytes(content)
        artifacts[package_id] = hashlib.sha256(content).hexdigest()
        operations.append(
            {
                "type": "package",
                "id": package_id,
                "activation": "available",
                "destination": f"mods/{package_id}.jar",
                "source": {
                    "provider": "local",
                    "location": artifact.name,
                    "sha256": artifacts[package_id],
                },
            }
        )
    operations.extend(
        [
            {"type": "group", "id": "core", "activation": "available"},
            {"type": "dependency", "source": "core", "target": "a"},
            {"type": "section", "id": "mods", "title": "Mods", "members": ["a", "b"]},
        ]
    )
    layer_factory("client", operations=operations)
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", outside(tmp_path))

    service.set_group_activation("dev", "core", "enabled")
    assert (
        service.instance_context("dev").resolution.state.groups["core"].activation.value
        == "enabled"
    )
    service.set_section_activation("dev", "mods", "enabled")
    context = service.instance_context("dev")
    assert {item.identity for item in context.resolution.active_packages} == {"a", "b"}
    local_edits = json.loads(
        (tmp_path / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"]
    assert [item["id"] for item in local_edits if item["type"] == "activation"] == ["a", "b"]
    bootstrap = service.bootstrap("dev")
    metadata_file = next(
        item for item in bootstrap["metadata_files"] if item["path"] == ".pack/operations.json"
    )
    assert "mods" in metadata_file["sections"]
    assert metadata_file["declarations"]


@pytest.mark.parametrize(
    ("entry", "content", "expected"),
    [
        (
            "fabric.mod.json",
            '{"id":"fabric-id","version":"1.0","name":"Fabric","depends":{"minecraft":"*"}}',
            JarMetadata("fabric", ("fabric-id",), "Fabric", "1.0", ("minecraft",)),
        ),
        (
            "quilt.mod.json",
            '{"quilt_loader":{"id":"quilt-id","version":"2.0","depends":["minecraft"]},'
            '"metadata":{"name":"Quilt"}}',
            JarMetadata("quilt", ("quilt-id",), "Quilt", "2.0", ("minecraft",)),
        ),
        (
            "META-INF/mods.toml",
            '[[mods]]\nmodId="forge_id"\nversion="3.0"\ndisplayName="Forge"\n',
            JarMetadata("forge", ("forge_id",), "Forge", "3.0", ()),
        ),
        (
            "META-INF/neoforge.mods.toml",
            '[[mods]]\nmodId="neo_id"\nversion="4.0"\ndisplayName="Neo"\n',
            JarMetadata("neoforge", ("neo_id",), "Neo", "4.0", ()),
        ),
    ],
)
def test_common_jar_metadata_parsers(
    tmp_path: Path, entry: str, content: str, expected: JarMetadata
) -> None:
    path = tmp_path / "mod.jar"
    path.write_bytes(jar({entry: content}))
    assert inspect_jar(path) == expected


def test_modrinth_adapter_accepts_only_exact_returned_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"jar bytes"
    hashes = file_hashes(content)
    response = {
        "id": "version-id",
        "project_id": "project-id",
        "version_number": "1.2.3",
        "files": [
            {
                "primary": True,
                "url": "https://cdn.example.invalid/mod.jar",
                "hashes": {"sha512": hashes["sha512"]},
            }
        ],
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: FakeResponse(response))
    match = ModrinthMatcher().match(content)
    assert match is not None
    assert match.project_id == "project-id"
    response["files"][0]["hashes"]["sha512"] = "0" * 128
    assert ModrinthMatcher().match(content) is None


def test_fuzzy_provider_match_requires_explicit_matching_hash() -> None:
    content = b"jar bytes"
    hashes = file_hashes(content)
    candidate = {
        "provider": "modrinth",
        "project_id": "project-id",
        "version_id": "version-id",
        "confidence": "fuzzy",
        "download_url": "https://cdn.example.invalid/mod.jar",
        "hashes": hashes,
    }
    match = confirmed_provider_match(candidate, content)
    assert match is not None and match.confidence == "fuzzy"
    candidate["hashes"] = {"sha512": "0" * 128}
    with pytest.raises(MetadataError, match="do not match"):
        confirmed_provider_match(candidate, content)


def test_semantic_package_delta_rename_and_provider_candidates(
    layer_factory, tmp_path: Path
) -> None:
    desired_jar = jar({"fabric.mod.json": '{"id":"mod","version":"1.0"}'})
    actual_jar = jar({"fabric.mod.json": '{"id":"mod","version":"2.0"}'})
    (tmp_path / "mod.jar").write_bytes(desired_jar)
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "version": "1.0",
                "activation": "enabled",
                "destination": "mods/mod.jar",
                "source": {
                    "provider": "local",
                    "location": "mod.jar",
                    "sha256": hashlib.sha256(desired_jar).hexdigest(),
                },
            }
        ],
        payload={"old-name.txt": "same", "missing.txt": "wanted"},
    )
    game = outside(tmp_path)
    (game / "mods").mkdir(parents=True)
    (game / "mods/mod.jar").write_bytes(actual_jar)
    (game / "new-name.txt").write_text("same")
    extra = jar({"fabric.mod.json": '{"id":"extra","version":"1"}'})
    (game / "mods/extra.jar").write_bytes(extra)
    matcher = ExactMatch()
    service = ProjectService(tmp_path, provider_matcher=matcher)
    service.link("client", "dev", game)

    delta = service.instance_diff(service.instance_context("dev"))
    by_status = {item["status"]: item for item in delta["items"]}
    assert by_status["update"]["package_id"] == "mod"
    assert by_status["update"]["actual_version"] == "2.0"
    assert by_status["rename"]["from_path"] == "new-name.txt"
    assert by_status["rename"]["to_path"] == "old-name.txt"
    assert by_status["remove"]["path"] == "missing.txt"
    assert by_status["add"]["jar_metadata"]["mod_ids"] == ["extra"]
    assert {item["provider"] for item in by_status["add"]["provider_candidates"]} == {
        "local",
        "modrinth",
    }


def test_import_defaults_local_but_accepts_explicit_exact_provider(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    game = outside(tmp_path)
    (game / "mods").mkdir(parents=True)
    content = jar({"fabric.mod.json": '{"id":"extra","version":"2.0"}'})
    (game / "mods/extra.jar").write_bytes(content)
    matcher = ExactMatch()
    service = ProjectService(tmp_path, provider_matcher=matcher)
    service.link("client", "dev", game)
    item = service.instance_diff(service.instance_context("dev"))["items"][0]

    service.import_package("dev", item["id"])
    operation = json.loads(
        (tmp_path / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"][0]
    assert operation["source"]["provider"] == "local"

    second_project = tmp_path.parent / f"{tmp_path.name}-provider-project"
    second_project.mkdir()
    metadata = second_project / "client/.pack"
    metadata.mkdir(parents=True)
    (metadata / "layer.json").write_text('{"schema":1,"id":"client","parents":[]}')
    second_game = outside(second_project)
    (second_game / "mods").mkdir(parents=True)
    (second_game / "mods/extra.jar").write_bytes(content)
    exact_service = ProjectService(second_project, provider_matcher=matcher)
    exact_service.link("client", "dev", second_game)
    candidate = exact_service.instance_diff(exact_service.instance_context("dev"))["items"][0][
        "provider_candidates"
    ][1]
    delta_id = exact_service.instance_diff(exact_service.instance_context("dev"))["items"][0]["id"]
    exact_service.import_package("dev", delta_id, provider_match=candidate)
    exact_operation = json.loads(
        (second_project / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"][0]
    assert exact_operation["source"]["provider"] == "direct_url"


def test_explicit_modrinth_failure_falls_back_to_local(layer_factory, tmp_path: Path) -> None:
    layer_factory("client", operations=[])
    game = outside(tmp_path)
    (game / "mods").mkdir(parents=True)
    content = jar({"fabric.mod.json": '{"id":"extra","version":"1"}'})
    (game / "mods/extra.jar").write_bytes(content)
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", game)
    delta_id = service.instance_diff(service.instance_context("dev"))["items"][0]["id"]

    service.import_package("dev", delta_id, provider_match="modrinth")

    operation = json.loads(
        (tmp_path / ".mcpack/features/dev/.pack/local-edits.json").read_text()
    )["operations"][0]
    assert operation["source"]["provider"] == "local"


def test_explain_focuses_package_file_dependency_and_group(layer_factory, tmp_path: Path) -> None:
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "activation": "available",
                "destination": "mods/mod.jar",
                "source": {"provider": "local", "location": "unused", "sha256": "0" * 64},
            },
            {"type": "group", "id": "core"},
            {"type": "dependency", "source": "core", "target": "mod"},
        ],
        payload={"config/a.txt": "value"},
    )
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", outside(tmp_path))

    package = service.explain("dev", "mod")
    assert package["kind"] == "package"
    assert package["dependencies"]["incoming"] == ["core"]
    assert {item["operation"]["type"] for item in package["provenance"]} == {
        "package",
        "dependency",
    }
    assert service.explain("dev", "core->mod")["kind"] == "dependency"
    assert service.explain("dev", "core")["kind"] == "group"
    assert service.explain("dev", "config/a.txt")["kind"] == "file"
