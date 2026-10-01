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
    PackageSelection,
)
from mcframework.build import Builder
from mcframework.errors import ArtifactError, MergeConflict
from mcframework.loader import WorkspaceLoader
from mcframework.manifest import manifest_bytes, materialized_files
from mcframework.model import Activation, ArtifactSource, PackageDeclaration, PackState, Resolution
from mcframework.resolver import Resolver
from mcframework.sync import Synchronizer, disabled_package_aliases
from mcframework.transport import LocalFilesystemInstance


def package_operation(location: str, digest: str, *, destination: str = "mods/a.jar") -> dict:
    return {
        "type": "package",
        "id": "a",
        "activation": "enabled",
        "destination": destination,
        "source": {"provider": "local", "location": location, "sha256": digest},
    }


def setup(tmp_path: Path, layer_factory, content: bytes = b"artifact"):
    digest = hashlib.sha256(content).hexdigest()
    layer_factory(
        "pack",
        operations=[package_operation(".pack/artifact.jar", digest)],
        payload={"config/a": "x"},
    )
    artifact = tmp_path / "pack" / ".pack" / "artifact.jar"
    artifact.write_bytes(content)
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
    (tmp_path / "pack" / ".pack" / "artifact.jar").unlink()
    second = artifacts.resolve(resolution)
    assert second.artifacts[0].sha256 == digest


def test_hash_mismatch_and_missing_artifact_fail(layer_factory, tmp_path: Path) -> None:
    layer_factory("bad", operations=[package_operation(".pack/bad.jar", "0" * 64)])
    artifact = tmp_path / "bad" / ".pack" / "bad.jar"
    artifact.write_bytes(b"wrong")
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
    assert (output / "config/a").read_bytes() == b"x"
    assert not (output / ".mcpack-manifest.json").exists()
    (output / ".mcpack-manifest.json").write_text("old manifest")
    second, report2 = builder.build(resolution, output)
    assert report2.updated == ()
    assert report2.created == ()
    assert report2.deleted == (".mcpack-manifest.json",)
    assert report2.unchanged == ("config/a", "mods/a.jar")
    assert not (output / ".mcpack-manifest.json").exists()
    document = json.loads(manifest_bytes(first))
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
        operations=[package_operation(".pack/artifact.jar", digest, destination="config/a")],
        payload={"config/a": "x"},
    )
    (tmp_path / "collision" / ".pack" / "artifact.jar").write_bytes(content)
    collision = artifacts.resolve(Resolver(WorkspaceLoader(tmp_path)).resolve("collision"))
    with pytest.raises(MergeConflict, match="conflicts"):
        materialized_files(collision)


def test_manifest_redacts_parameter_values(layer_factory, tmp_path: Path) -> None:
    resolution, artifacts, _ = setup(tmp_path, layer_factory)
    resolution = Resolver(WorkspaceLoader(tmp_path)).resolve("pack", {"api_secret": "hidden"})
    prepared, _ = Builder(artifacts).prepare(resolution)
    assert b"hidden" not in manifest_bytes(prepared)
    assert b"<redacted>" in manifest_bytes(prepared)


def test_available_package_hydrates_alias_without_becoming_build_payload(tmp_path: Path) -> None:
    content = b"available package bytes"
    digest = hashlib.sha256(content).hexdigest()
    package = PackageDeclaration(
        identity="optional-mod",
        logical_identity="optional-mod",
        kind="mod",
        source=ArtifactSource("modrinth", "optional-mod", ""),
        destination="",
        activation=Activation.AVAILABLE,
        version="1.2.3",
    )
    selection = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/optional-mod.jar",
        "optional-mod.jar",
        digest,
    )
    cache = ArtifactCache(tmp_path / "cache")
    cache.seed_exact_match(package, selection, content)
    builder = Builder(ArtifactResolver(ArtifactRegistry(), cache))
    initial = Resolution(PackState(packages={package.identity: package}), (), ())

    resolved, files = builder.prepare(initial)

    hydrated = resolved.state.packages[package.identity]
    alias = "mods/optional-mod.jar.disabled"
    assert hydrated.activation is Activation.AVAILABLE
    assert hydrated.destination == "mods/optional-mod.jar"
    assert files == {}
    assert resolved.active_packages == ()
    assert disabled_package_aliases(resolved.state.packages) == (alias,)

    instance = tmp_path / "instance"
    disabled = instance / alias
    disabled.parent.mkdir(parents=True)
    disabled.write_bytes(content)
    report = Synchronizer().sync(
        files,
        LocalFilesystemInstance(instance),
        prune=("mods/**",),
        preserve=disabled_package_aliases(resolved.state.packages),
    )

    assert report.deleted == ()
    assert disabled.read_bytes() == content


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
