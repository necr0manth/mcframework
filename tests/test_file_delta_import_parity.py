from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.package_inspect import ProviderMatch, file_hashes
from mcframework.project import ProjectService


class Matcher:
    def __init__(self, confidence: str | None) -> None:
        self.confidence = confidence
        self.calls = 0

    def match(self, content: bytes) -> ProviderMatch | None:
        self.calls += 1
        if self.confidence is None:
            return None
        return ProviderMatch(
            "modrinth",
            "project-id",
            "version-id",
            "2.0",
            "https://cdn.example.invalid/selected.jar",
            file_hashes(content),
            confidence=self.confidence,
            project_slug="selected-slug",
            filename="selected.jar",
        )


def jar(version: str) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "selected", "version": version}))
    return content.getvalue()


def prepare(layer_factory, project: Path, changed: bool, confidence: str | None):
    layer_factory(
        "client",
        operations=[],
        payload={"mods/selected.jar": jar("1.0")} if changed else {},
    )
    game = project.parent / f"{project.name}-game"
    (game / "mods").mkdir(parents=True)
    content = jar("2.0")
    (game / "mods/selected.jar").write_bytes(content)
    matcher = Matcher(confidence)
    service = ProjectService(project, provider_matcher=matcher)
    service.link("client", "dev", game)
    return service, matcher, content


def detail_from_both_entries(service: ProjectService, matcher: Matcher, entry: str):
    explorer_entry = service.explorer_snapshot("dev").entries["mods/selected.jar"]
    plan = service.explorer_plan("dev", "mods/selected.jar")
    planned = plan["items"][0]
    assert explorer_entry["delta_id"] == planned["id"]
    assert planned["provider_candidates"][0]["provider"] == "local"
    assert planned["provider_lookup"] in {"pending", "ready"}
    delta = service.instance_diff(service.instance_context("dev"), enrich_actual=False)
    selected = next(item for item in delta["items"] if item["path"] == planned["path"])
    assert planned["id"] == selected["id"]
    deadline = time.monotonic() + 1
    while True:
        from_files = service.delta_detail("dev", planned["id"])
        if from_files["provider_lookup"] != "pending" or time.monotonic() >= deadline:
            break
        time.sleep(0.001)
    from_delta = service.delta_detail("dev", selected["id"])
    assert from_files == from_delta
    assert matcher.calls == 1, "the same selected content must use one provider lookup"
    assert from_files["actual_sha256"] == planned["expected_sha256"]
    assert from_files["jar_metadata"]["mod_ids"] == ["selected"]
    assert from_files["capture_modes"] == ["package", "payload"]
    return from_files if entry == "files" else from_delta


@pytest.mark.parametrize("entry", ["files", "delta"])
@pytest.mark.parametrize("changed", [False, True], ids=["new", "changed"])
@pytest.mark.parametrize("mode", ["exact", "local", "payload"])
def test_files_and_delta_share_detail_and_import_results(
    layer_factory,
    tmp_path: Path,
    entry: str,
    changed: bool,
    mode: str,
) -> None:
    service, matcher, content = prepare(
        layer_factory,
        tmp_path,
        changed,
        "exact" if mode == "exact" else None,
    )
    detail = detail_from_both_entries(service, matcher, entry)
    assert detail["status"] == ("change" if changed else "add")
    target = service.state.feature_root("dev")
    target_layer = target.relative_to(tmp_path).as_posix()
    digest = hashlib.sha256(content).hexdigest()
    if mode == "payload":
        service.capture_delta(
            "dev",
            detail["id"],
            target_layer,
            expected_sha256=detail["actual_sha256"],
            require_expected_sha256=True,
        )
        assert (target / "mods/selected.jar").read_bytes() == content
        assert not (target / ".pack/local-edits.json").exists()
    else:
        candidates = detail["provider_candidates"]
        assert [candidate["provider"] for candidate in candidates] == (
            ["local", "modrinth"] if mode == "exact" else ["local"]
        )
        service.import_package(
            "dev",
            detail["id"],
            target_layer,
            candidates[-1],
            expected_sha256=detail["actual_sha256"],
        )
        operations = json.loads((target / ".pack/local-edits.json").read_text())["operations"]
        operation = next(item for item in operations if item["type"] == "package")
        if changed:
            assert {"type": "remove", "path": "mods/selected.jar"} in operations
        assert operation.get("destination") == ("mods/selected.jar" if mode == "local" else None)
        assert operation["source"]["provider"] == ("modrinth" if mode == "exact" else "local")
        assert operation["update_policy"] == "fixed"
        assert operation["version"] == "2.0"
        artifact = target / ".pack/artifacts" / f"{digest}.jar"
        if mode == "local":
            assert operation["source"]["sha256"] == digest
            assert artifact.read_bytes() == content
        else:
            assert operation["source"]["location"] == "selected-slug"
            assert not artifact.exists()
    assert matcher.calls == 1
    context = service.instance_context("dev")
    assert context.files["mods/selected.jar"] == content
    assert service.instance_diff(context, enrich_actual=False)["items"] == []


