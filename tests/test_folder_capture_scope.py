from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.project import ProjectService


class NoMatch:
    def match(self, content: bytes) -> None:
        return None


def capture_fixture(layer_factory, tmp_path: Path, path: str):
    root = layer_factory("client", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    (game / "logs").mkdir()
    (game / "ignored").mkdir()
    (game / "logs/latest.log").write_text("old log")
    (game / "ignored/old.txt").write_text("ignored")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("fabric.mod.json", '{"id":"selected","version":"1"}')
    selected = game / "mods/selected.jar"
    content = archive.getvalue()
    selected.write_bytes(content)
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", game)
    service.add_ignore("dev", "ignored", "subtree", "client")
    plan = service.explorer_plan("dev", path, "client")
    decisions = {
        item["id"]: {
            "mode": "package", "provider_match": "local",
            "expected_sha256": item["expected_sha256"],
        }
        for item in plan["items"]
    }
    return service, root, game, selected, content, decisions


@pytest.mark.parametrize("path", ["mods", "mods/selected.jar"])
@pytest.mark.parametrize("churn", ["logs", "ignored", "root"])
def test_reviewed_capture_ignores_unrelated_instance_directory_churn(
    layer_factory, tmp_path: Path, path: str, churn: str
):
    service, _, game, selected, content, decisions = capture_fixture(layer_factory, tmp_path, path)
    watched = game if churn == "root" else game / churn
    previous = watched.stat()
    if churn == "root":
        (game / "screenshots").mkdir()
    elif churn == "logs":
        (game / "logs/latest.log").unlink()
        (game / "logs/rotated.log").write_text("new log")
    else:
        (game / "ignored/old.txt").unlink()
        (game / "ignored/new.txt").write_text("new ignored file")
    # Ensure the old global directory watch rejects this independent of FS timing.
    os.utime(watched, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    try:
        service.capture_explorer_path("dev", path, "client", decisions)
        assert service.instance_context("dev").files["mods/selected.jar"] == content
        assert selected.read_bytes() == content
        if churn == "logs":
            assert (game / "logs/rotated.log").read_text() == "new log"
        elif churn == "ignored":
            assert (game / "ignored/new.txt").read_text() == "new ignored file"
    finally:
        service.close()


def test_single_file_capture_ignores_new_unselected_sibling(layer_factory, tmp_path: Path):
    path = "mods/selected.jar"
    service, _, game, _, content, decisions = capture_fixture(layer_factory, tmp_path, path)
    sibling = game / "mods/new.jar"
    sibling.write_bytes(content)
    try:
        service.capture_explorer_path("dev", path, "client", decisions)
        assert service.instance_context("dev").files["mods/selected.jar"] == content
        assert "mods/new.jar" not in service.instance_context("dev").files
        assert sibling.read_bytes() == content
        # Refreshing the explorer still discovers the uncaptured sibling.
        remaining = service.explorer_plan("dev", "mods", "client")["items"]
        assert [item["path"] for item in remaining] == ["mods/new.jar"]
    finally:
        service.close()


@pytest.mark.parametrize("change", ["add", "remove", "type", "hash", "metadata"])
def test_folder_capture_rejects_changes_to_reviewed_sources_or_semantic_context(
    layer_factory, tmp_path: Path, change: str
):
    service, root, game, selected, _, decisions = capture_fixture(layer_factory, tmp_path, "mods")
    if change == "add":
        (game / "mods/new.jar").write_bytes(selected.read_bytes())
    elif change == "remove":
        selected.unlink()
    elif change == "type":
        selected.unlink()
        selected.mkdir()
    elif change == "hash":
        original = selected.read_bytes()
        selected.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        assert selected.stat().st_size == len(original)
    else:
        (root / ".pack/operations.json").write_text(json.dumps({"operations": [{
            "type": "put", "path": "config/changed.txt", "content": "new desired content",
        }]}))
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    try:
        with pytest.raises(MetadataError, match="changed before capture|changed before mutation"):
            service.capture_explorer_path("dev", "mods", "client", decisions)
        after = {
            path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
        }
        assert after == before
        assert service.instance_context("dev").resolution.state.packages == {}
    finally:
        service.close()
