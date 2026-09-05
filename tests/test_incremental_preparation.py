from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from mcframework import generations
from mcframework.artifacts import ArtifactCache
from mcframework.build import Builder
from mcframework.errors import ArtifactError, MergeConflict, ResolutionError, UnsafePathError
from mcframework.generations import generation
from mcframework.local_state import LinkRecord
from mcframework.project import ProjectService


def fixture(layer_factory, tmp_path: Path):
    operations = []
    for index in range(4):
        content = bytes([index]) * 65536
        source = tmp_path / f"artifact-{index}.jar"
        source.write_bytes(content)
        operations.append(
            {
                "type": "package",
                "id": f"mod-{index}",
                "activation": "enabled",
                "destination": f"mods/mod-{index}.jar",
                "source": {
                    "provider": "local",
                    "location": str(source),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            }
        )
    layer = layer_factory(
        "client",
        operations=operations,
        payload={f"config/file-{index}.txt": bytes([index + 10]) * 4096 for index in range(5)},
    )
    service = ProjectService(tmp_path)
    service.link("client", "dev", tmp_path.parent / f"{tmp_path.name}-game")
    service.bootstrap("dev", include_delta=False)
    return service, layer


def test_warm_toggle_and_ignore_do_not_read_or_hash_unchanged_content(
    layer_factory, tmp_path: Path, monkeypatch
) -> None:
    service, layer = fixture(layer_factory, tmp_path)
    before = service.instance_context("dev")
    original_read = Path.read_bytes
    original_sha = hashlib.sha256
    original_obtain = ArtifactCache.obtain
    reads: list[Path] = []
    hashed: list[bytes] = []
    obtained: list[str] = []

    def read(path):
        reads.append(path)
        return original_read(path)

    def sha(content=b"", *args, **kwargs):
        if content in before.files.values():
            hashed.append(content)
        return original_sha(content, *args, **kwargs)

    def obtain(cache, package, provider):
        obtained.append(package.identity)
        return original_obtain(cache, package, provider)

    def no_prepare(*_args, **_kwargs):
        pytest.fail("warm mutation performed a full prepare")

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(hashlib, "sha256", sha)
    monkeypatch.setattr(ArtifactCache, "obtain", obtain)
    monkeypatch.setattr(Builder, "prepare", no_prepare)
    service.set_activation("dev", "mod-0", "disabled", "client")
    service.bootstrap("dev", include_delta=False)
    assert not obtained
    assert not hashed
    assert not any(
        path.is_relative_to(layer / "config") or "sha256" in path.parts for path in reads
    )

    service.set_activation("dev", "mod-0", "enabled", "client")
    assert obtained == ["mod-0"]
    assert set(hashed) == {before.files["mods/mod-0.jar"]}
    obtained.clear()
    hashed.clear()
    reads.clear()
    prepared = service._contexts["dev"].preparer.current
    service.add_ignore("dev", "config/file-0.txt", "exact", "client")
    service.bootstrap("dev", include_delta=False)
    current = service._contexts["dev"].preparer.current
    assert current.files is prepared.files
    assert current.resolution.manifest is prepared.resolution.manifest
    identity = next(
        rule["identity"] for rule in service.ignore_rules("dev", "client") if rule["own"]
    )
    service.remove_ignore("dev", identity, "client")
    service.bootstrap("dev", include_delta=False)
    assert not obtained
    assert not hashed
    assert not any(
        path.is_relative_to(layer / "config") or "sha256" in path.parts for path in reads
    )


def test_payload_external_changes_and_metadata_only_reuse(
    layer_factory, tmp_path: Path, monkeypatch
) -> None:
    service, layer = fixture(layer_factory, tmp_path)
    original_read = Path.read_bytes
    reads = []

    def read(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    service.add_ignore("dev", "config/file-0.txt", "exact", "client")
    assert not any(path.is_relative_to(layer / "config") for path in reads)
    path = layer / "config/file-0.txt"
    before_stat = path.stat()
    before_generation = generation(path)
    path.write_bytes(b"z" * before_stat.st_size)
    os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    assert generation(path) != before_generation
    assert service.instance_context("dev").files["config/file-0.txt"] == b"z" * before_stat.st_size
    assert [item for item in reads if item.is_relative_to(layer / "config")] == [path]
    added = layer / "config/new.txt"
    added.write_bytes(b"new")
    path.unlink()
    assert "config/file-0.txt" not in service.instance_context("dev").files
    assert service.instance_context("dev").files["config/new.txt"] == b"new"


@pytest.mark.parametrize("damage", ["corrupt", "delete"])
def test_artifact_cache_damage_matches_cold_preparation(layer_factory, tmp_path: Path, damage: str):
    service, _ = fixture(layer_factory, tmp_path)
    before = service.instance_context("dev")
    artifact = before.resolution.artifacts[0]
    cache = Path(artifact.cache_path)
    if damage == "corrupt":
        saved = cache.stat()
        cache.write_bytes(b"z" * artifact.size)
        os.utime(cache, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    else:
        cache.unlink()
    assert service.instance_context("dev") == ProjectService(tmp_path).instance_context("dev")
    assert cache.read_bytes() == before.files[artifact.declaration.destination]


def test_external_parent_metadata_and_payload_conflict_invalidate(layer_factory, tmp_path: Path):
    parent = layer_factory("external", payload={"settings.txt": "old"})
    layer = layer_factory("client", parents=[str(parent)])
    service = ProjectService(tmp_path)
    service.link("client", "dev", tmp_path.parent / f"{tmp_path.name}-game")
    assert service.instance_context("dev").files["settings.txt"] == b"old"
    (parent / "settings.txt").write_bytes(b"new")
    assert service.instance_context("dev").files["settings.txt"] == b"new"
    (layer / ".pack/new.json").write_text(
        json.dumps(
            {
                "operations": [
                    {"type": "put", "path": "settings.txt", "content": "override"},
                ]
            }
        )
    )
    assert service.instance_context("dev").files["settings.txt"] == b"override"


def test_reused_artifact_colliding_with_new_payload_still_fails(layer_factory, tmp_path: Path):
    service, layer = fixture(layer_factory, tmp_path)
    (layer / "mods").mkdir()
    (layer / "mods/mod-0.jar").write_bytes(b"collision")
    with pytest.raises(MergeConflict, match="package destination conflicts"):
        service.instance_context("dev")
    (layer / "mods/mod-0.jar").unlink()
    assert service.instance_context("dev") == ProjectService(tmp_path).instance_context("dev")


def test_new_payload_symlink_is_not_hidden_by_cache(layer_factory, tmp_path: Path):
    service, layer = fixture(layer_factory, tmp_path)
    try:
        (layer / "config/link.txt").symlink_to(layer / "config/file-0.txt")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError, match="payload symlinks"):
        service.instance_context("dev")


def test_fingerprint_reuses_only_matching_content_not_stale_manifest(layer_factory, tmp_path: Path):
    service, _ = fixture(layer_factory, tmp_path)
    context = service.instance_context("dev")
    assert service._context_fingerprint(context) == service._context_fingerprint(
        replace(context, content_digests={})
    )
    files = dict(context.files)
    files["config/file-0.txt"] = b"new auto-payload bytes"
    patched = replace(context, files=files)
    assert service._context_fingerprint(patched) != service._context_fingerprint(context)
    assert service._context_fingerprint(patched) == service._context_fingerprint(
        replace(patched, content_digests={})
    )


def test_failed_external_prepare_keeps_failing_until_repaired(layer_factory, tmp_path: Path):
    service, layer = fixture(layer_factory, tmp_path)
    metadata = layer / ".pack/operations.json"
    original = metadata.read_text()
    document = json.loads(original)
    document["operations"][0]["source"]["sha256"] = "f" * 64
    metadata.write_text(json.dumps(document))
    for _ in range(2):
        with pytest.raises(ArtifactError, match="hash mismatch"):
            service.instance_context("dev")
    metadata.write_text(original)
    assert service.instance_context("dev") == ProjectService(tmp_path).instance_context("dev")


def test_logical_only_refresh_cannot_validate_old_prepared_bytes(layer_factory, tmp_path: Path):
    service, layer = fixture(layer_factory, tmp_path)
    (layer / "config/file-0.txt").write_bytes(b"new bytes")
    logical = service.instance_context("dev", prepare=False)
    assert logical.files == {}
    assert logical.resolution.state.files["config/file-0.txt"] == b"new bytes"
    assert service.instance_context("dev").files["config/file-0.txt"] == b"new bytes"


def test_put_diagnostics_reuse_overwritten_content_after_metadata_edit(
    layer_factory, tmp_path: Path, monkeypatch
):
    original_content = "overwritten payload " * 4096
    layer_factory(
        "base", operations=[{"type": "put", "path": "settings.txt", "content": original_content}]
    )
    layer = layer_factory(
        "client",
        parents=["../base"],
        operations=[{"type": "put", "path": "settings.txt", "content": "final"}],
    )
    service = ProjectService(tmp_path)
    service.link("client", "dev", tmp_path.parent / f"{tmp_path.name}-game")
    service.bootstrap("dev", include_delta=False)
    original_sha = hashlib.sha256

    def sha(content=b"", *args, **kwargs):
        assert content not in (b"final", original_content.encode())
        return original_sha(content, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", sha)
    metadata = layer / ".pack/operations.json"
    document = json.loads(metadata.read_text())
    document["operations"].append({"type": "ignore", "glob": "logs/**"})
    metadata.write_text(json.dumps(document))
    service.bootstrap("dev", include_delta=False)
    service._context_fingerprint(service.instance_context("dev"))


@pytest.mark.parametrize(
    "operation",
    [
        {"type": "dependency", "source": "mod-0", "target": "absent"},
        {
            "type": "package",
            "id": "ambiguous-a",
            "logical_id": "library",
            "destination": "mods/a.jar",
        },
    ],
)
def test_failed_dependency_changes_match_cold_and_never_return_old_context(
    layer_factory, tmp_path: Path, operation: dict
):
    service, layer = fixture(layer_factory, tmp_path)
    metadata = layer / ".pack/operations.json"
    document = json.loads(metadata.read_text())
    if operation["type"] == "package":
        declaration = dict(document["operations"][0])
        declaration.update(operation)
        document["operations"].append(declaration)
        other = dict(declaration, id="ambiguous-b", destination="mods/b.jar")
        document["operations"].append(other)
        document["operations"][0]["automatic_requires"] = ["library"]
    else:
        document["operations"].append(operation)
    metadata.write_text(json.dumps(document))
    with pytest.raises(ResolutionError) as cold:
        ProjectService(tmp_path).instance_context("dev")
    for _ in range(2):
        with pytest.raises(ResolutionError) as warm:
            service.instance_context("dev")
        assert str(warm.value) == str(cold.value)


def test_feature_graph_changes_prune_obsolete_cache_and_atomic_payload_replace(
    layer_factory, tmp_path: Path
):
    service, layer = fixture(layer_factory, tmp_path)
    feature = layer_factory("feature", payload={"feature.txt": "feature"})
    service.add_feature("dev", "feature")
    assert service.instance_context("dev").files["feature.txt"] == b"feature"
    service.remove_feature("dev", "feature")
    current = service.instance_context("dev")
    assert "feature.txt" not in current.files
    assert feature not in service._contexts["dev"].loader._cache
    path = layer / "config/file-0.txt"
    replacement = layer / "config/replacement.txt"
    replacement.write_bytes(b"replacement")
    replacement.replace(path)
    assert service.instance_context("dev").files["config/file-0.txt"] == b"replacement"
    assert not any(
        content == b"feature"
        for content, _ in service._contexts["dev"].preparer.current.digests.values()
    )


def test_metadata_symlink_target_edit_invalidates_cached_definition(layer_factory, tmp_path: Path):
    service, layer = fixture(layer_factory, tmp_path)
    target = tmp_path / "metadata-target.json"
    target.write_text(json.dumps({"operations": [{"type": "ignore", "glob": "old/**"}]}))
    try:
        (layer / ".pack/linked.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert service.instance_context("dev").resolution.state.ignore[-1].value == "old/**"
    stamp = target.stat()
    target.write_text(json.dumps({"operations": [{"type": "ignore", "glob": "new/**"}]}))
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert service.instance_context("dev").resolution.state.ignore[-1].value == "new/**"


def test_artifact_change_during_materialization_cannot_publish_stale_cache(
    layer_factory, tmp_path: Path, monkeypatch
):
    service, _ = fixture(layer_factory, tmp_path)
    before = service.instance_context("dev")
    cache = Path(before.resolution.artifacts[0].cache_path)
    service.set_activation("dev", "mod-0", "disabled", "client")
    original_read = Path.read_bytes
    read_count = 0

    def read(path):
        nonlocal read_count
        content = original_read(path)
        if path == cache:
            read_count += 1
            if read_count == 2:
                path.write_bytes(b"corrupt" * 10)
        return content

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", read)
        with pytest.raises(ArtifactError, match="artifact changed"):
            service.set_activation("dev", "mod-0", "enabled", "client")
    # The failing activation rolled back. The next valid attempt repairs the hash cache.
    assert "mods/mod-0.jar" not in service.instance_context("dev").files
    service.set_activation("dev", "mod-0", "enabled", "client")
    assert service.instance_context("dev") == ProjectService(tmp_path).instance_context("dev")


def test_registry_change_during_read_cannot_bless_old_link(
    layer_factory, tmp_path: Path, monkeypatch
):
    service, _ = fixture(layer_factory, tmp_path)
    layer_factory("other", payload={"other.txt": "other"})
    read_links = service.state.links
    changed = False

    def links():
        nonlocal changed
        result = read_links()
        if not changed:
            changed = True
            service.state.write_links({"dev": LinkRecord("dev", "other", "other")})
        return result

    monkeypatch.setattr(service.state, "links", links)
    service.instance_context("dev")
    assert service.instance_context("dev").files == {"other.txt": b"other"}


def test_uncacheable_generation_falls_back_to_verified_reads(
    layer_factory, tmp_path: Path, monkeypatch
):
    service, _ = fixture(layer_factory, tmp_path)
    before = service.instance_context("dev")
    counter = 0

    def unavailable(_path, _fallback):
        nonlocal counter
        counter -= 1
        return counter

    monkeypatch.setattr(generations, "_change_time", unavailable)
    # A filesystem unable to provide ChangeTime remains usable; none of these
    # generations may validate the prior in-memory bytes.
    assert service.instance_context("dev") == before
    assert service.instance_context("dev") == before
