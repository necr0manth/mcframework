from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcframework.artifacts import (
    ArtifactCache,
    ArtifactRegistry,
    ArtifactResolver,
    DirectUrlArtifactProvider,
    LocalArtifactProvider,
)
from mcframework.build import Builder
from mcframework.errors import ArtifactError, MergeConflict
from mcframework.loader import WorkspaceLoader
from mcframework.manifest import manifest_bytes, materialized_files
from mcframework.model import ArtifactSource, PackageDeclaration
from mcframework.resolver import Resolver


def package_operation(location: str, digest: str, *, destination: str = "mods/a.jar") -> dict:
    return {
        "type": "package",
        "id": "a",
        "activation": "enabled",
        "destination": destination,
        "source": {"provider": "local", "location": location, "sha256": digest},
    }


def setup(tmp_path: Path, layer_factory, content: bytes = b"artifact"):
    artifact = tmp_path / "artifact.jar"
    artifact.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    layer_factory(
        "pack", operations=[package_operation("artifact.jar", digest)], payload={"config/a": "x"}
    )
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("pack")
    registry = ArtifactRegistry()
    registry.register("local", LocalArtifactProvider(tmp_path))
    artifacts = ArtifactResolver(registry, ArtifactCache(tmp_path / "cache"))
    return resolution, artifacts, digest


def test_local_artifact_cached_by_hash(layer_factory, tmp_path: Path) -> None:
    resolution, artifacts, digest = setup(tmp_path, layer_factory)
    first = artifacts.resolve(resolution)
    cache_path = Path(first.artifacts[0].cache_path)
    assert cache_path.name == digest
    (tmp_path / "artifact.jar").unlink()
    second = artifacts.resolve(resolution)
    assert second.artifacts[0].sha256 == digest


def test_hash_mismatch_and_missing_artifact_fail(layer_factory, tmp_path: Path) -> None:
    artifact = tmp_path / "bad.jar"
    artifact.write_bytes(b"wrong")
    layer_factory("bad", operations=[package_operation("bad.jar", "0" * 64)])
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("bad")
    registry = ArtifactRegistry()
    registry.register("local", LocalArtifactProvider(tmp_path))
    with pytest.raises(ArtifactError, match="hash mismatch"):
        ArtifactResolver(registry, ArtifactCache(tmp_path / "cache")).resolve(resolution)
    artifact.unlink()
    with pytest.raises(ArtifactError, match="does not exist"):
        ArtifactResolver(registry, ArtifactCache(tmp_path / "cache2")).resolve(resolution)


