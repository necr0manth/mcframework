from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from mcframework.artifacts import (
    ArtifactCache,
    ArtifactRegistry,
    ArtifactResolver,
    PackageSelection,
)
from mcframework.curseforge_provider import CurseForgePackageProvider
from mcframework.errors import ArtifactError, ResolutionError
from mcframework.mcpack import McpackParser
from mcframework.model import Activation, ArtifactSource, PackageDeclaration, PackState, Resolution
from mcframework.modrinth_provider import ModrinthPackageProvider
from mcframework.operations import operation_from_json
from mcframework.update import UpdateService


def package(provider: str, location: str, *, version: str | None = None) -> PackageDeclaration:
    return PackageDeclaration(
        identity="library",
        logical_identity="library",
        kind="mod",
        source=ArtifactSource(provider, location, ""),
        destination="",
        activation=Activation.ENABLED,
        version=version,
        minecraft="26.3",
        loader="neoforge",
    )


def test_operation_accepts_unresolved_provider_selector_and_computes_stable_id() -> None:
    operation = operation_from_json(
        {
            "type": "package",
            "source": {"provider": "modrinth", "location": "sodium", "sha256": ""},
            "activation": "enabled",
        },
        source="pack.mcpack",
        index=1,
    )
    declaration = operation.package
    assert declaration.identity == "anon:modrinth:sodium"
    assert declaration.version is None
    assert declaration.source.sha256 == ""
    assert declaration.destination == ""


def test_direct_url_still_requires_an_explicit_digest() -> None:
    with pytest.raises(ResolutionError, match="explicit sha256"):
        operation_from_json(
            {
                "type": "package",
                "id": "custom",
                "source": {"provider": "direct_url", "location": "https://example/x.jar"},
                "destination": "mods/x.jar",
            },
            source="pack.mcpack",
            index=1,
        )


def test_modrinth_selects_latest_compatible_prerelease_and_caches_exact_bytes(
    monkeypatch, tmp_path
) -> None:
    from mcframework import modrinth_provider

    content = b"modrinth artifact"
    versions = [
        {
            "id": "stable-id",
            "project_id": "canonical",
            "version_number": "stable",
            "date_published": "2026-01-01T00:00:00Z",
            "game_versions": ["26.3"],
            "loaders": ["neoforge"],
            "files": [{"filename": "stable.jar", "url": "https://cdn/stable", "primary": True}],
        },
        {
            "id": "alpha-id",
            "project_id": "canonical",
            "version_number": "alpha.2",
            "date_published": "2026-02-01T00:00:00Z",
            "game_versions": ["26.3"],
            "loaders": ["neoforge"],
            "dependencies": [{"project_id": "required-mod", "dependency_type": "required"}],
            "files": [
                {
                    "filename": "alpha.jar",
                    "url": "https://cdn/alpha",
                    "primary": True,
                    "hashes": {"sha256": hashlib.sha256(content).hexdigest()},
                }
            ],
        },
        {
            "id": "wrong-game",
            "project_id": "canonical",
            "version_number": "newer-but-incompatible",
            "date_published": "2027-01-01T00:00:00Z",
            "game_versions": ["26.4"],
            "loaders": ["neoforge"],
            "files": [{"filename": "wrong.jar", "url": "https://cdn/wrong", "primary": True}],
        },
    ]
    monkeypatch.setattr(modrinth_provider, "read_json", lambda *args, **kwargs: versions)
    monkeypatch.setattr(modrinth_provider, "read_bytes", lambda url, **kwargs: content)
    registry = ArtifactRegistry()
    registry.register("modrinth", ModrinthPackageProvider())
    resolution = Resolution(
        PackState(packages={"library": package("modrinth", "sodium")}),
        (package("modrinth", "sodium"),),
        (),
    )

    result = ArtifactResolver(registry, ArtifactCache(tmp_path / "cache")).resolve(resolution)

    assert result.active_packages[0].version == "alpha.2"
    assert result.active_packages[0].destination == "mods/alpha.jar"
    assert result.active_packages[0].logical_identity == "canonical"
    assert result.active_packages[0].automatic_requires == ("required-mod",)
    assert result.artifacts[0].sha256 == hashlib.sha256(content).hexdigest()
    assert result.state.packages["library"].source.sha256 == result.artifacts[0].sha256


