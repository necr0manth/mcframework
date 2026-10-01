from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mcframework.cli import main
from mcframework.project import ProjectService


def test_cli_sync_prune_preserves_disabled_declared_mod(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"artifact"
    (tmp_path / "artifact.jar").write_bytes(content)
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "kept",
                "kind": "mod",
                "activation": "disabled",
                "destination": "mods/kept.jar",
                "source": {
                    "provider": "local",
                    "location": "artifact.jar",
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            },
            {
                "type": "package",
                "id": "available",
                "kind": "mod",
                "activation": "available",
                "destination": "mods/available.jar",
                "source": {
                    "provider": "local",
                    "location": "artifact.jar",
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            },
            {"type": "prune", "glob": "mods/**"},
        ],
    )
    instance = tmp_path.parent / f"{tmp_path.name}-instance"
    (instance / "mods").mkdir(parents=True)
    kept = instance / "mods/kept.jar.disabled"
    available = instance / "mods/available.jar.disabled"
    unrelated = instance / "mods/unrelated.jar.disabled"
    kept.write_bytes(b"kept-local")
    available.write_bytes(b"available")
    unrelated.write_bytes(b"unrelated")
    service = ProjectService(tmp_path)
    service.link("client", "dev", instance)
    before = service.instance_diff(service.instance_context("dev"))
    assert [item["path"] for item in before["items"]] == [
        "mods/unrelated.jar.disabled"
    ]

    monkeypatch.chdir(tmp_path)
    assert main(["sync", "dev"]) == 0

    assert kept.read_bytes() == b"kept-local"
    assert not (instance / "mods/kept.jar").exists()
    assert available.read_bytes() == b"available"
    assert not unrelated.exists()


@pytest.mark.parametrize("removal", ["remove", "remove_paths"])
@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("pruned", [False, True])
def test_removed_inherited_file_survives_unless_explicitly_pruned(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    removal: str,
    ignored: bool,
    pruned: bool,
) -> None:
    path = "config/nested/inherited.txt"
    layer_factory("base", operations=[], payload={path: "parent content"})
    operations = [
        {"type": "remove", "path": path}
        if removal == "remove"
        else {"type": "remove_paths", "glob": "config/**"}
    ]
    if ignored:
        operations.append({"type": "ignore", "selector": {"kind": "subtree", "path": "config"}})
    if pruned:
        operations.append({"type": "prune", "glob": "config/**"})
    layer_factory("child", parents=["../base"], operations=operations)
    instance = tmp_path.parent / f"{tmp_path.name}-instance"
    service = ProjectService(tmp_path)
    service.link("base", "dev", instance)
    monkeypatch.chdir(tmp_path)
    assert main(["sync", "dev"]) == 0
    assert (instance / path).read_bytes() == b"parent content"

    service.unlink("dev")
    service.link("child", "dev", instance)
    context = service.instance_context("dev")
    assert path not in context.files
    before = service.instance_diff(context)
    assert [(item["path"], item["status"]) for item in before["items"]] == (
        [] if ignored else [(path, "add")]
    )
    assert before["summary"]["ignored"] == int(ignored)

    assert main(["sync", "dev"]) == 0
    if pruned:
        assert not (instance / path).exists()
    else:
        assert (instance / path).read_bytes() == b"parent content"
    after = service.instance_diff(service.instance_context("dev"), respect_ignore=False)
    assert [(item["path"], item["status"]) for item in after["items"]] == (
        [] if pruned else [(path, "add")]
    )


@pytest.mark.parametrize(
    "ignore_rule",
    [
        {"glob": "cache/**"},
        {"selector": {"kind": "exact", "path": "cache/nested/obsolete.txt"}},
        {"selector": {"kind": "subtree", "path": "cache"}},
    ],
    ids=["legacy-glob", "exact", "subtree"],
)
def test_prune_deletes_ignored_extra_but_preserves_desired_and_unrelated_files(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_rule: dict,
) -> None:
    layer_factory(
        "client",
        operations=[
            {"type": "prune", "glob": "cache/**"},
            {"type": "ignore", **ignore_rule},
        ],
        payload={"cache/managed.txt": "desired"},
    )
    instance = tmp_path.parent / f"{tmp_path.name}-instance"
    files = {
        "cache/nested/obsolete.txt": b"obsolete",
        "cache/managed.txt": b"desired",
        "cache-other/notes.txt": b"unrelated",
    }
    for path, content in files.items():
        target = instance / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    service = ProjectService(tmp_path)
    service.link("client", "dev", instance)
    context = service.instance_context("dev")
    before = service.instance_diff(context)
    assert [(item["path"], item["status"]) for item in before["items"]] == [
        ("cache-other/notes.txt", "add"),
    ]
    unfiltered = service.instance_diff(context, respect_ignore=False)
    assert {item["path"] for item in unfiltered["items"]} == {
        "cache/nested/obsolete.txt",
        "cache-other/notes.txt",
    }

    monkeypatch.chdir(tmp_path)
    assert main(["sync", "dev"]) == 0
    assert not (instance / "cache/nested/obsolete.txt").exists()
    assert (instance / "cache/managed.txt").read_bytes() == b"desired"
    assert (instance / "cache-other/notes.txt").read_bytes() == b"unrelated"
    after = service.instance_diff(service.instance_context("dev"), respect_ignore=False)
    assert [(item["path"], item["status"]) for item in after["items"]] == [
        ("cache-other/notes.txt", "add"),
    ]


@pytest.mark.parametrize(
    "ignore_rule",
    [
        {"glob": "config/**"},
        {"selector": {"kind": "exact", "path": "config/nested/value.txt"}},
        {"selector": {"kind": "subtree", "path": "config"}},
    ],
    ids=["legacy-glob", "exact", "subtree"],
)
@pytest.mark.parametrize("exists", [False, True], ids=["missing", "changed"])
def test_ignored_managed_file_is_still_created_or_updated(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_rule: dict,
    exists: bool,
) -> None:
    path = "config/nested/value.txt"
    layer_factory(
        "client", operations=[{"type": "ignore", **ignore_rule}], payload={path: "desired"}
    )
    instance = tmp_path.parent / f"{tmp_path.name}-instance"
    if exists:
        (instance / path).parent.mkdir(parents=True)
        (instance / path).write_bytes(b"local edit")
    service = ProjectService(tmp_path)
    service.link("client", "dev", instance)
    context = service.instance_context("dev")
    before = service.instance_diff(context)
    assert before["items"] == []
    assert before["summary"]["ignored"] == 1
    unfiltered = service.instance_diff(context, respect_ignore=False)
    assert [(item["path"], item["status"]) for item in unfiltered["items"]] == [
        (path, "change" if exists else "remove"),
    ]

    monkeypatch.chdir(tmp_path)
    assert main(["sync", "dev"]) == 0
    assert (instance / path).read_bytes() == b"desired"
    after = service.instance_diff(service.instance_context("dev"), respect_ignore=False)
    assert after["items"] == []