def test_traversal_digest_is_rejected_without_touching_victim(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    crafted_digest = "../" * 20 + "abcd"
    assert len(crafted_digest) == 64
    package = PackageDeclaration(
        identity="crafted",
        logical_identity="crafted",
        kind="mod",
        source=ArtifactSource("local", "unused", crafted_digest),
        destination="mods/crafted.jar",
    )
    with pytest.raises(ArtifactError, match="lowercase hexadecimal"):
        ArtifactCache(tmp_path / "cache").obtain(package, LocalArtifactProvider(tmp_path))
    assert victim.read_bytes() == b"keep"


def test_direct_url_provider_supports_exact_file_url(layer_factory, tmp_path: Path) -> None:
    artifact = tmp_path / "download.jar"
    artifact.write_bytes(b"download")
    digest = hashlib.sha256(b"download").hexdigest()
    layer_factory(
        "pack",
        operations=[
            {
                **package_operation(artifact.as_uri(), digest),
                "source": {
                    "provider": "direct_url",
                    "location": artifact.as_uri(),
                    "sha256": digest,
                },
            }
        ],
    )
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("pack")
    registry = ArtifactRegistry()
    registry.register("direct_url", DirectUrlArtifactProvider())
    result = ArtifactResolver(registry, ArtifactCache(tmp_path / "cache")).resolve(resolution)
    assert Path(result.artifacts[0].cache_path).read_bytes() == b"download"


def test_direct_url_provider_wraps_transport_error(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"missing").hexdigest()
    package = PackageDeclaration(
        identity="missing",
        logical_identity="missing",
        kind="mod",
        source=ArtifactSource("direct_url", (tmp_path / "missing.jar").as_uri(), digest),
        destination="mods/missing.jar",
    )
    with pytest.raises(ArtifactError, match="cannot download"):
        DirectUrlArtifactProvider().fetch(package)


def test_build_is_deterministic_and_prunes_stale_files(layer_factory, tmp_path: Path) -> None:
    resolution, artifacts, digest = setup(tmp_path, layer_factory)
    output = tmp_path / "output"
    output.mkdir()
    (output / "stale").write_text("old")
    builder = Builder(artifacts)
    first, report = builder.build(resolution, output)
    assert report.deleted == ("stale",)
    assert (output / "mods/a.jar").read_bytes() == b"artifact"
    manifest_first = (output / ".mcpack-manifest.json").read_bytes()
    second, report2 = builder.build(resolution, output)
    assert report2.updated == ()
    assert (output / ".mcpack-manifest.json").read_bytes() == manifest_first
    document = json.loads(manifest_first)
    assert [item["path"] for item in document["files"]] == ["config/a", "mods/a.jar"]
    assert document["artifacts"][0]["sha256"] == digest
    assert first.manifest == second.manifest


def test_package_destination_conflicts_with_payload(layer_factory, tmp_path: Path) -> None:
    resolution, artifacts, _ = setup(tmp_path, layer_factory)
    # Replace package destination by constructing a second layer setup.
    content = b"artifact"
    digest = hashlib.sha256(content).hexdigest()
    layer_factory(
        "collision",
        operations=[package_operation("artifact.jar", digest, destination="config/a")],
        payload={"config/a": "x"},
    )
    collision = artifacts.resolve(Resolver(WorkspaceLoader(tmp_path)).resolve("collision"))
    with pytest.raises(MergeConflict, match="conflicts"):
        materialized_files(collision)


def test_manifest_redacts_parameter_values(layer_factory, tmp_path: Path) -> None:
    resolution, artifacts, _ = setup(tmp_path, layer_factory)
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("pack", {"api_secret": "hidden"})
    prepared, _ = Builder(artifacts).prepare(resolution)
    assert b"hidden" not in manifest_bytes(prepared)
    assert b"<redacted>" in manifest_bytes(prepared)


def test_manifest_is_checkout_root_independent(tmp_path: Path) -> None:
    manifests = []
    for checkout_name in ("checkout-one", "checkout-two"):
        workspace = tmp_path / checkout_name
        metadata = workspace / "pack/.pack"
        metadata.mkdir(parents=True)
        (metadata / "layer.json").write_text(
            '{"schema": 1, "id": "pack", "parents": []}', encoding="utf-8"
        )
        (metadata / "files.json").write_text(
            '{"operations": [{"type": "put", "path": "generated.txt", "content": "same"}]}',
            encoding="utf-8",
        )
        (workspace / "pack/payload.txt").write_text("same", encoding="utf-8")
        resolution = Resolver(WorkspaceLoader(workspace)).resolve("pack")
        prepared, _ = materialized_files(resolution)
        manifests.append(manifest_bytes(prepared))
    assert manifests[0] == manifests[1]
    assert str(tmp_path).encode() not in manifests[0]


def test_manifest_redacts_common_credential_names(layer_factory, tmp_path: Path) -> None:
    layer_factory("pack", operations=[], payload={"file": "content"})
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve(
        "pack",
        {
            "password": "password-value",
            "token": "token-value",
            "api_key": "api-key-value",
            "nested": {"privateKey": "private-key-value"},
            "secret_reference": {"$secret": "vault-entry"},
            "public": "visible",
        },
    )
    prepared, _ = materialized_files(resolution)
    output = manifest_bytes(prepared)
    for leaked in (
        b"password-value",
        b"token-value",
        b"api-key-value",
        b"private-key-value",
        b"vault-entry",
    ):
        assert leaked not in output
    assert b"visible" in output