def test_empty_mcpack_selector_is_preserved_and_resolves_as_unpinned(monkeypatch) -> None:
    from mcframework import modrinth_provider

    record = McpackParser().parse(
        'sodium = *modrinth("sodium", version="")\n'
    ).lines[0].record
    declaration = replace(
        operation_from_json(record, source="packages.mcpack", index=1).package,
        minecraft="26.3",
        loader="neoforge",
    )
    assert declaration.version == ""

    monkeypatch.setattr(
        modrinth_provider,
        "read_json",
        lambda *args, **kwargs: [
            {
                "version_number": "latest-compatible",
                "game_versions": ["26.3"],
                "loaders": ["neoforge"],
                "files": [
                    {"filename": "sodium.jar", "url": "https://cdn/sodium", "primary": True}
                ],
            }
        ],
    )

    assert ModrinthPackageProvider().select(declaration).selector == "latest-compatible"


def test_seeded_exact_nonprimary_match_resolves_cached_bytes_without_network(
    monkeypatch, tmp_path
) -> None:
    from mcframework import modrinth_provider

    content = b"confirmed secondary file bytes"
    digest = hashlib.sha256(content).hexdigest()
    declaration = package("modrinth", "magic-slug", version="1.2.3")
    selection = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/secondary.jar",
        "secondary.jar",
        digest,
    )
    cache = ArtifactCache(tmp_path / "cache")
    seed = cache.seed_exact_match(declaration, selection, content)
    registry = ArtifactRegistry()
    registry.register("modrinth", ModrinthPackageProvider())
    monkeypatch.setattr(
        modrinth_provider,
        "read_json",
        lambda *args, **kwargs: pytest.fail("seeded selection must avoid provider lookup"),
    )
    monkeypatch.setattr(
        modrinth_provider,
        "read_bytes",
        lambda *args, **kwargs: pytest.fail("content-addressed bytes should already be cached"),
    )

    artifact = ArtifactResolver(registry, cache).resolve_packages((declaration,))[0]

    assert Path(artifact.cache_path).read_bytes() == content
    assert artifact.sha256 == digest
    assert cache.load_selection(declaration) == selection
    assert seed.selection_path.is_file()


def test_exact_match_cache_seed_rejects_conflicting_file_selection(tmp_path) -> None:
    first_bytes = b"first exact file"
    second_bytes = b"second exact file"
    declaration = package("modrinth", "magic-slug", version="1.2.3")
    first = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/first.jar",
        "first.jar",
        hashlib.sha256(first_bytes).hexdigest(),
    )
    second = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/second.jar",
        "second.jar",
        hashlib.sha256(second_bytes).hexdigest(),
    )
    cache = ArtifactCache(tmp_path / "cache")
    cache.seed_exact_match(declaration, first, first_bytes)
    with pytest.raises(ArtifactError, match="conflicting exact provider selection"):
        cache.seed_exact_match(declaration, second, second_bytes)

    assert cache.load_selection(declaration) == first


def test_exact_match_cache_seed_preserves_richer_same_file_selection_metadata(tmp_path) -> None:
    content = b"same exact file"
    digest = hashlib.sha256(content).hexdigest()
    declaration = package("modrinth", "magic-slug", version="1.2.3")
    richer = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/same.jar",
        "same.jar",
        digest,
        "canonical-project",
        ("required-project",),
    )
    sparse = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/same.jar",
        "same.jar",
        digest,
    )
    cache = ArtifactCache(tmp_path / "cache")
    cache.seed_exact_match(declaration, richer, content)

    cache.seed_exact_match(declaration, sparse, content)

    assert cache.load_selection(declaration) == richer


def test_exact_match_cache_seed_can_restore_a_new_selection_record(tmp_path) -> None:
    content = b"confirmed exact file"
    declaration = package("modrinth", "magic-slug", version="1.2.3")
    selection = PackageSelection(
        "1.2.3",
        "https://cdn.example.invalid/confirmed.jar",
        "confirmed.jar",
        hashlib.sha256(content).hexdigest(),
    )
    cache = ArtifactCache(tmp_path / "cache")
    seed = cache.seed_exact_match(declaration, selection, content)

    cache.restore_exact_match_seed(seed)

    assert cache.load_selection(declaration) is None
    assert seed.selection_path.is_file() is False