@pytest.mark.parametrize("entry", ["files", "delta"])
@pytest.mark.parametrize("changed", [False, True], ids=["new", "changed"])
def test_explicit_fuzzy_selection_requires_matching_hash_for_both_entries(
    layer_factory,
    tmp_path: Path,
    entry: str,
    changed: bool,
) -> None:
    service, matcher, content = prepare(layer_factory, tmp_path, changed, "fuzzy")
    detail = detail_from_both_entries(service, matcher, entry)
    candidate = detail["provider_candidates"][-1]
    assert candidate["confidence"] == "fuzzy"
    # The protocol treats an explicit candidate document as user confirmation.
    # The common UI gates this submission; the backend also verifies its hash.
    with pytest.raises(MetadataError, match="do not match"):
        service.import_package(
            "dev",
            detail["id"],
            provider_match={**candidate, "hashes": {"sha256": "0" * 64}},
            expected_sha256=detail["actual_sha256"],
        )
    target = service.state.feature_root("dev")
    assert not (target / ".pack/local-edits.json").exists()
    assert not (target / ".pack/artifacts").exists()
    service.import_package(
        "dev",
        detail["id"],
        provider_match=candidate,
        expected_sha256=detail["actual_sha256"],
    )
    operations = json.loads((target / ".pack/local-edits.json").read_text())["operations"]
    operation = next(item for item in operations if item["type"] == "package")
    assert operation["source"]["provider"] == "modrinth"
    assert operation["source"]["location"] == "selected-slug"
    assert operation["version"] == "2.0"
    assert service.instance_context("dev").files["mods/selected.jar"] == content


@pytest.mark.parametrize("provider", ["exact", "local"])
def test_changed_package_import_preserves_identity_and_declaration_metadata(
    layer_factory,
    tmp_path: Path,
    provider: str,
) -> None:
    original = jar("1.0")
    (tmp_path / "original.jar").write_bytes(original)
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "declared-id",
                "logical_id": "selected",
                "alias": "mod-alias",
                "kind": "mod",
                "activation": "enabled",
                "version": "1.0",
                "destination": "mods/selected.jar",
                "automatic_requires": ["dependency"],
                "source": {
                    "provider": "local",
                    "location": "original.jar",
                    "sha256": hashlib.sha256(original).hexdigest(),
                },
            }
        ],
    )
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    content = jar("2.0")
    (game / "mods/selected.jar").write_bytes(content)
    matcher = Matcher("exact" if provider == "exact" else None)
    service = ProjectService(tmp_path, provider_matcher=matcher)
    service.link("client", "dev", game)
    entry = service.explorer_snapshot("dev").entries["mods/selected.jar"]
    detail = service.delta_detail("dev", entry["delta_id"])
    assert detail["status"] == "update"
    assert detail["capture_modes"] == ["package"]
    service.import_package(
        "dev",
        detail["id"],
        provider_match=detail["provider_candidates"][-1],
        expected_sha256=detail["actual_sha256"],
    )
    context = service.instance_context("dev")
    assert list(context.resolution.state.packages) == ["declared-id"]
    package = context.resolution.state.packages["declared-id"]
    assert package.logical_identity == "selected"
    assert package.alias == "mod-alias"
    assert package.automatic_requires == ("dependency",)
    assert package.version == "2.0"
    assert context.files["mods/selected.jar"] == content


