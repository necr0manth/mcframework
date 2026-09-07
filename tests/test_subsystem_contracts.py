import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from mcframework import manifest, operations, resolver
from mcframework.artifacts import (
    ArtifactCache,
    ArtifactRegistry,
    ArtifactResolver,
    LocalArtifactProvider,
)
from mcframework.build import Builder
from mcframework.loader import WorkspaceLoader
from mcframework.model import (
    Activation,
    ArtifactSource,
    ContentState,
    Dependency,
    IgnoreSelector,
    PackageDeclaration,
    PackageState,
    PackState,
    Resolution,
    SelectorKind,
    frozen_map,
)
from mcframework.preparation import IncrementalPreparer


def test_dependency_adapter_uses_package_only_contract(monkeypatch) -> None:
    package = PackageDeclaration(
        "a", "a", "mod", ArtifactSource("local", "a.jar", "0" * 64), "mods/a.jar",
        activation=Activation.ENABLED,
    )
    dependency = replace(package, identity="b", destination="mods/b.jar",
                         activation=Activation.AVAILABLE)
    state = PackState(
        packages=frozen_map({"a": package, "b": dependency}),
        dependencies=(Dependency("a", "b"),),
        ignore=(IgnoreSelector(SelectorKind.GLOB, "logs/**"),),
    )
    original = resolver.resolve_package_dependencies
    calls = []

    def narrow(catalog, automatic_dependencies):
        assert isinstance(catalog, PackageState)
        assert not hasattr(catalog, "ignore")
        assert not hasattr(catalog, "files")
        calls.append(catalog)
        return original(catalog, automatic_dependencies)

    monkeypatch.setattr(resolver, "resolve_package_dependencies", narrow)
    resolved, active = resolver.resolve_dependencies(state, True)
    assert len(calls) == 1
    assert [item.identity for item in active] == ["a", "b"]
    assert resolved.packages["b"].activation is Activation.TRANSITIVE
    assert resolved.ignore is state.ignore


def test_resolver_dispatches_policy_before_domain_reduction(
    layer_factory, tmp_path: Path, monkeypatch,
) -> None:
    layer_factory("base", operations=[{"type": "ignore", "glob": "logs/**"}])
    layer_factory("child", parents=["../base"], operations=[
        {"type": "put", "path": "config/a", "content": "a"},
        {"type": "ignore", "glob": "screenshots/**"},
        {"type": "routing_rule", "id": "mods", "when": {"operation_types": ["package"]},
         "write": {"file": {"path": "mods/.pack/mods.json", "path_mode": "layer"}}},
    ])
    original = operations.apply_domain_operations
    calls = []

    def narrow(content, catalog, records, precedence):
        assert isinstance(content, ContentState)
        assert isinstance(catalog, PackageState)
        assert not hasattr(content, "ignore")
        assert not any(isinstance(item, (operations.AddIgnoreSelector, operations.RoutingRule))
                       for item in records)
        calls.append(records)
        return original(content, catalog, records, precedence)

    monkeypatch.setattr(operations, "apply_domain_operations", narrow)
    result = resolver.Resolver(WorkspaceLoader(tmp_path)).resolve("child")
    assert len(calls) == 2
    assert result.state.files == {"config/a": b"a"}
    assert [item.value for item in result.state.ignore] == ["logs/**", "screenshots/**"]


def test_builder_calls_narrow_materializer_and_policy_changes_reuse_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    builder = Builder(ArtifactResolver(ArtifactRegistry(), ArtifactCache(tmp_path / "cache")))
    original = manifest.materialize_content
    calls = []
    artifact_inputs = []
    original_artifacts = builder.artifacts.resolve_packages

    def narrow_artifacts(packages):
        assert isinstance(packages, tuple)
        artifact_inputs.append(packages)
        return original_artifacts(packages)

    def narrow(content, artifacts):
        assert isinstance(content, ContentState)
        assert not hasattr(content, "ignore")
        assert not hasattr(content, "package_catalog")
        calls.append(content)
        return original(content, artifacts)

    monkeypatch.setattr(manifest, "materialize_content", narrow)
    monkeypatch.setattr(builder.artifacts, "resolve_packages", narrow_artifacts)
    state = PackState(files=frozen_map({"config/a": b"a"}))
    preparer = IncrementalPreparer()
    before = preparer.prepare(Resolution(state, (), ()), builder)
    assert len(calls) == 1
    assert artifact_inputs == [()]

    def no_full_prepare(*args):
        pytest.fail("policy-only update must reuse prepared payload")

    monkeypatch.setattr(builder, "prepare", no_full_prepare)
    updated = replace(state, ignore=(IgnoreSelector(SelectorKind.GLOB, "logs/**"),))
    after = preparer.prepare(Resolution(updated, (), ()), builder)
    assert after.files is before.files
    assert after.resolution.manifest is before.resolution.manifest
    assert after.resolution.state.ignore == updated.ignore


def test_mixed_metadata_keeps_record_provenance_through_real_build(
    layer_factory, tmp_path: Path,
) -> None:
    artifact = b"test mod artifact"
    (tmp_path / "artifact.jar").write_bytes(artifact)
    layer_factory("pack", operations=[
        {"type": "routing_rule", "id": "mods", "when": {"operation_types": ["package"]},
         "write": {"file": {"path": "mods/.pack/mods.json", "path_mode": "layer"}}},
        {"type": "ignore", "glob": "logs/**"},
        {"type": "package", "id": "mod", "activation": "enabled",
         "destination": "mods/mod.jar", "source": {
             "provider": "local", "location": "artifact.jar",
             "sha256": hashlib.sha256(artifact).hexdigest(),
         }},
    ], payload={"config/a": "a"})
    result = resolver.Resolver(WorkspaceLoader(tmp_path)).resolve("pack")
    package = result.active_packages[0]
    source = Path(package.source_file).relative_to(tmp_path).as_posix()
    assert source == "pack/.pack/operations.json"
    assert package.source_index == 2
    producer = result.trace[0].producers[0]
    assert isinstance(producer.operations[0], operations.RoutingRule)
    assert isinstance(producer.operations[1], operations.AddIgnoreSelector)
    assert isinstance(producer.operations[2], operations.DeclarePackage)
    registry = ArtifactRegistry()
    registry.register("local", LocalArtifactProvider(tmp_path))
    builder = Builder(ArtifactResolver(registry, ArtifactCache(tmp_path / "cache")))
    prepared, files = builder.prepare(result)
    assert files == {"config/a": b"a", "mods/mod.jar": artifact}
    assert {item.path for item in prepared.manifest} == set(files)
    assert prepared.active_packages[0].source_index == 2
    assert prepared.state.ignore == (IgnoreSelector(SelectorKind.GLOB, "logs/**"),)