def test_modrinth_explicit_version_must_be_compatible_and_unique(monkeypatch) -> None:
    from mcframework import modrinth_provider

    monkeypatch.setattr(
        modrinth_provider,
        "read_json",
        lambda *args, **kwargs: [
            {
                "version_number": "chosen",
                "game_versions": ["26.3"],
                "loaders": ["fabric"],
                "files": [{"filename": "mod.jar", "url": "https://cdn/mod", "primary": True}],
            }
        ],
    )
    with pytest.raises(ArtifactError, match="not uniquely compatible"):
        ModrinthPackageProvider().select(package("modrinth", "project", version="chosen"))


def test_pinned_provider_selection_persists_and_recovers_stale_url(monkeypatch, tmp_path) -> None:
    from mcframework import modrinth_provider

    content = b"locked provider bytes"
    version = {
        "id": "immutable-id",
        "project_id": "canonical",
        "version_number": "alpha.3",
        "date_published": "2026-02-01T00:00:00Z",
        "game_versions": ["26.3"],
        "loaders": ["neoforge"],
        "files": [
            {
                "filename": "locked.jar",
                "url": "https://cdn/old",
                "primary": True,
                "hashes": {"sha256": hashlib.sha256(content).hexdigest()},
            }
        ],
    }
    metadata_calls = 0
    download_urls: list[str] = []
    old_url_stale = False

    def read_json(*_args, **_kwargs):
        nonlocal metadata_calls
        metadata_calls += 1
        return [version]

    def read_bytes(url, **_kwargs):
        download_urls.append(url)
        if old_url_stale and url == "https://cdn/old":
            raise OSError("stale CDN url")
        return content

    monkeypatch.setattr(modrinth_provider, "read_json", read_json)
    monkeypatch.setattr(modrinth_provider, "read_bytes", read_bytes)
    registry = ArtifactRegistry()
    registry.register("modrinth", ModrinthPackageProvider())
    cache = ArtifactCache(tmp_path / "cache")
    unresolved = package("modrinth", "sodium")
    first = ArtifactResolver(registry, cache).resolve(
        Resolution(PackState(packages={"library": unresolved}), (unresolved,), ())
    )
    assert first.active_packages[0].version == "alpha.3"
    assert list((tmp_path / "cache" / "selections").rglob("*.json"))

    old_url_stale = True
    metadata_calls = 0
    download_urls.clear()
    pinned = package("modrinth", "sodium", version="alpha.3")
    second = ArtifactResolver(registry, cache).resolve(
        Resolution(PackState(packages={"library": pinned}), (pinned,), ())
    )
    assert metadata_calls == 0
    assert download_urls == []
    assert second.artifacts[0].sha256 == first.artifacts[0].sha256

    Path(first.artifacts[0].cache_path).unlink()
    version["files"][0]["url"] = "https://cdn/refreshed"
    third = ArtifactResolver(registry, cache).resolve(
        Resolution(PackState(packages={"library": pinned}), (pinned,), ())
    )
    assert metadata_calls == 1
    assert download_urls == ["https://cdn/old", "https://cdn/refreshed"]
    assert third.artifacts[0].sha256 == first.artifacts[0].sha256


def test_provider_metadata_automatic_requires_activates_declared_package(
    monkeypatch, tmp_path
) -> None:
    from mcframework import modrinth_provider

    source_bytes = b"owner"
    requirement_bytes = b"required local artifact"
    versions = [
        {
            "id": "v1",
            "project_id": "owner-id",
            "version_number": "1.0",
            "date_published": "2026-01-01T00:00:00Z",
            "game_versions": ["26.3"],
            "loaders": ["neoforge"],
            "dependencies": [{"project_id": "api-id", "dependency_type": "required"}],
            "files": [
                {
                    "filename": "owner.jar",
                    "url": "https://cdn/owner",
                    "primary": True,
                    "hashes": {"sha256": hashlib.sha256(source_bytes).hexdigest()},
                }
            ],
        }
    ]
    monkeypatch.setattr(modrinth_provider, "read_json", lambda *args, **kwargs: versions)
    monkeypatch.setattr(modrinth_provider, "read_bytes", lambda url, **kwargs: source_bytes)

    from mcframework.artifacts import LocalArtifactProvider

    local_path = tmp_path / "required.jar"
    local_path.write_bytes(requirement_bytes)
    owner = package("modrinth", "owner")
    required = PackageDeclaration(
        identity="required",
        logical_identity="api-id",
        kind="mod",
        source=ArtifactSource("local", str(local_path), ""),
        destination="mods/required.jar",
        minecraft="26.3",
        loader="neoforge",
    )
    registry = ArtifactRegistry()
    registry.register("modrinth", ModrinthPackageProvider())
    registry.register("local", LocalArtifactProvider(tmp_path))
    resolution = Resolution(
        PackState(packages={"owner": owner, "required": required}), (owner,), ()
    )

    result = ArtifactResolver(registry, ArtifactCache(tmp_path / "cache")).resolve(resolution)

    assert {item.identity for item in result.active_packages} == {"library", "required"}
    assert {item.declaration.identity for item in result.artifacts} == {"library", "required"}