@pytest.mark.parametrize("fail_reconciliation", [False, True])
def test_payload_conversion_in_target_preserves_other_files_and_rolls_back(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_reconciliation: bool,
) -> None:
    service, matcher, content = prepare(layer_factory, tmp_path, True, None)
    target = tmp_path / "client"
    other = target / "unrelated.txt"
    other.write_text("preserve")
    metadata_path = target / ".pack/local-edits.json"
    metadata_before = b'{"operations": [{"type": "ignore", "glob": "logs/**"}]}\n'
    metadata_path.write_bytes(metadata_before)
    old_content = (target / "mods/selected.jar").read_bytes()
    detail = detail_from_both_entries(service, matcher, "files")
    context = service.instance_context
    calls = 0

    def fail_after_mutation(instance: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MetadataError("simulated reconciliation failure")
        return context(instance)

    if fail_reconciliation:
        monkeypatch.setattr(service, "instance_context", fail_after_mutation)
        with pytest.raises(MetadataError, match="simulated"):
            service.import_package(
                "dev", detail["id"], "client", expected_sha256=detail["actual_sha256"]
            )
        assert (target / "mods/selected.jar").read_bytes() == old_content
        assert metadata_path.read_bytes() == metadata_before
        assert not list((target / ".pack/artifacts").glob("*.jar"))
    else:
        service.import_package(
            "dev", detail["id"], "client", expected_sha256=detail["actual_sha256"]
        )
        assert not (target / "mods/selected.jar").exists()
        assert service.instance_context("dev").files["mods/selected.jar"] == content
        operations = json.loads(metadata_path.read_text())["operations"]
        assert {"type": "ignore", "glob": "logs/**"} in operations
    assert other.read_text() == "preserve"


def test_bare_modrinth_request_does_not_implicitly_accept_fuzzy_match(
    layer_factory, tmp_path: Path
):
    service, matcher, _ = prepare(layer_factory, tmp_path, False, "fuzzy")
    detail = detail_from_both_entries(service, matcher, "files")
    service.import_package(
        "dev",
        detail["id"],
        provider_match="modrinth",
        expected_sha256=detail["actual_sha256"],
    )
    target = service.state.feature_root("dev")
    operation = json.loads((target / ".pack/local-edits.json").read_text())["operations"][0]
    assert operation["source"]["provider"] == "local"


@pytest.mark.parametrize("filename", ["operations.json", "packages.json", "local-edits.json"])
@pytest.mark.parametrize(
    ("provider", "fail_reconciliation", "artifact_preexisting"),
    [
        ("exact", False, False),
        ("local", False, False),
        ("exact", True, False),
        ("local", True, False),
        ("local", True, True),
    ],
)
def test_import_updates_owning_package_declaration_and_rolls_back(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    filename: str,
    fail_reconciliation: bool,
    artifact_preexisting: bool,
) -> None:
    original = jar("1.0")
    (tmp_path / "original.jar").write_bytes(original)
    declaration = {
        "type": "package",
        "id": "declared-id",
        "logical_id": "selected",
        "alias": "mod-alias",
        "kind": "mod",
        "activation": "enabled",
        "version": "1.0",
        "destination": "mods/selected.jar",
        "automatic_requires": ["dependency"],
        "source": {
            "provider": "local",
            "location": "original.jar",
            "sha256": hashlib.sha256(original).hexdigest(),
        },
        "custom_metadata": {"notes": ["preserve this declaration"]},
    }
    before_op = {"type": "ignore", "glob": "logs/**"}
    after_op = {"type": "ignore", "glob": "screenshots/**"}
    target = layer_factory(
        "client",
        metadata_files={filename: [before_op, declaration, after_op]},
        payload={"unrelated.txt": "preserve"},
    )
    owner = target / ".pack" / filename
    document = json.loads(owner.read_text())
    owner_before = (json.dumps(document, indent=4) + "\n").encode()
    owner.write_bytes(owner_before)
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    content = jar("2.0")
    (game / "mods/selected.jar").write_bytes(content)
    service = ProjectService(
        tmp_path, provider_matcher=Matcher("exact" if provider == "exact" else None)
    )
    service.link("client", "dev", game)
    entry = service.explorer_snapshot("dev").entries["mods/selected.jar"]
    detail = service.delta_detail("dev", entry["delta_id"])
    context_before = service.instance_context("dev")
    package = context_before.resolution.state.packages["declared-id"]
    assert Path(package.source_file) == owner
    assert package.source_index == 1
    artifact = target / ".pack/artifacts" / f"{hashlib.sha256(content).hexdigest()}.jar"
    if artifact_preexisting:
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(content)

    def import_selected():
        service.import_package(
            "dev",
            detail["id"],
            "client",
            detail["provider_candidates"][-1],
            expected_sha256=detail["actual_sha256"],
        )

        if fail_reconciliation:
            calls = 0
            instance_context_before = service.instance_context

            def fail_after_mutation(instance: str, **kwargs):
                nonlocal calls
                calls += 1
                if calls == (3 if provider == "exact" else 2):
                    raise MetadataError("simulated reconciliation failure")
                if provider == "exact":
                    return instance_context_before(instance, **kwargs)
                return context_before

            with monkeypatch.context() as patch:
                patch.setattr(service, "instance_context", fail_after_mutation)
                with pytest.raises(MetadataError, match="simulated"):
                    import_selected()
            assert owner.read_bytes() == owner_before
            assert artifact.exists() is artifact_preexisting
            if artifact_preexisting:
                assert artifact.read_bytes() == content
            assert service.instance_context("dev").files["mods/selected.jar"] == original
        else:
            import_selected()
            changed = json.loads(owner.read_text())
            updated = changed["operations"][1]
            expected_source = (
                {"provider": "modrinth", "location": "selected-slug"}
                if provider == "exact"
                else {
                    "provider": "local",
                    "location": artifact.relative_to(tmp_path).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
            assert updated == {
                **declaration,
                "source": expected_source,
                "version": "2.0",
                "update_policy": "fixed",
            }
            assert changed == {**document, "operations": [before_op, updated, after_op]}
            context = service.instance_context("dev")
            assert list(context.resolution.state.packages) == ["declared-id"]
            package = context.resolution.state.packages["declared-id"]
            assert Path(package.source_file) == owner
            assert package.source_index == 1
            assert context.files["mods/selected.jar"] == content
            if provider == "local":
                assert artifact.read_bytes() == content
    if filename != "local-edits.json":
        assert not (target / ".pack/local-edits.json").exists()
    assert (target / "unrelated.txt").read_text() == "preserve"
    assert (tmp_path / "original.jar").read_bytes() == original
