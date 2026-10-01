from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcframework.artifacts import ArtifactCache, ArtifactRegistry, PackageUpdate
from mcframework.cli import main
from mcframework.errors import UpdateError
from mcframework.loader import WorkspaceLoader
from mcframework.model import ArtifactSource
from mcframework.resolver import Resolver
from mcframework.update import UpdateService


class UpdatingProvider:
    def __init__(self, content: bytes, update: PackageUpdate | None):
        self.content = content
        self.update = update

    def fetch(self, package):
        return self.content

    def find_update(self, package):
        return self.update


class FetchOnlyProvider:
    def fetch(self, package):
        return b"content"


def declaration(digest: str, *, policy: str = "latest_compatible") -> dict:
    return {
        "type": "package",
        "id": "mod",
        "version": "1",
        "activation": "enabled",
        "update_policy": policy,
        "destination": "mods/mod.jar",
        "source": {"provider": "mock", "location": "v1", "sha256": digest},
    }


def test_update_requires_policy_and_provider_capability(layer_factory, tmp_path: Path) -> None:
    digest = hashlib.sha256(b"old").hexdigest()
    layer_factory("fixed", operations=[declaration(digest, policy="fixed")])
    fixed = Resolver(WorkspaceLoader(tmp_path)).resolve("fixed").state.packages["mod"]
    registry = ArtifactRegistry()
    registry.register("mock", FetchOnlyProvider())
    service = UpdateService(registry, ArtifactCache(tmp_path / "cache"))
    with pytest.raises(UpdateError, match="fixed"):
        service.update(fixed)
    layer_factory("updatable", operations=[declaration(digest)])
    updatable = Resolver(WorkspaceLoader(tmp_path)).resolve("updatable").state.packages["mod"]
    with pytest.raises(UpdateError, match="capability"):
        service.update(updatable)


def test_verified_update_rewrites_exact_source(layer_factory, tmp_path: Path) -> None:
    old_digest = hashlib.sha256(b"old").hexdigest()
    new_content = b"new"
    new_digest = hashlib.sha256(new_content).hexdigest()
    layer_factory("pack", operations=[declaration(old_digest)])
    package = Resolver(WorkspaceLoader(tmp_path)).resolve("pack").state.packages["mod"]
    registry = ArtifactRegistry()
    registry.register(
        "mock",
        UpdatingProvider(new_content, PackageUpdate(ArtifactSource("mock", "v2", new_digest), "2")),
    )
    result = UpdateService(registry, ArtifactCache(tmp_path / "cache")).update(package)
    assert result.changed
    document = json.loads((tmp_path / "pack/.pack/operations.json").read_text())
    assert document["operations"][0]["version"] == "2"
    assert document["operations"][0]["source"]["sha256"] == new_digest


def test_bad_update_hash_does_not_rewrite_source(layer_factory, tmp_path: Path) -> None:
    old_digest = hashlib.sha256(b"old").hexdigest()
    metadata = layer_factory("pack", operations=[declaration(old_digest)]) / ".pack/operations.json"
    before = metadata.read_bytes()
    package = Resolver(WorkspaceLoader(tmp_path)).resolve("pack").state.packages["mod"]
    registry = ArtifactRegistry()
    registry.register(
        "mock",
        UpdatingProvider(b"different", PackageUpdate(ArtifactSource("mock", "v2", "0" * 64), "2")),
    )
    service = UpdateService(registry, ArtifactCache(tmp_path / "cache"))
    with pytest.raises(Exception, match="hash mismatch"):
        service.update(package)
    assert metadata.read_bytes() == before


def test_cli_resolve_build_and_local_sync(layer_factory, tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "artifact.jar"
    artifact.write_bytes(b"jar")
    digest = hashlib.sha256(b"jar").hexdigest()
    layer_factory(
        "pack",
        operations=[
            {
                "type": "package",
                "id": "mod",
                "activation": "enabled",
                "destination": "mods/mod.jar",
                "source": {"provider": "local", "location": "artifact.jar", "sha256": digest},
            },
            {"type": "prune", "glob": "mods/*.jar"},
        ],
        payload={"config/a": "value"},
    )
    metadata_path = tmp_path / "pack/.pack/operations.json"
    metadata_before = metadata_path.read_bytes()
    cache = tmp_path / "cache"
    assert main(["resolve", str(tmp_path), "pack", "--cache", str(cache)]) == 0
    assert '"mods/mod.jar"' in capsys.readouterr().out
    output = tmp_path / "build"
    assert main(["build", str(tmp_path), "pack", str(output), "--cache", str(cache)]) == 0
    assert not (output / ".mcpack-manifest.json").exists()
    assert (output / "mods/mod.jar").read_bytes() == b"jar"
    assert (output / "config/a").read_text() == "value"
    instance = tmp_path / "instance"
    (instance / "mods").mkdir(parents=True)
    (instance / "mods/old.jar").write_bytes(b"old")
    assert (
        main(["sync", str(tmp_path), "pack", "--local", str(instance), "--cache", str(cache)]) == 0
    )
    assert not (instance / "mods/old.jar").exists()
    assert (instance / "mods/mod.jar").read_bytes() == b"jar"
    assert metadata_path.read_bytes() == metadata_before


def test_legacy_cli_sync_preserves_inactive_package_aliases(
    layer_factory, tmp_path: Path
) -> None:
    artifact = tmp_path / "artifact.jar"
    artifact.write_bytes(b"jar")
    digest = hashlib.sha256(b"jar").hexdigest()

    def package(identity: str, activation: str) -> dict:
        return {
            "type": "package",
            "id": identity,
            "kind": "mod",
            "activation": activation,
            "destination": f"mods/{identity}.jar",
            "source": {
                "provider": "local",
                "location": "artifact.jar",
                "sha256": digest,
            },
        }

    layer_factory(
        "pack",
        operations=[
            package("kept", "disabled"),
            package("available", "available"),
            {"type": "prune", "glob": "mods/**"},
        ],
    )
    instance = tmp_path / "instance-disabled"
    (instance / "mods").mkdir(parents=True)
    kept = instance / "mods/kept.jar.disabled"
    available = instance / "mods/available.jar.disabled"
    unrelated = instance / "mods/unrelated.jar.disabled"
    kept.write_bytes(b"kept-local")
    available.write_bytes(b"available-local")
    unrelated.write_bytes(b"unrelated")

    assert main(["sync", str(tmp_path), "pack", "--local", str(instance)]) == 0

    assert kept.read_bytes() == b"kept-local"
    assert not (instance / "mods/kept.jar").exists()
    assert not (instance / "mods/available.jar").exists()
    assert available.read_bytes() == b"available-local"
    assert not unrelated.exists()


def test_cli_reports_domain_errors(layer_factory, tmp_path: Path, capsys) -> None:
    layer_factory("pack", operations=[])
    assert main(["update", str(tmp_path), "pack", "missing"]) == 2
    assert "not declared" in capsys.readouterr().err
