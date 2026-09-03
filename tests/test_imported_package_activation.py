from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from mcframework.errors import MetadataError, ResolutionError
from mcframework.project import ProjectService

OVERLAY = ".mcpack/features/dev"


@pytest.fixture
def imported_package(layer_factory, tmp_path: Path):
    layer_factory("base", operations=[])
    layer_factory("unrelated", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"extra","version":"1.0"}')
    (game / "mods/extra.jar").write_bytes(output.getvalue())
    service = ProjectService(tmp_path, provider_matcher=Mock(match=Mock(return_value=None)))
    service.link("base", "dev", game)

    def import_into(target: str) -> tuple[ProjectService, Path]:
        delta = service.instance_diff(service.instance_context("dev"), enrich_actual=False)
        item = next(item for item in delta["items"] if item["path"] == "mods/extra.jar")
        service.import_package("dev", item["id"], target, provider_match="local")
        return service, tmp_path / target / ".pack/local-edits.json"

    return import_into


@pytest.mark.parametrize("target", [None, OVERLAY])
def test_overlay_import_toggle_edits_owning_declaration(imported_package, tmp_path: Path, target):
    service, declaration = imported_package(OVERLAY)
    original = json.loads(declaration.read_text())
    base_before = (tmp_path / "base/.pack/operations.json").read_bytes()
    artifact = next((declaration.parent / "artifacts").glob("*.jar"))
    artifact_before = artifact.read_bytes()

    for state in ("disabled", "enabled", "disabled", "enabled"):
        service.set_activation("dev", "extra", state, target)
        context = service.instance_context("dev")
        assert context.resolution.state.packages["extra"].activation.value == state
        assert ("mods/extra.jar" in context.files) == (state == "enabled")
        expected = json.loads(json.dumps(original))
        expected["operations"][0]["activation"] = state
        assert json.loads(declaration.read_text()) == expected
        assert (tmp_path / "base/.pack/operations.json").read_bytes() == base_before
        assert not (tmp_path / "base/.pack/local-edits.json").exists()
        assert artifact.read_bytes() == artifact_before


@pytest.mark.parametrize("target", [None, OVERLAY])
def test_inherited_import_toggle_writes_override_without_changing_parent(
    imported_package, tmp_path: Path, target
):
    service, declaration = imported_package("base")
    before = declaration.read_bytes()

    for state in ("disabled", "enabled", "disabled"):
        service.set_activation("dev", "extra", state, target)
        assert service.instance_context("dev").resolution.state.packages[
            "extra"
        ].activation.value == state
        assert declaration.read_bytes() == before
        assert json.loads((tmp_path / OVERLAY / ".pack/local-edits.json").read_text()) == {
            "operations": [{"type": "activation", "id": "extra", "state": state}]
        }


def test_explicit_base_import_owner_edits_declaration(imported_package):
    service, declaration = imported_package("base")
    for state in ("disabled", "enabled"):
        service.set_activation("dev", "extra", state, "base")
        assert service.instance_context("dev").resolution.state.packages[
            "extra"
        ].activation.value == state
        operations = json.loads(declaration.read_text())["operations"]
        assert len(operations) == 1
        assert operations[0]["type"] == "package"
        assert operations[0]["activation"] == state
        assert not service.state.feature_root("dev").exists()


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        ("base", ResolutionError, "same-layer activation"),
        ("unrelated", MetadataError, "not writable"),
    ],
)
def test_invalid_explicit_target_is_rejected_without_leaving_edits(
    imported_package, tmp_path: Path, target, error, message
):
    service, declaration = imported_package(OVERLAY)
    before = declaration.read_bytes()
    base_before = (tmp_path / "base/.pack/operations.json").read_bytes()

    with pytest.raises(error, match=message):
        service.set_activation("dev", "extra", "disabled", target)

    assert declaration.read_bytes() == before
    assert (tmp_path / "base/.pack/operations.json").read_bytes() == base_before
    assert not (tmp_path / target / ".pack/local-edits.json").exists()
    assert service.instance_context("dev").resolution.state.packages[
        "extra"
    ].activation.value == "enabled"


@pytest.mark.parametrize("activation_first", [False, True])
def test_imported_package_still_rejects_same_layer_activation_operation(
    imported_package, activation_first
):
    service, declaration = imported_package(OVERLAY)
    document = json.loads(declaration.read_text())
    operation = {"type": "activation", "id": "extra", "state": "disabled"}
    document["operations"].insert(0 if activation_first else 1, operation)
    declaration.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ResolutionError, match="same-layer activation"):
        service.instance_context("dev")