def test_curseforge_filename_remains_filename_but_automatic_choice_pins_file_id(
    monkeypatch,
) -> None:
    from mcframework import curseforge_provider

    monkeypatch.setattr(
        curseforge_provider,
        "read_json",
        lambda *args, **kwargs: {
            "data": [
                {
                    "id": 101,
                    "fileName": "mod-neoforge.jar",
                    "fileDate": "2026-01-01T00:00:00Z",
                    "gameVersions": ["26.3", "NeoForge"],
                    "downloadUrl": "https://cdn/101",
                },
                {
                    "id": 102,
                    "fileName": "mod-neoforge.jar",
                    "fileDate": "2026-02-01T00:00:00Z",
                    "gameVersions": ["26.3", "Forge"],
                    "downloadUrl": "https://cdn/102",
                },
            ],
            "pagination": {"totalCount": 2},
        },
    )
    provider = CurseForgePackageProvider("test-key")
    named = provider.select(package("curseforge", "42", version="mod-neoforge.jar"))
    automatic = provider.select(package("curseforge", "42"))
    assert named.selector == "mod-neoforge.jar"
    assert named.download_url == "https://cdn/101"
    assert automatic.selector == "101"


def test_curseforge_ambiguous_applicable_filename_fails(monkeypatch) -> None:
    from mcframework import curseforge_provider

    monkeypatch.setattr(
        curseforge_provider,
        "read_json",
        lambda *args, **kwargs: {
            "data": [
                {"id": 1, "fileName": "same.jar", "gameVersions": ["26.3", "NeoForge"]},
                {"id": 2, "fileName": "same.jar", "gameVersions": ["26.3", "NeoForge"]},
            ],
            "pagination": {"totalCount": 2},
        },
    )
    with pytest.raises(ArtifactError, match="not uniquely compatible"):
        CurseForgePackageProvider("key").select(package("curseforge", "42", version="same.jar"))


def test_explicit_update_rewrites_only_mcpack_selector_and_preserves_comments(tmp_path) -> None:
    from mcframework.artifacts import PackageUpdate

    content = (
        b"#mods\n\n# Keep this note.\n"
        b'sodium = modrinth("sodium", version="old", update="latest_compatible")  // pin me\n'
    )
    source = tmp_path / "mods.mcpack"
    source.write_bytes(content)
    line = McpackParser().parse(content, source).lines[-1]
    package_record = line.record
    assert package_record is not None
    package_record = {
        key: value
        for key, value in package_record.items()
        if not key.startswith("_") and key != "section"
    }
    declaration = operation_from_json(
        package_record, source=str(source), index=line.line_number
    ).package
    declaration = replace(declaration, source_file=str(source))
    new_bytes = b"updated mod bytes"
    new_digest = hashlib.sha256(new_bytes).hexdigest()

    class UpdatingProvider:
        def find_update(self, _package):
            return PackageUpdate(
                ArtifactSource("modrinth", "sodium", ""),
                "new-alpha",
                "https://cdn/new-alpha.jar",
                "new-alpha.jar",
            )

        def fetch(self, _package):
            return new_bytes

    registry = ArtifactRegistry()
    registry.register("modrinth", UpdatingProvider())

    result = UpdateService(registry, ArtifactCache(tmp_path / "cache")).update(declaration)

    assert result.changed
    updated = source.read_bytes()
    assert b"# Keep this note." in updated
    assert b"// pin me" in updated
    parsed = McpackParser().parse(updated, source)
    assert parsed.lines[-1].record["version"] == "new-alpha"
    assert new_digest == result.package.source.sha256
