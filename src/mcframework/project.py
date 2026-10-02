"""High-level project, instance, feature, diff, and bounded-edit services."""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import re
import stat
import tempfile
import threading
import urllib.parse
import zipfile
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial, wraps
from pathlib import Path, PurePosixPath
from typing import Any

from .artifacts import (
    ArtifactCache,
    ArtifactRegistry,
    ArtifactResolver,
    DirectUrlArtifactProvider,
    LocalArtifactProvider,
    PackageSelection,
)
from .build import Builder
from .curseforge_provider import CurseForgePackageProvider
from .errors import MetadataError, ResolutionError
from .explorer import (
    ExplorerSnapshot,
    SourceGenerations,
    build_snapshot,
    scan_generation_matches,
    scan_instance,
)
from .generations import Generation, generation
from .loader import LEGACY_MARKER_FILE, MARKER_FILE, METADATA_DIR, WorkspaceLoader
from .local_state import InstanceRecord, LinkRecord, LocalState, validate_name
from .mcpack import (
    McpackDocument,
    McpackParser,
    format_operation,
    format_section,
    pin_provider_selector,
)
from .metadata import (
    JsonMetadataParser,
    MetadataParser,
    decode_record_paths,
    encode_record_paths,
    normalize_metadata_destination,
)
from .metadata_write import MetadataWrite, MetadataWritePlan
from .model import Activation, IgnoreSelector, PackState, Resolution, SelectorKind, frozen_map
from .modrinth_provider import ModrinthPackageProvider
from .operations import (
    AddIgnoreSelector,
    RoutingEngine,
    RoutingRule,
    merge_routing_rules,
    operation_from_json,
    operation_to_json,
)
from .package_inspect import (
    CurseForgeFingerprintRunner,
    ProviderLookupResult,
    ProviderMatch,
    ProviderMatcher,
    confirmed_provider_match,
    default_provider_matcher,
    file_hashes,
    inspect_jar,
    normalize_package_kind,
    provider_lookup_error,
)
from .paths import atomic_write, confined_path, matches, normalize_relative_path
from .preparation import ContentDigests, IncrementalPreparer, content_digest
from .resolver import LayerComposer
from .sync import disabled_package_aliases
from .transport import LocalFilesystemInstance


@dataclass(frozen=True)
class InstanceContext:
    instance: InstanceRecord
    link: LinkRecord
    features: tuple[str, ...]
    parameters: dict[str, Any]
    resolution: Resolution
    files: dict[str, bytes]
    content_digests: ContentDigests = field(default_factory=dict, repr=False, compare=False)


@dataclass
class _ContextCache:
    loader: WorkspaceLoader
    preparer: IncrementalPreparer = field(default_factory=IncrementalPreparer)
    context: InstanceContext | None = None
    generations: tuple[tuple[Path, Generation], ...] = ()


@dataclass
class _ProviderLookup:
    status: str = "pending"
    matches: tuple[ProviderMatch, ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    future: Future[ProviderLookupResult] | None = None


@dataclass(frozen=True)
class _PackagePrefetch:
    root: Path
    path: str
    expected: tuple[str, int, int, int]
    declared: bool
    kind: str | None


_edit_journals: ContextVar[tuple[Any, ...]] = ContextVar("metadata_edit_journals", default=())
_edit_destinations: ContextVar[tuple[tuple[Any, str | None, str | None], ...]] = ContextVar(
    "metadata_edit_destinations", default=()
)
_edit_layer_locks: ContextVar[tuple[tuple[Any, Path], ...]] = ContextVar(
    "metadata_edit_layer_locks", default=()
)


def _metadata_edit(method: Any) -> Any:
    """Serialize and journal one metadata mutation against its physical target layer."""
    signature = inspect.signature(method)

    @wraps(method)
    def transactional(self: ProjectService, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(self, *args, **kwargs)
        instance = bound.arguments.get("instance")
        if not isinstance(instance, str):
            raise RuntimeError("metadata edits require an instance argument")
        target = self._provisional_target(instance, bound.arguments.get("target_layer"))
        with self._target_layer_edit_lock(target):
            if any(journal.service is self for journal in _edit_journals.get()):
                return method(self, *args, **kwargs)
            with _BeforeImageJournal(self, self.project) as journal:
                token = _edit_journals.set((*_edit_journals.get(), journal))
                try:
                    return method(self, *args, **kwargs)
                finally:
                    _edit_journals.reset(token)
    return transactional


class ProjectService:
    def __init__(
        self,
        project: Path | str | None = None,
        *,
        provider_matcher: ProviderMatcher | None = None,
        metadata_parser: MetadataParser | None = None,
    ):
        self.project = Path(project or Path.cwd()).resolve()
        self.state = LocalState(self.project)
        if provider_matcher is None:
            fingerprint_runner = CurseForgeFingerprintRunner()
            self._fingerprint_runner: CurseForgeFingerprintRunner | None = fingerprint_runner
            self.provider_matcher = default_provider_matcher(
                fingerprinter=fingerprint_runner
            )
        else:
            self._fingerprint_runner = None
            self.provider_matcher = provider_matcher
        self.metadata: MetadataParser = metadata_parser or JsonMetadataParser()
        self._provider_matches: dict[str, _ProviderLookup] = {}
        self._provider_prefetches: set[_PackagePrefetch] = set()
        self._provider_lock = threading.RLock()
        self._provider_executor = ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="mcpack-provider"
        )
        self._provider_prefetch_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mcpack-package-prefetch"
        )
        self._explorer_snapshots: dict[str, ExplorerSnapshot] = {}
        self._context_lock = threading.RLock()
        self._contexts: dict[str, _ContextCache] = {}
        self._close_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._close_lock:
            with self._provider_lock:
                if self._closed:
                    return
                self._closed = True
            self._provider_prefetch_executor.shutdown(wait=True, cancel_futures=True)
            self._provider_executor.shutdown(wait=True, cancel_futures=True)
            if self._fingerprint_runner is not None:
                self._fingerprint_runner.close()

    def _loader(self, *, refresh: bool = False) -> WorkspaceLoader:
        return WorkspaceLoader(self.project, refresh=refresh, parser=self.metadata)

    def init_layer(self, layer: str) -> Path:
        validate_name(layer, "layer name")
        root = self.project / layer
        if root.exists():
            if not root.is_dir():
                raise MetadataError(f"layer path is not a directory: {root}")
            if any(root.iterdir()):
                raise MetadataError(f"layer directory is not empty: {layer!r}")
        marker = root / METADATA_DIR / MARKER_FILE
        atomic_write(marker, f'layer({json.dumps(layer, ensure_ascii=False)})\n'.encode())
        return root

    def branch_root(self, branch: str) -> Path:
        validate_name(branch, "branch name")
        root = (self.project / branch).resolve()
        if root.parent != self.project or self._layer_marker_path(root) is None:
            raise MetadataError(f"branch does not exist at project root: {branch!r}")
        return root

    def branches(self) -> tuple[str, ...]:
        return tuple(
            child.name
            for child in sorted(self.project.iterdir(), key=lambda item: item.name)
            if child.is_dir()
            and child.name not in {"build", ".mcpack"}
            and self._layer_marker_path(child) is not None
        )

    def artifact_components(self) -> tuple[ArtifactRegistry, ArtifactCache, Builder]:
        registry = ArtifactRegistry()
        registry.register("local", LocalArtifactProvider(self.project))
        registry.register("direct_url", DirectUrlArtifactProvider())
        registry.register("modrinth", ModrinthPackageProvider())
        registry.register("curseforge", CurseForgePackageProvider())
        cache = ArtifactCache(self.state.root / "cache")
        return registry, cache, Builder(ArtifactResolver(registry, cache))

    def resolve_branch(
        self,
        branch: str,
        parameters: dict[str, Any] | None = None,
        *,
        automatic_dependencies: bool = True,
    ) -> Resolution:
        layer = self.branch_root(branch)
        registry, _, _ = self.artifact_components()
        loader = self._loader(refresh=True)
        for _attempt in range(2):
            resolution = LayerComposer(
                loader, automatic_dependencies=automatic_dependencies
            ).resolve(layer, parameters)
            if not self._pin_provider_selectors(resolution, registry):
                return resolution
            loader = self._loader(refresh=True)
        raise MetadataError("package selectors changed repeatedly during resolution")

    def prepare_branch(
        self,
        branch: str,
        parameters: dict[str, Any] | None = None,
        *,
        automatic_dependencies: bool = True,
    ) -> Resolution:
        """Prepare a branch and pin every provider selector in its active closure."""
        layer = self.branch_root(branch)
        registry, _, builder = self.artifact_components()
        loader = self._loader(refresh=True)
        attempt = 0
        limit: int | None = None
        while True:
            resolution = LayerComposer(
                loader, automatic_dependencies=automatic_dependencies
            ).resolve(layer, parameters)
            if limit is None:
                limit = len(resolution.state.packages) + 1
                if limit == 1:
                    limit = 2
            prepared, _files = builder.prepare(resolution)
            if not self._pin_provider_selectors(
                prepared, registry, source_resolution=resolution
            ):
                return prepared
            attempt += 1
            if attempt >= limit:
                raise MetadataError("package selector pinning did not converge")
            loader = self._loader(refresh=True)

    def _pin_provider_selectors(
        self,
        resolution: Resolution,
        registry: ArtifactRegistry,
        *,
        source_resolution: Resolution | None = None,
        include_unselected: bool = False,
    ) -> bool:
        candidates = []
        packages = (
            resolution.state.packages.values()
            if include_unselected
            else resolution.active_packages
        )
        for package in packages:
            source_package = (
                source_resolution.state.packages.get(package.identity, package)
                if source_resolution is not None
                else package
            )
            if (
                package.source.provider not in {"modrinth", "curseforge"}
                or source_package.version
                or source_package.source_file is None
                or not source_package.source_file.casefold().endswith(".mcpack")
            ):
                continue
            if source_resolution is None:
                provider = registry.provider(source_package.source.provider)
                select = getattr(provider, "select", None)
                if not callable(select):
                    raise MetadataError(
                        "provider "
                        f"{source_package.source.provider!r} cannot select a package version"
                    )
                selector = select(source_package).selector
            else:
                selector = package.version
            if not isinstance(selector, str) or not selector:
                raise MetadataError(
                    f"provider {package.source.provider!r} returned an empty selector "
                    f"for {package.identity!r}"
                )
            candidates.append((source_package, selector))
        if not candidates:
            return False

        parser = McpackParser()
        originals: dict[Path, bytes] = {}
        edited: dict[Path, bytes] = {}
        layers: set[Path] = set()
        trace_roots = tuple(Path(trace.root).resolve() for trace in resolution.trace)
        for package, selector in candidates:
            if package.source_file is None:
                raise MetadataError(f"package {package.identity!r} has no source file")
            path = Path(package.source_file).resolve()
            if path not in originals:
                originals[path] = path.read_bytes()
                edited[path] = originals[path]
            document = parser.parse(edited[path], path)
            if package.source_index is None:
                raise MetadataError(f"package {package.identity!r} has no source line")
            line = next(
                (item for item in document.lines if item.line_number == package.source_index),
                None,
            )
            record = line.record if line is not None else None
            source = record.get("source") if isinstance(record, dict) else None
            if (
                line is None
                or not isinstance(record, dict)
                or record.get("type") != "package"
                or record.get("id") != package.identity
                or not isinstance(source, dict)
                or source.get("provider") != package.source.provider
                or source.get("location") != package.source.location
                or record.get("version") != package.version
            ):
                raise MetadataError(
                    f"package declaration moved or changed before pinning {package.identity!r}"
                )
            replacement = pin_provider_selector(line.expression, selector)
            edited[path] = document.replace_expression(
                line.line_id, line.expression, replacement
            )
            owner = next(
                (root for root in trace_roots if path.is_relative_to(root)),
                path.parent,
            )
            layers.add(owner)

        with ExitStack() as locks:
            for layer in sorted(layers, key=lambda item: str(item)):
                locks.enter_context(self._target_layer_edit_lock(layer))
            paths = list(originals)
            with _BeforeImageJournal(self, self.project, paths):
                for path in paths:
                    if path.read_bytes() != originals[path]:
                        raise MetadataError(
                            f"package source changed while pinning selectors: {path}"
                        )
                for path in paths:
                    atomic_write(path, edited[path])
        return True

    def create_instance(self, name: str, path: Path | str) -> InstanceRecord:
        validate_name(name, "instance name")
        root = Path(path).resolve()
        self._validate_instance_path(root)
        if root.exists() and not root.is_dir():
            raise MetadataError(f"instance path is not a directory: {root}")
        with self.state.lock():
            instances = self.state.instances()
            if name in instances:
                raise MetadataError(f"instance already exists: {name!r}")
            root.mkdir(parents=True, exist_ok=True)
            instances[name] = InstanceRecord(name, root)
            self.state.write_instances(instances)
            self.state.write_parameters(name)
        return instances[name]

    def link(self, branch: str, name: str, path: Path | str | None = None) -> LinkRecord:
        self.branch_root(branch)
        validate_name(name, "instance name")
        requested = Path(path).resolve() if path is not None else None
        if requested is not None:
            self._validate_instance_path(requested)
        with self.state.lock():
            instances = self.state.instances()
            existing = instances.get(name)
            if existing is None:
                if requested is None:
                    raise MetadataError(
                        f"instance {name!r} is not registered; pass -p with its .minecraft path"
                    )
                requested.mkdir(parents=True, exist_ok=True)
                existing = InstanceRecord(name, requested)
                instances[name] = existing
                self.state.write_instances(instances)
                self.state.write_parameters(name)
            elif requested is not None and requested != existing.path:
                raise MetadataError(
                    f"instance {name!r} already uses {existing.path}; "
                    f"refusing different -p {requested}"
                )
            links = self.state.links()
            previous = links.get(name)
            if previous is not None and previous.branch != branch:
                raise MetadataError(
                    f"instance {name!r} is linked to {previous.branch!r}; unlink it first"
                )
            effective = previous.effective_layer if previous else branch
            links[name] = LinkRecord(name, branch, effective)
            self.state.write_links(links)
        return links[name]

    def unlink(self, name: str) -> None:
        self._require_web_stopped(name)
        with self.state.lock():
            links = self.state.links()
            if name not in links:
                raise MetadataError(f"instance is not linked: {name!r}")
            del links[name]
            self.state.write_links(links)

    def rename_instance(self, old: str, new: str) -> None:
        validate_name(new, "instance name")
        self._require_web_stopped(old)
        with self.state.lock():
            instances = self.state.instances()
            if old not in instances:
                raise MetadataError(f"instance does not exist: {old!r}")
            if new in instances:
                raise MetadataError(f"instance already exists: {new!r}")
            old_record = instances.pop(old)
            instances[new] = InstanceRecord(new, old_record.path)
            links = self.state.links()
            moves = []
            metadata_updates: dict[Path, bytes] = {}
            old_overlay = self.state.feature_root(old)
            new_overlay = self.state.feature_root(new)
            before_paths = [self.state.instances_path, self.state.links_path]
            if old in links:
                old_link = links.pop(old)
                effective = old_link.effective_layer
                if old_overlay.exists():
                    metadata_updates = self._renamed_overlay_sources(old_overlay, new_overlay)
                    before_paths.extend(metadata_updates)
                    before_paths.append(
                        self._layer_marker_path(old_overlay)
                        or old_overlay / METADATA_DIR / LEGACY_MARKER_FILE
                    )
                    moves.append((old_overlay, new_overlay))
                    effective = self._layer_reference(new_overlay)
                links[new] = LinkRecord(new, old_link.branch, effective)
            moves.extend(
                (source, destination)
                for source, destination in (
                    (self.state.parameters_path(old), self.state.parameters_path(new)),
                    (self.state.web_log_path(old), self.state.web_log_path(new)),
                    (self.state.routing_rules_path(old), self.state.routing_rules_path(new)),
                )
                if source.exists()
            )
            for _, destination in moves:
                if destination.exists() or destination.is_symlink():
                    raise MetadataError(
                        f"instance rename destination already exists: {destination}"
                    )
            with _BeforeImageJournal(self, self.state.root, before_paths):
                completed_moves = []
                try:
                    for source, destination in moves:
                        self._rename_optional(source, destination)
                        completed_moves.append((source, destination))
                        if source == old_overlay:
                            self._rewrite_overlay_identity(new, new_overlay)
                            for path, content in metadata_updates.items():
                                atomic_write(new_overlay / path.relative_to(old_overlay), content)
                    self.state.write_instances(instances)
                    self.state.write_links(links)
                except Exception:
                    # Restore directory names before replaying their metadata before-images.
                    for source, destination in reversed(completed_moves):
                        destination.rename(source)
                    raise

    def delete_instance(self, name: str) -> None:
        self._require_web_stopped(name)
        with self.state.lock():
            instances = self.state.instances()
            if name not in instances:
                raise MetadataError(f"instance does not exist: {name!r}")
            overlay = self.state.feature_root(name).resolve()
            conflicting = next(
                (item.path for item in instances.values() if _intersects(overlay, item.path)),
                None,
            )
            if conflicting is not None:
                raise MetadataError(
                    f"refusing to remove local layer {overlay}; it intersects game path "
                    f"{conflicting}"
                )
            del instances[name]
            links = self.state.links()
            links.pop(name, None)
            self.state.write_instances(instances)
            self.state.write_links(links)
            self.state.parameters_path(name).unlink(missing_ok=True)
            self.state.routing_rules_path(name).unlink(missing_ok=True)
            self._remove_local_tree(overlay)
            self.state.web_path(name).unlink(missing_ok=True)
            self.state.web_log_path(name).unlink(missing_ok=True)

    def add_feature(self, instance: str, feature: str) -> LinkRecord:
        self.branch_root(feature)
        context = self.instance_context(instance, prepare=False)
        if feature == context.link.branch:
            raise MetadataError("the base branch cannot also be a feature")
        features = list(context.features)
        if feature in features:
            return context.link
        features.append(feature)
        return self._change_features(instance, context, features)

    def remove_feature(self, instance: str, feature: str) -> LinkRecord:
        context = self.instance_context(instance, prepare=False)
        if feature not in context.features:
            raise MetadataError(f"feature is not enabled for {instance!r}: {feature!r}")
        features = [item for item in context.features if item != feature]
        return self._change_features(instance, context, features)

    def instance_context(self, name: str, *, prepare: bool = True) -> InstanceContext:
        # Cache publication is atomic across threaded HTTP readers. The public context
        # retains mutable files/parameters for compatibility without exposing cached maps.
        with self._context_lock:
            context = self._instance_context(name, prepare=prepare)
            resolution = replace(
                context.resolution,
                trace=tuple(
                    replace(trace, parameters=frozen_map(deepcopy(dict(trace.parameters))))
                    for trace in context.resolution.trace
                ),
            )
            if not prepare:
                resolution = replace(resolution, artifacts=(), manifest=())
            return replace(
                context,
                files=dict(context.files) if prepare else {},
                parameters=deepcopy(context.parameters),
                resolution=resolution,
            )

    def _instance_context(self, name: str, *, prepare: bool) -> InstanceContext:
        # Pair parsed registry data with generations observed before the read. If an
        # external writer races the read, the next lookup cannot bless the old data.
        source_paths = (
            self.state.instances_path,
            self.state.links_path,
            self.state.parameters_path(name),
        )
        sources = tuple((path, generation(path)) for path in source_paths)
        instances = self.state.instances()
        links = self.state.links()
        if name not in instances:
            raise MetadataError(f"instance does not exist: {name!r}")
        if name not in links:
            raise MetadataError(f"instance is not linked: {name!r}")
        self._contexts = {key: value for key, value in self._contexts.items() if key in instances}
        cached = self._contexts.setdefault(
            name, _ContextCache(self._loader(refresh=True))
        )
        current = cached.context
        prepared = cached.preparer.current
        if (
            current is not None
            and sources == cached.generations
            and cached.loader.fresh()
            and (
                not prepare
                or (
                    prepared is not None
                    and prepared.resolution is current.resolution
                    and prepared.fresh()
                )
            )
        ):
            return current
        # Parsing new source generations must not make an older successful context
        # appear fresh after a failed prepare or a logical-only refresh.
        cached.context = None
        link = links[name]
        self._validate_instance_path(instances[name].path)
        parameters, _ = self.state.parameters(name)
        layer = self._effective_root(name, link)
        features = self._overlay_features(name, link)
        cached.loader.begin()
        try:
            resolution = LayerComposer(cached.loader).resolve(layer, parameters)
        finally:
            cached.loader.finish()
        files: dict[str, bytes] = {}
        digests: ContentDigests = {}
        if prepare:
            _, _, builder = self.artifact_components()
            attempt = 0
            limit = len(resolution.state.packages) + 1
            while True:
                prepared = cached.preparer.prepare(resolution, builder)
                if not self._pin_provider_selectors(
                    prepared.resolution,
                    builder.artifacts.registry,
                    source_resolution=resolution,
                ):
                    resolution = prepared.resolution
                    break
                attempt += 1
                if attempt >= max(limit, 2):
                    raise MetadataError("package selector pinning did not converge")
                cached.preparer.current = None
                cached.loader = self._loader(refresh=True)
                cached.loader.begin()
                try:
                    resolution = LayerComposer(cached.loader).resolve(layer, parameters)
                finally:
                    cached.loader.finish()
            files = (
                current.files
                if current is not None
                and len(prepared.files) == len(current.files)
                and all(
                    current.files.get(path) is content for path, content in prepared.files.items()
                )
                else dict(prepared.files)
            )
            digests = prepared.digests
        context = InstanceContext(
            instances[name], link, features, parameters, resolution, files, digests
        )
        # A logical-only check must not evict successfully prepared bytes.
        cached.context = context
        cached.generations = sources
        return context

    def instance_diff(
        self,
        context: InstanceContext,
        *,
        respect_ignore: bool = True,
        enrich_actual: bool = True,
        existing_files: frozenset[str] | set[str] | None = None,
    ) -> dict[str, Any]:
        transport = LocalFilesystemInstance(context.instance.path)
        existing = set(transport.list_files()) if existing_files is None else set(existing_files)
        existing -= set(disabled_package_aliases(context.resolution.state.packages))
        desired = set(context.files)
        items: list[dict[str, Any]] = []
        summary = {
            "add": 0,
            "remove": 0,
            "change": 0,
            "update": 0,
            "rename": 0,
            "ignored": 0,
        }
        ignored: set[str] = set()
        for path in existing | desired:
            if respect_ignore and any(
                selector.contains(path) for selector in context.resolution.state.ignore
            ):
                ignored.add(path)
                summary["ignored"] += 1
        desired_only = desired - existing - ignored
        actual_only = existing - desired - ignored
        desired_hashes = {
            path: hashlib.sha256(context.files[path]).hexdigest() for path in desired_only
        }
        actual_content: dict[str, bytes] = {}
        actual_hashes: dict[str, str] = {}
        if enrich_actual:
            actual_content = {path: transport.read(path) for path in actual_only}
            actual_hashes = {
                path: hashlib.sha256(content).hexdigest()
                for path, content in actual_content.items()
            }
            used_actual: set[str] = set()
            for path in sorted(tuple(desired_only)):
                candidates = [
                    item
                    for item in actual_only - used_actual
                    if actual_hashes[item] == desired_hashes[path]
                ]
                reverse_count = sum(
                    1 for item in desired_only if desired_hashes[item] == desired_hashes[path]
                )
                if len(candidates) != 1 or reverse_count != 1:
                    continue
                source_path = candidates[0]
                used_actual.add(source_path)
                desired_only.remove(path)
                package = self._package_for_path(context, path)
                item = self._delta_item(
                    context,
                    "rename",
                    source_path,
                    package_id=package.identity if package else None,
                )
                item.update(
                    {
                        "from_path": source_path,
                        "to_path": path,
                        "desired_sha256": desired_hashes[path],
                        "actual_sha256": actual_hashes[source_path],
                        "capture_modes": ["rename"],
                    }
                )
                items.append(item)
                summary["rename"] += 1
            actual_only -= used_actual

        for path in sorted(desired_only):
            package = self._package_for_path(context, path)
            item = self._delta_item(
                context,
                "remove",
                path,
                package_id=package.identity if package else None,
            )
            item["desired_sha256"] = hashlib.sha256(context.files[path]).hexdigest()
            item["capture_modes"] = ["activation"] if package else ["remove"]
            item["enriched"] = enrich_actual
            items.append(item)
            summary["remove"] += 1

        for path in sorted(actual_only):
            package_kind = self._package_kind_for_path(path)
            package_metadata = None
            if enrich_actual and package_kind is not None:
                package_metadata = self._package_content_metadata(path, actual_content[path])
            item = self._delta_item(context, "add", path)
            if not enrich_actual and package_kind is not None:
                item["kind"] = "package"
            if enrich_actual:
                item["actual_sha256"] = actual_hashes[path]
            item["capture_modes"] = (
                ["package", "payload"]
                if package_metadata is not None
                or (not enrich_actual and package_kind is not None)
                else ["payload"]
            )
            item["enriched"] = enrich_actual
            if package_metadata is not None:
                item["kind"] = "package"
                item["proposed_package_kind"] = package_kind
                version = package_metadata.version if package_kind == "mod" else None
                if package_kind == "mod":
                    item["jar_metadata"] = package_metadata.document()
                self._set_provider_fields(item, actual_content[path], version)
            items.append(item)
            summary["add"] += 1

        for path in sorted((existing & desired) - ignored):
            desired_content = context.files[path]
            current_content = transport.read(path)
            if desired_content == current_content:
                continue
            package = self._package_for_path(context, path)
            metadata = (
                self._package_content_metadata(path, current_content, kind=str(package.kind))
                if enrich_actual and package
                else None
            )
            status = "change"
            if (
                package is not None
                and metadata is not None
                and package.kind == "mod"
                and metadata.version is not None
                and package.version is not None
                and _version_is_newer(metadata.version, package.version)
                and (
                    {
                        package.identity,
                        package.logical_identity,
                        *(tuple([package.alias]) if package.alias is not None else ()),
                    }
                    & set(metadata.mod_ids)
                )
            ):
                status = "update"
            item = self._delta_item(
                context,
                status,
                path,
                package_id=package.identity if package else None,
            )
            item["desired_sha256"] = hashlib.sha256(desired_content).hexdigest()
            item["actual_sha256"] = hashlib.sha256(current_content).hexdigest()
            package_kind = self._package_kind_for_path(path)
            item["capture_modes"] = (
                ["package"]
                if package
                else ["package", "payload"]
                if package_kind is not None
                else ["payload"]
            )
            if package is None and package_kind is not None:
                item["kind"] = "package"
            item["enriched"] = enrich_actual or package is None
            if package is not None:
                item["desired_version"] = package.version
                item["logical_id"] = package.logical_identity
                if enrich_actual:
                    version = metadata.version if package.kind == "mod" and metadata else None
                    self._set_provider_fields(
                        item,
                        current_content,
                        version,
                        allow_remote=metadata is not None,
                    )
            if metadata is not None and package is not None and package.kind == "mod":
                item["jar_metadata"] = metadata.document()
                item["actual_version"] = metadata.version
            elif enrich_actual and package_kind is not None:
                self._enrich_package_item(context, item, current_content)
            items.append(item)
            summary[status] += 1
        items.sort(key=lambda item: (str(item.get("path", "")), str(item["status"])))
        summary_document: dict[str, int | None] = dict(summary)
        if not enrich_actual:
            summary_document["change"] = None
            summary_document["update"] = None
            summary_document["rename"] = None
        return {
            "items": items,
            "summary": summary_document,
            "loaded": True,
            "complete": enrich_actual,
        }

    def invalidate_explorer(self, instance: str) -> None:
        self._explorer_snapshots.pop(instance, None)

    def _auto_payload_epoch_path(self, target: Path) -> Path:
        canonical = str(target.resolve()).casefold().encode("utf-8")
        identity = hashlib.sha256(canonical).hexdigest()
        return self.state.root / "locks" / "layers" / f"{identity}.auto-payload.epoch"

    def _auto_payload_epoch(self, target: Path) -> str | None:
        path = self._auto_payload_epoch_path(target)
        try:
            return path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise MetadataError(f"cannot read physical layer epoch {path}: {exc}") from exc

    def _context_auto_payload_epochs(
        self, context: InstanceContext
    ) -> tuple[tuple[str, str | None], ...]:
        roots = {Path(trace.root).resolve() for trace in context.resolution.trace}
        return tuple(
            (str(root), self._auto_payload_epoch(root))
            for root in sorted(roots, key=lambda value: str(value).casefold())
        )

    def _snapshot_auto_payload_epochs_match(self, snapshot: ExplorerSnapshot) -> bool:
        return all(
            self._auto_payload_epoch(Path(root)) == expected
            for root, expected in snapshot.auto_payload_epochs
        )

    @staticmethod
    def _source_generation(path: Path) -> Generation:
        return generation(path)

    def _context_source_generations(self, context: InstanceContext) -> SourceGenerations:
        with self._context_lock:
            cached = self._contexts.get(context.instance.name)
            if (
                cached is not None
                and cached.context is not None
                and cached.context.resolution.state is context.resolution.state
                and len(cached.context.files) == len(context.files)
                and all(
                    cached.context.files.get(path) is content
                    for path, content in context.files.items()
                )
            ):
                # These stamps precede the reads that produced this context. Re-enumerating
                # now adds a full source scan and could bless a concurrent source edit.
                return (*cached.generations, *cached.loader.source_generations())
        paths = {
            self.state.instances_path,
            self.state.links_path,
            self.state.parameters_path(context.instance.name),
        }
        for root in {Path(trace.root).resolve() for trace in context.resolution.trace}:
            pending = [root]
            while pending:
                directory = pending.pop()
                paths.add(directory)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        paths.add(path)
                        if path == root / METADATA_DIR:
                            # Root metadata documents are consumed by the configured
                            # frontend; artifact storage and other internals are not payload.
                            paths.add(path.resolve())
                            for metadata in path.iterdir():
                                if metadata.suffix not in self.metadata.extensions:
                                    continue
                                paths.add(metadata)
                                if metadata.is_symlink():
                                    paths.add(metadata.resolve())
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(path)
        return tuple(
            (path, self._source_generation(path))
            for path in sorted(paths, key=lambda value: str(value))
        )

    def _snapshot_source_generations_match(self, snapshot: ExplorerSnapshot) -> bool:
        # Recheck saved files and directories without rereading payload or resolving layers.
        # Directory stats detect additions as well as changes to the already-known files.
        try:
            return all(
                self._source_generation(path) == expected
                for path, expected in snapshot.source_generations
            )
        except OSError:
            return False

    @staticmethod
    def _context_fingerprint(context: InstanceContext) -> str:
        digest = hashlib.sha256()
        for path, content in sorted(context.files.items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(content_digest(content, context.content_digests)))
        digest.update(
            json.dumps(
                {
                    "ignore": [item.document() for item in context.resolution.state.ignore],
                    "exclusions": list(context.resolution.state.exclusions),
                    "trace": [
                        {
                            "root": trace.root,
                            "producers": [
                                {
                                    "source": producer.source,
                                    "operations": [
                                        operation_to_json(
                                            operation,
                                            file_digest=lambda content: content_digest(
                                                content, context.content_digests
                                            ),
                                        )
                                        for operation in producer.operations
                                    ],
                                }
                                for producer in trace.producers
                            ],
                        }
                        for trace in context.resolution.trace
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return digest.hexdigest()

    def explorer_snapshot(self, instance: str, *, refresh: bool = False) -> ExplorerSnapshot:
        cached = self._explorer_snapshots.get(instance)
        if not refresh and cached is not None:
            desired_paths = {path for path, entry in cached.entries.items() if entry["desired"]}
            if (
                self._snapshot_auto_payload_epochs_match(cached)
                and self._snapshot_source_generations_match(cached)
                and scan_generation_matches(cached.scan, desired_paths)
            ):
                return cached
            self.invalidate_explorer(instance)
        context = self.instance_context(instance)
        physical = scan_instance(context.instance.path)
        underlying = self.instance_diff(
            context,
            respect_ignore=False,
            enrich_actual=False,
            existing_files=physical.files,
        )
        snapshot = build_snapshot(
            context.instance.path,
            context.files,
            underlying["items"],
            context.resolution.state.ignore,
            scan=physical,
            context_fingerprint=self._context_fingerprint(context),
            auto_payload_epochs=self._context_auto_payload_epochs(context),
            source_generations=self._context_source_generations(context),
        )
        self._explorer_snapshots[instance] = snapshot
        return snapshot

    def explorer_page(
        self,
        instance: str,
        parent: str = "",
        *,
        mode: str = "all",
        show_ignored: bool = False,
        cursor: int = 0,
        limit: int = 200,
        target_layer: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self._empty_page_snapshot(instance, parent)
        page = snapshot.page(
            parent,
            mode=mode,
            show_ignored=show_ignored,
            cursor=cursor,
            limit=limit,
        )
        prefetches: list[_PackagePrefetch] = []
        for item in page["items"]:
            path = str(item["path"])
            if (
                item.get("type") != "file"
                or not item.get("actual")
                or item.get("delta_id") is None
                or "package" not in item.get("capture_modes", [])
            ):
                continue
            expected = snapshot.scan.entry_generations.get(path)
            if expected is not None:
                delta = next(
                    (
                        delta
                        for delta in snapshot.delta_items
                        if str(delta["id"]) == str(item["delta_id"])
                        and str(delta["path"]) == path
                    ),
                    None,
                )
                if delta is not None:
                    inferred_kind = self._package_kind_for_path(path)
                    proposed_kind = delta.get("proposed_package_kind")
                    prefetches.append(
                        _PackagePrefetch(
                            snapshot.scan.root,
                            path,
                            expected,
                            delta.get("package_id") is not None,
                            str(proposed_kind) if proposed_kind is not None else inferred_kind,
                        )
                    )
        if prefetches:
            self._schedule_package_prefetch(instance, tuple(prefetches))
        page["ignore_rules"] = self.ignore_rules(instance, target_layer)
        return page

    def _empty_page_snapshot(self, instance: str, parent: str) -> ExplorerSnapshot:
        cached = self._explorer_snapshots.get(instance)
        try:
            normalized = normalize_relative_path(parent) if parent else ""
        except MetadataError:
            return self.explorer_snapshot(instance)
        if (
            cached is None
            or cached.children.get(normalized, ())
            or not self._snapshot_auto_payload_epochs_match(cached)
            or not self._snapshot_source_generations_match(cached)
        ):
            return self.explorer_snapshot(instance)
        expected = cached.scan.entry_generations.get(normalized)
        if expected is None or expected[0] != "directory":
            return self.explorer_snapshot(instance)
        target = (
            cached.scan.root
            if not normalized
            else cached.scan.root.joinpath(*normalized.split("/"))
        )
        try:
            current = target.stat(follow_symlinks=False)
        except OSError:
            return self.explorer_snapshot(instance)
        if ("directory", current.st_mtime_ns, current.st_size, current.st_ino) != expected:
            return self.explorer_snapshot(instance)
        return cached

    def explorer_plan(
        self, instance: str, path: str, target_layer: str | None = None
    ) -> dict[str, Any]:
        normalized = normalize_relative_path(path)
        snapshot = self.explorer_snapshot(instance)
        if not snapshot.context_verified:
            snapshot = self.explorer_snapshot(instance, refresh=True)
        selected = snapshot.entries.get(normalized)
        if selected is None:
            raise MetadataError(f"explorer path does not exist: {normalized!r}")
        prefix = normalized + "/"
        context = self.instance_context(instance)
        if snapshot.context_fingerprint != self._context_fingerprint(context):
            self.invalidate_explorer(instance)
            raise MetadataError("explorer snapshot changed; refresh the file plan")
        physical_target = self._target_path(instance, context, target_layer)
        transport = LocalFilesystemInstance(context.instance.path)
        items = []
        seen: set[str] = set()
        for item in snapshot.delta_items:
            item_path = normalize_relative_path(str(item["path"]))
            destination = str(item.get("to_path", ""))
            if self._delta_ignored(item, snapshot.selectors):
                continue
            in_scope = item_path == normalized or item_path.startswith(prefix)
            if destination:
                destination_path = normalize_relative_path(destination)
                in_scope = (
                    in_scope
                    or destination_path == normalized
                    or destination_path.startswith(prefix)
                )
            if in_scope and str(item["id"]) not in seen:
                seen.add(str(item["id"]))
                planned = dict(item)
                source_entry = snapshot.entries.get(item_path)
                if source_entry is not None and source_entry["actual"]:
                    content = transport.read(item_path)
                    planned["expected_sha256"] = hashlib.sha256(content).hexdigest()
                    if (
                        self._package_kind_for_path(item_path) is not None
                        or self._package_for_path(context, item_path) is not None
                    ):
                        self._enrich_package_item(context, planned, content)
                else:
                    planned["expected_absent"] = True
                reason = self._same_layer_capture_reason(physical_target, item)
                if item["status"] == "add" and any(
                    matches(str(item["path"]), pattern)
                    for pattern in context.resolution.state.exclusions
                ):
                    reason = "inherited remove_paths excludes this path"
                planned["supported"] = reason is None
                if reason is not None:
                    planned["reason"] = reason
                items.append(planned)
        conflicts = [
            {
                "path": entry["path"],
                "reason": entry["reason"],
            }
            for entry in snapshot.entries.values()
            if (entry["path"] == normalized or entry["path"].startswith(prefix))
            and entry["changed"]
            and not any(selector.contains(entry["path"]) for selector in snapshot.selectors)
            and not entry["actionable"]
        ]
        return {"path": normalized, "items": items, "unsupported": conflicts}

    def ignore_rules(self, instance: str, target_layer: str | None = None) -> list[dict[str, Any]]:
        context = self.instance_context(instance, prepare=False)
        return self._ignore_rules(context, target_layer)

    def mutation_projection(
        self, instance: str, *, activation: bool = False, ignore: bool = False,
        target_layer: str | None = None,
    ) -> dict[str, Any]:
        """Project a just-validated mutation without preparing or scanning sources again.

        The HTTP caller holds the instance mutation lock and calls this only after a
        successful activation or ignore edit, which validates and publishes its context.
        """
        with self._context_lock:
            context = self._contexts[instance].context
            if context is None:
                raise RuntimeError("mutation did not publish a validated context")
            result: dict[str, Any] = {}
            if activation:
                state = context.resolution.state
                result["activation_patch"] = {
                    "packages": [
                        {
                            "id": package.identity,
                            "activation": (
                                "enabled" if package.activation is Activation.TRANSITIVE
                                else package.activation.value
                            ),
                            **(
                                {"activation_reason": "dependency"}
                                if package.activation is Activation.TRANSITIVE else {}
                            ),
                        }
                        for package in state.packages.values()
                    ],
                    "groups": [
                        {"id": group.identity, "activation": group.activation.value}
                        for group in state.groups.values()
                    ],
                }
            if ignore:
                result["ignore_rules"] = self._ignore_rules(context, target_layer)
            return result

    def _ignore_rules(
        self, context: InstanceContext, target_layer: str | None = None
    ) -> list[dict[str, Any]]:
        instance = context.instance.name
        target = self._target_path(instance, context, target_layer)
        rules: list[dict[str, Any]] = []
        for trace in context.resolution.trace:
            trace_root = Path(trace.root).resolve()
            for producer in trace.producers:
                for index, operation in enumerate(producer.operations):
                    if not isinstance(operation, AddIgnoreSelector):
                        continue
                    own = trace_root == target
                    identity = {
                        "target": self._layer_reference(target),
                        "source": producer.source,
                        "index": index,
                        "selector": operation.selector.document(),
                    }
                    rules.append(
                        {
                            "id": hashlib.sha256(
                                json.dumps(identity, sort_keys=True).encode("utf-8")
                            ).hexdigest()[:20],
                            "selector": operation.selector.document(),
                            "layer": trace.layer,
                            "source": producer.source,
                            "operation_index": index,
                            "own": own,
                            "removable": own,
                            "identity": identity,
                        }
                    )
        return rules

    @_metadata_edit
    def add_ignore(
        self,
        instance: str,
        path: str,
        selector_kind: str,
        target_layer: str | None = None,
    ) -> None:
        context = self.instance_context(instance)
        cached = self._explorer_snapshots.get(instance)
        if cached is not None and not self._snapshot_source_generations_match(cached):
            self.invalidate_explorer(instance)
            cached = None
        target = self._target_path(instance, context, target_layer)
        try:
            kind = SelectorKind(selector_kind)
        except ValueError as exc:
            raise MetadataError("ignore selector kind must be exact or subtree") from exc
        if kind is SelectorKind.GLOB:
            raise MetadataError("new explorer ignore rules must be exact or subtree")
        selector = IgnoreSelector(kind, path)
        journal_paths = [
            target / METADATA_DIR / MARKER_FILE,
            target / METADATA_DIR / "local-edits.json",
            self.state.links_path,
        ]
        with self._target_layer_edit_lock(target), _BeforeImageJournal(
            self, target, journal_paths
        ):
            editable = self._editable_target(instance, context, target_layer)
            self._append_operation(
                editable,
                {"type": "ignore", "selector": selector.document()},
                instance=instance,
            )
            refreshed = self.instance_context(instance)
        if cached is not None:
            self._explorer_snapshots[instance] = cached.reproject(
                refreshed.resolution.state.ignore,
                context_fingerprint=self._context_fingerprint(refreshed),
                source_generations=self._context_source_generations(refreshed),
            )

    def remove_ignore(
        self,
        instance: str,
        identity: object,
        target_layer: str | None = None,
    ) -> None:
        if not isinstance(identity, dict):
            raise MetadataError("ignore rule identity must be an object")
        context = self.instance_context(instance)
        cached = self._explorer_snapshots.get(instance)
        if cached is not None and not self._snapshot_source_generations_match(cached):
            self.invalidate_explorer(instance)
            cached = None
        target = self._target_path(instance, context, target_layer)
        expected_target = self._layer_reference(target)
        if identity.get("target") != expected_target:
            raise MetadataError("ignore rule does not belong to the selected target layer")
        source = identity.get("source")
        if not isinstance(source, str) or not any(
            rule["identity"] == identity and rule["removable"]
            for rule in self._ignore_rules(context, target_layer)
        ):
            raise MetadataError("inherited ignore rule cannot be removed, or the rule has moved")
        index = identity.get("index")
        if not isinstance(index, int) or index < 0:
            raise MetadataError("ignore rule identity has an invalid operation index")
        with self._target_layer_edit_lock(target):
            path = confined_path(target, source)
            with _BeforeImageJournal(self, target, [path]):
                try:
                    if path.suffix == ".mcpack":
                        document = McpackParser().parse(path.read_bytes(), path)
                        operation_lines = [
                            line for line in document.lines
                            if isinstance(line.record, dict)
                            and line.record.get("type") != "section"
                        ]
                        line = operation_lines[index]
                        raw = line.record
                    else:
                        object_document = dict(self.metadata.read_object(path))
                        operations = object_document["operations"]
                        raw = operations[index]
                except (
                    MetadataError,
                    KeyError,
                    IndexError,
                    TypeError,
                ) as exc:
                    raise MetadataError("ignore rule moved or no longer exists") from exc
                if not isinstance(raw, dict):
                    raise MetadataError("ignore rule moved or no longer exists")
                marker_path = self._layer_marker_path(target)
                schema = (
                    2 if marker_path is not None and marker_path.suffix != ".json"
                    else int(JsonMetadataParser().read_object(marker_path).get("schema", 1))
                    if marker_path is not None else 1
                )
                parsed = operation_from_json(
                    raw if path.suffix == ".mcpack" else decode_record_paths(raw, source, schema),
                    source=source,
                    index=index,
                )
                if not isinstance(
                    parsed, AddIgnoreSelector
                ) or parsed.selector.document() != identity.get("selector"):
                    raise MetadataError("ignore rule moved or no longer matches its identity")
                if path.suffix == ".mcpack":
                    physical = document.text.splitlines(keepends=True)
                    del physical[line.line_number - 1]
                    atomic_write(path, "".join(physical).encode("utf-8"))
                else:
                    del operations[index]
                    atomic_write(
                        path,
                        self.metadata.encode_object(object_document, destination=path),
                    )
                refreshed = self.instance_context(instance)
        if cached is not None:
            self._explorer_snapshots[instance] = cached.reproject(
                refreshed.resolution.state.ignore,
                context_fingerprint=self._context_fingerprint(refreshed),
                source_generations=self._context_source_generations(refreshed),
            )

    @_metadata_edit
    def capture_explorer_path(
        self,
        instance: str,
        path: str,
        target_layer: str | None = None,
        decisions: object = None,
        *,
        auto_payload: bool = False,
    ) -> dict[str, Any] | None:
        if auto_payload:
            return self._capture_explorer_auto_payload(instance, path, target_layer)
        normalized = normalize_relative_path(path)
        snapshot = self._explorer_snapshots.get(instance)
        if snapshot is None or not snapshot.context_verified:
            snapshot = self.explorer_snapshot(instance, refresh=snapshot is not None)
        selected = snapshot.entries.get(normalized)
        if selected is None:
            raise MetadataError(f"explorer path does not exist: {normalized!r}")
        prefix = normalized + "/"
        scoped_entries = self._entries_in_scope(snapshot.entries.values(), normalized)
        unsupported = next(
            (
                item for item in scoped_entries
                if item["changed"] and not item["actionable"]
                and not any(selector.contains(item["path"]) for selector in snapshot.selectors)
            ),
            None,
        )
        if unsupported is not None:
            reason = unsupported["reason"] or "unsupported entry"
            raise MetadataError(f"cannot capture {unsupported['path']!r}: {reason}")
        delta_by_id = {
            str(item["id"]): dict(item)
            for item in snapshot.delta_items
            if self._delta_in_scope(item, normalized)
            and not self._delta_ignored(item, snapshot.selectors)
        }
        if not delta_by_id:
            raise MetadataError(f"explorer path has no changed descendants: {normalized!r}")
        if not isinstance(decisions, dict):
            raise MetadataError("folder capture decisions must be an object")
        decision_map = decisions
        if set(decision_map) != set(delta_by_id):
            raise MetadataError("folder capture decisions must match every planned delta exactly")
        for item_id, raw in decision_map.items():
            if not isinstance(raw, dict):
                raise MetadataError(f"delta {item_id} decision must be an object")
            has_sha = "expected_sha256" in raw
            has_absent = "expected_absent" in raw
            if has_sha == has_absent:
                raise MetadataError(
                    f"delta {item_id} requires exactly one expected_sha256 or expected_absent"
                )
            if has_sha and not isinstance(raw["expected_sha256"], str):
                raise MetadataError(f"delta {item_id} expected_sha256 must be a string")
            if has_absent and raw["expected_absent"] is not True:
                raise MetadataError(f"delta {item_id} expected_absent must be true")
        provisional_target = self._provisional_target(instance, target_layer)
        final_context: InstanceContext | None = None
        pure_payload_committed = False

        with self._target_layer_edit_lock(provisional_target):
            context = self.instance_context(instance)
            target = self._target_path(instance, context, target_layer)
            if target.resolve() != provisional_target.resolve():
                raise MetadataError("selected physical target changed before capture")
            if snapshot.context_fingerprint != self._context_fingerprint(context):
                raise MetadataError("file plan changed before capture; refresh it")
            expected_inventory = {
                path: entry["type"]
                for path, entry in snapshot.scan.entries.items()
                if path == normalized or path.startswith(prefix)
            }
            current_inventory = self._scan_scope(context.instance.path, normalized)
            if current_inventory != expected_inventory:
                raise MetadataError("instance folder changed before capture; refresh its plan")

            transport = LocalFilesystemInstance(context.instance.path)
            prepared: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
            for item in sorted(delta_by_id.values(), key=lambda value: str(value["path"])):
                item_id = str(item["id"])
                raw = decision_map.get(item_id, {})
                if not isinstance(raw, dict):
                    raise MetadataError(f"delta {item_id} decision must be an object")
                modes = [str(value) for value in item.get("capture_modes", [])]
                if len(modes) > 1 and not isinstance(raw.get("mode"), str):
                    raise MetadataError(
                        f"delta {item_id} requires an explicit package or payload decision"
                    )
                mode = str(raw.get("mode", modes[0] if len(modes) == 1 else ""))
                if mode not in modes:
                    raise MetadataError(
                        f"capture mode {mode!r} is not available for {item['path']!r}"
                    )
                provider_match = raw.get("provider_match")
                provider_is_local = provider_match is None or provider_match == "local"
                if isinstance(provider_match, dict):
                    provider_is_local = provider_match.get("provider") == "local"
                if not provider_is_local and mode != "package":
                    raise MetadataError(
                        "remote provider enrichment requires package capture mode"
                    )
                if isinstance(provider_match, dict) and not provider_is_local and (
                    provider_match.get("confidence") != "exact"
                    or not all(
                        isinstance(provider_match.get(field), str) and provider_match[field]
                        for field in ("project_id", "version_id", "download_url")
                    )
                    or not isinstance(provider_match.get("hashes"), dict)
                ):
                    raise MetadataError(
                        "review the package individually before choosing an exact provider match"
                    )
                item_path = normalize_relative_path(str(item["path"]))
                prepared.append((item, raw, mode, item_path))

            source_paths = [
                item_path
                for _, _, _, item_path in prepared
                if (
                    (physical := snapshot.scan.entries.get(item_path)) is not None
                    and physical["type"] == "file"
                )
            ]
            with ThreadPoolExecutor(max_workers=min(32, max(1, len(source_paths)))) as pool:
                source_content = dict(
                    zip(source_paths, pool.map(transport.read, source_paths), strict=True)
                )

            captured: list[tuple[dict[str, Any], str, bytes | None]] = []
            for item, raw, mode, item_path in prepared:
                item_id = str(item["id"])
                physical = snapshot.scan.entries.get(item_path)
                content: bytes | None = None
                if physical is not None and physical["type"] == "file":
                    content = source_content[item_path]
                    actual_sha256 = hashlib.sha256(content).hexdigest()
                    expected_sha256 = raw.get("expected_sha256")
                    if not isinstance(expected_sha256, str):
                        raise MetadataError(
                            f"delta {item_id} requires expected_sha256 from its file plan"
                        )
                    if expected_sha256 is not None and expected_sha256 != actual_sha256:
                        raise MetadataError(
                            f"instance delta file changed before mutation: {item_path!r}"
                        )
                else:
                    if raw.get("expected_absent") is not True:
                        raise MetadataError(
                            f"delta {item_id} requires expected_absent from its file plan"
                        )
                self._revalidate_batch_item(context, target, item, content)
                if mode == "package":
                    existing_id = item.get("package_id")
                    existing_kind = (
                        str(context.resolution.state.packages[str(existing_id)].kind)
                        if existing_id is not None
                        else None
                    )
                    if content is None or (
                        self._package_content_metadata(
                            item_path, content, kind=existing_kind
                        )
                        is None
                        and (existing_id is None or existing_kind != "mod")
                    ):
                        raise MetadataError(
                            f"package capture requires a valid package: {item_path!r}"
                        )
                captured.append((item, mode, content))

            payload_writes: dict[Path, bytes] = {}
            artifact_writes: dict[Path, bytes] = {}
            additions: list[dict[str, Any]] = []
            operation_contexts: dict[str, dict[str, Any]] = {}
            package_selections: dict[
                str, tuple[PackageSelection, bytes, str, dict[str, str]]
            ] = {}
            exact_match_seeds = []
            reserved_ids = set(context.resolution.state.packages)
            reconciliations: dict[str, bytes | None] = {}
            for item, mode, content in captured:
                item_path = normalize_relative_path(str(item["path"]))
                reconciliations[item_path] = content
                package_id = item.get("package_id")
                if package_id is not None and content is not None:
                    artifact, operation = self._batch_existing_package(
                        context, target, str(package_id), item_path, content
                    )
                    artifact_writes[artifact] = content
                    additions.extend(
                        self._package_import_operations(
                            context, operation, source_path=item_path
                        )
                    )
                elif package_id is not None:
                    additions.append({"type": "activation", "id": package_id, "state": "disabled"})
                elif content is None:
                    additions.append({"type": "remove", "path": item_path})
                elif mode == "package":
                    artifact, operation = self._batch_local_package(
                        target, item_path, content, reserved_ids
                    )
                    artifact_writes[artifact] = content
                    additions.extend(
                        self._package_import_operations(
                            context, operation, source_path=item_path
                        )
                    )
                else:
                    payload_writes[self._bounded_target_path(target, item_path)] = content
                if content is not None and (package_id is not None or mode == "package"):
                    match = self._selected_provider_match(
                        decision_map[str(item["id"])].get("provider_match"), content
                    )
                    if match is not None:
                        operation = additions[-1]
                        self._validate_provider_kind(match, str(operation["kind"]))
                        digest = hashlib.sha256(content).hexdigest()
                        source, selection, default_destination = self._native_provider_match(
                            match, str(operation["kind"]), str(operation["destination"]), digest
                        )
                        artifact_writes.pop(artifact, None)
                        cache_path = self.state.root / "cache" / "sha256" / digest[:2] / digest
                        artifact_writes[cache_path] = content
                        operation["source"] = source
                        operation["update_policy"] = "fixed"
                        operation["version"] = selection.selector
                        if operation.get("destination") == default_destination:
                            operation.pop("destination", None)
                        existing = context.resolution.state.packages.get(str(operation["id"]))
                        if existing is not None and not existing.automatic_requires_explicit:
                            operation.pop("automatic_requires", None)
                        package_selections[str(operation["id"])] = (
                            selection,
                            content,
                            str(operation.get("destination", default_destination or "")),
                            source,
                        )
                        routing_context = self._provider_routing_context(match)
                        routing_context["package"]["default_destination"] = default_destination
                        operation_contexts[str(operation["id"])] = routing_context

            operations_path = target / METADATA_DIR / "local-edits.json"
            pure_payload_fast_path = (
                not additions
                and not artifact_writes
                and len(payload_writes) == len(captured)
                and self._payload_target_has_final_precedence(instance, context, target)
            )
            journal_paths = [
                target / METADATA_DIR / MARKER_FILE,
                operations_path,
                self.state.links_path,
                *payload_writes,
                *artifact_writes,
            ]
            with _BeforeImageJournal(self, target, journal_paths):
                editable = self._editable_target(instance, context, target_layer)
                if editable.resolve() != target.resolve():
                    raise MetadataError("editable target changed before capture")
                writes = [*payload_writes.items(), *artifact_writes.items()]

                def write(item: tuple[Path, bytes]) -> None:
                    self._batch_atomic_write(*item)

                with ThreadPoolExecutor(max_workers=min(32, max(1, len(writes)))) as pool:
                    tuple(pool.map(write, writes))
                if additions:
                    self._append_operations(
                        target, additions, instance=instance,
                        operation_contexts=operation_contexts, origin="instance_capture",
                    )
                try:
                    if package_selections:
                        logical_context = self.instance_context(instance, prepare=False)
                        cache = ArtifactCache(self.state.root / "cache")
                        for (
                            package_id,
                            (selection, content, destination, source),
                        ) in package_selections.items():
                            package = self._resolved_import_package(
                                logical_context, package_id, source, selection, destination
                            )
                            if package is None:
                                raise MetadataError(
                                    f"captured provider package did not resolve: {package_id!r}"
                                )
                            exact_match_seeds.append(
                                cache.seed_exact_match(package, selection, content)
                            )
                    if pure_payload_fast_path:
                        expected_writes = {
                            self._bounded_target_path(target, item_path): actual_content
                            for item_path, actual_content in reconciliations.items()
                            if actual_content is not None
                        }
                        if expected_writes != payload_writes:
                            raise MetadataError("pure payload batch write set did not reconcile")
                        pure_payload_committed = True
                    else:
                        final_context = self.instance_context(instance)
                        preserved = set(
                            disabled_package_aliases(final_context.resolution.state.packages)
                        )
                        for item_path, actual_content in reconciliations.items():
                            if item_path in preserved:
                                continue
                            if final_context.files.get(item_path) != actual_content:
                                raise MetadataError(
                                    f"captured folder did not reconcile changed path {item_path!r}"
                                )
                except Exception:
                    cache = ArtifactCache(self.state.root / "cache")
                    for seed in reversed(exact_match_seeds):
                        cache.restore_exact_match_seed(seed)
                    raise

        if pure_payload_committed:
            # Creating an overlay changes the resolution trace. The payload write itself is
            # proven above, but retaining the old snapshot would make later plans stale.
            self.invalidate_explorer(instance)
            return None

        if final_context is None:
            raise RuntimeError("batch capture completed without a final context")
        outside_before = {
            path: content
            for path, content in context.files.items()
            if path != normalized and not path.startswith(prefix)
        }
        outside_after = {
            path: content
            for path, content in final_context.files.items()
            if path != normalized and not path.startswith(prefix)
        }
        if outside_before == outside_after:
            remaining_items = [
                dict(item)
                for item in snapshot.delta_items
                if not self._delta_in_scope(item, normalized)
            ]
            self._explorer_snapshots[instance] = build_snapshot(
                final_context.instance.path,
                final_context.files,
                remaining_items,
                final_context.resolution.state.ignore,
                scan=snapshot.scan,
                context_fingerprint=self._context_fingerprint(final_context),
                auto_payload_epochs=self._context_auto_payload_epochs(final_context),
                source_generations=self._context_source_generations(final_context),
            )
        else:
            self.invalidate_explorer(instance)

        return None

    def _capture_explorer_auto_payload(
        self,
        instance: str,
        path: str,
        target_layer: str | None,
    ) -> dict[str, Any]:
        """Capture an unambiguous raw-payload scope without building an explorer plan."""
        normalized = normalize_relative_path(path)
        prefix = normalized + "/"
        provisional_target = self._provisional_target(instance, target_layer)

        with self._target_layer_edit_lock(provisional_target):
            context = self.instance_context(instance)
            target = self._target_path(instance, context, target_layer)
            if target.resolve() != provisional_target.resolve():
                raise MetadataError("selected physical target changed before capture")
            cached = self._explorer_snapshots.get(instance)
            cache_candidate = cached
            if cache_candidate is not None and (
                not cache_candidate.context_verified
                or cache_candidate.context_fingerprint != self._context_fingerprint(context)
                or cache_candidate.scan.root != context.instance.path.resolve()
                or not self._snapshot_auto_payload_epochs_match(cache_candidate)
                or not scan_generation_matches(cache_candidate.scan, context.files)
            ):
                cache_candidate = None

            before = self._scan_scope_state(context.instance.path, normalized)
            source_paths = sorted(
                item_path for item_path, generation in before.items() if generation[0] == "file"
            )
            transport = LocalFilesystemInstance(context.instance.path)
            with ThreadPoolExecutor(max_workers=min(32, max(1, len(source_paths)))) as pool:
                source_content = dict(
                    zip(source_paths, pool.map(transport.read, source_paths), strict=True)
                )

            actual_files = set(source_content)
            desired_files = {
                item_path
                for item_path in context.files
                if item_path == normalized or item_path.startswith(prefix)
            }
            items: list[dict[str, Any]] = []
            contents: dict[str, bytes] = {}

            for item_path, generation in sorted(before.items()):
                if generation[0] in {"file", "directory"}:
                    continue
                item = self._delta_item(context, "add", item_path)
                item.update(
                    {
                        "supported": False,
                        "reason": f"instance path type {generation[0]!r} cannot be captured",
                        "expected_absent": True,
                    }
                )
                items.append(item)

            for item_path in sorted(desired_files - actual_files):
                package = self._package_for_path(context, item_path)
                item = self._delta_item(
                    context,
                    "remove",
                    item_path,
                    package_id=package.identity if package else None,
                )
                item["desired_sha256"] = hashlib.sha256(context.files[item_path]).hexdigest()
                item["capture_modes"] = ["activation"] if package else ["remove"]
                item["enriched"] = False
                item["expected_absent"] = True
                items.append(item)

            for item_path in sorted(actual_files):
                content = source_content[item_path]
                desired = context.files.get(item_path)
                if desired == content:
                    continue
                package = self._package_for_path(context, item_path)
                status = "add" if desired is None else "change"
                item = self._delta_item(
                    context,
                    status,
                    item_path,
                    package_id=package.identity if package else None,
                )
                digest = hashlib.sha256(content).hexdigest()
                item["actual_sha256"] = digest
                item["expected_sha256"] = digest
                item["enriched"] = (
                    package is None and self._package_kind_for_path(item_path) is None
                )
                if desired is not None:
                    item["desired_sha256"] = hashlib.sha256(desired).hexdigest()
                if package is not None:
                    item["capture_modes"] = ["package"]
                    item["desired_version"] = package.version
                    item["logical_id"] = package.logical_identity
                elif self._package_kind_for_path(item_path) is not None:
                    item["kind"] = "package"
                    item["capture_modes"] = ["package", "payload"]
                else:
                    item["capture_modes"] = ["payload"]
                contents[item_path] = content
                items.append(item)

            target_reason: str | None = None
            if not target.is_dir() or self._layer_marker_path(target) is None:
                target_reason = "auto payload capture requires an existing writable layer"
            elif not self._payload_target_has_final_precedence(instance, context, target):
                target_reason = "auto payload capture requires the final-precedence layer"

            eligible = bool(items) and target_reason is None
            for item in items:
                modes = item.get("capture_modes")
                reason = target_reason or self._same_layer_capture_reason(target, item)
                item_path = normalize_relative_path(str(item["path"]))
                if item.get("status") == "add" and any(
                    matches(item_path, pattern) for pattern in context.resolution.state.exclusions
                ):
                    reason = "inherited remove_paths excludes this path"
                if modes != ["payload"]:
                    eligible = False
                if reason is not None:
                    item["supported"] = False
                    item["reason"] = reason
                    eligible = False
                else:
                    item["supported"] = True

            items.sort(key=lambda item: (str(item.get("path", "")), str(item["status"])))
            if not eligible:
                return {"applied": False, "path": normalized, "items": items}

            payload_writes = {
                self._bounded_target_path(target, item_path): contents[item_path]
                for item_path in sorted(contents)
            }
            if len(payload_writes) != len(items):
                raise MetadataError("auto payload write set did not reconcile")
            captured_ids = {str(item["id"]) for item in items}
            if len(captured_ids) != len(items):
                cache_candidate = None
            if cache_candidate is not None:
                cached_items = {str(item["id"]): item for item in cache_candidate.delta_items}
                for item in items:
                    cached_item = cached_items.get(str(item["id"]))
                    cached_entry = cache_candidate.entries.get(str(item["path"]))
                    cached_generation = cache_candidate.scan.entry_generations.get(
                        str(item["path"])
                    )
                    if (
                        cached_item is None
                        or any(
                            cached_item.get(key) != item.get(key)
                            for key in ("path", "status", "capture_modes")
                        )
                        or not (
                            cached_entry is not None
                            and cached_entry["actual"]
                            and cached_entry["type"] == "file"
                            and cached_generation is not None
                            and cached_generation[0] == "file"
                        )
                    ):
                        cache_candidate = None
                        break
            if self._scan_scope_state(context.instance.path, normalized) != before:
                raise MetadataError("instance folder changed during auto payload capture")
            patched_snapshot: ExplorerSnapshot | None = None
            epoch_path = self._auto_payload_epoch_path(target)
            next_epoch = os.urandom(16).hex()
            with _BeforeImageJournal(self, target, [*payload_writes, epoch_path]):

                def write(item: tuple[Path, bytes]) -> None:
                    self._batch_atomic_write(*item)

                writes = list(payload_writes.items())
                with ThreadPoolExecutor(max_workers=min(32, max(1, len(writes)))) as pool:
                    tuple(pool.map(write, writes))

                for destination, content in writes:
                    destination_stat = destination.stat(follow_symlinks=False)
                    if not stat.S_ISREG(destination_stat.st_mode) or (
                        destination_stat.st_size != len(content)
                    ):
                        raise MetadataError(f"auto payload target did not reconcile {destination}")

                atomic_write(epoch_path, f"{next_epoch}\n".encode("ascii"))

                if cache_candidate is not None:
                    patched_files = dict(context.files)
                    patched_files.update(contents)
                    if scan_generation_matches(cache_candidate.scan, patched_files):
                        remaining_items = [
                            dict(item)
                            for item in cache_candidate.delta_items
                            if str(item["id"]) not in captured_ids
                        ]
                        patched_context = InstanceContext(
                            context.instance,
                            context.link,
                            context.features,
                            context.parameters,
                            context.resolution,
                            patched_files,
                            context.content_digests,
                        )
                        patched_snapshot = build_snapshot(
                            patched_context.instance.path,
                            patched_files,
                            remaining_items,
                            patched_context.resolution.state.ignore,
                            scan=cache_candidate.scan,
                            context_fingerprint=self._context_fingerprint(patched_context),
                            context_verified=False,
                            auto_payload_epochs=self._context_auto_payload_epochs(patched_context),
                            source_generations=self._context_source_generations(patched_context),
                        )

        for cached_instance in tuple(self._explorer_snapshots):
            if cached_instance != instance:
                self.invalidate_explorer(cached_instance)
        if (
            patched_snapshot is None
            or cache_candidate is None
            or self._explorer_snapshots.get(instance) is not cache_candidate
        ):
            self.invalidate_explorer(instance)
        else:
            self._explorer_snapshots[instance] = patched_snapshot
        return {
            "applied": True,
            "path": normalized,
            "captured_count": len(items),
        }

    @staticmethod
    def _entries_in_scope(
        entries: Iterable[dict[str, Any]], normalized: str
    ) -> list[dict[str, Any]]:
        prefix = normalized + "/"
        return [
            item
            for item in entries
            if item["path"] == normalized or item["path"].startswith(prefix)
        ]

    @staticmethod
    def _delta_ignored(item: dict[str, Any], selectors: tuple[IgnoreSelector, ...]) -> bool:
        paths = [normalize_relative_path(str(item["path"]))]
        if item.get("to_path"):
            paths.append(normalize_relative_path(str(item["to_path"])))
        return any(selector.contains(path) for selector in selectors for path in paths)

    @staticmethod
    def _delta_in_scope(item: dict[str, Any], normalized: str) -> bool:
        prefix = normalized + "/"
        paths = [normalize_relative_path(str(item["path"]))]
        if item.get("to_path"):
            paths.append(normalize_relative_path(str(item["to_path"])))
        return any(path == normalized or path.startswith(prefix) for path in paths)

    def _provisional_target(self, instance: str, target_layer: str | None) -> Path:
        overlay = self.state.feature_root(instance).resolve()
        if target_layer is None or target_layer == self._layer_reference(overlay):
            return overlay
        return self.branch_root(target_layer)

    @contextmanager
    def _target_layer_edit_lock(self, layer: Path) -> Iterator[None]:
        """Acquire the cross-process layer lock once for a nested edit workflow."""
        target = layer.resolve()
        if any(owner is self and locked == target for owner, locked in _edit_layer_locks.get()):
            yield
            return
        with self.state.target_layer_lock(target):
            token = _edit_layer_locks.set((*_edit_layer_locks.get(), (self, target)))
            try:
                yield
            finally:
                _edit_layer_locks.reset(token)

    @staticmethod
    def _scan_scope(root: Path, normalized: str) -> dict[str, str]:
        return {
            path: generation[0]
            for path, generation in ProjectService._scan_scope_state(root, normalized).items()
        }

    @staticmethod
    def _scan_scope_state(root: Path, normalized: str) -> dict[str, tuple[str, int, int, int, int]]:
        target = root.joinpath(*normalized.split("/"))
        result: dict[str, tuple[str, int, int, int, int]] = {}

        def visit(path: Path, relative: str) -> None:
            try:
                item_stat = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            generation = (
                item_stat.st_mtime_ns,
                item_stat.st_ctime_ns,
                item_stat.st_size,
                item_stat.st_ino,
            )
            if stat.S_ISLNK(item_stat.st_mode):
                result[relative] = ("symlink", *generation)
            elif stat.S_ISDIR(item_stat.st_mode):
                result[relative] = ("directory", *generation)
                try:
                    values = list(os.scandir(path))
                except OSError as exc:
                    raise MetadataError(f"cannot list instance directory {path}: {exc}") from exc
                for entry in values:
                    child = f"{relative}/{entry.name}"
                    visit(Path(entry.path), child)
            elif stat.S_ISREG(item_stat.st_mode):
                result[relative] = ("file", *generation)
            else:
                result[relative] = ("special", *generation)

        visit(target, normalized)
        return result

    def _revalidate_batch_item(
        self,
        context: InstanceContext,
        target: Path,
        item: dict[str, Any],
        content: bytes | None,
    ) -> None:
        path = normalize_relative_path(str(item["path"]))
        desired = context.files.get(path)
        if content is None:
            current_status = "remove" if desired is not None else "unchanged"
        elif desired is None:
            current_status = "add"
        else:
            current_status = "unchanged" if content == desired else "change"
        if current_status == "unchanged":
            raise MetadataError(f"delta changed while capture was being planned: {path!r}")
        expected_status = "change" if item["status"] == "update" else item["status"]
        if current_status != expected_status:
            raise MetadataError(f"delta changed while capture was being planned: {path!r}")
        reason = self._same_layer_capture_reason(target, item)
        if reason is not None:
            raise MetadataError(f"cannot capture {path!r} into its owning layer: {reason}")
        if current_status == "add" and any(
            matches(path, pattern) for pattern in context.resolution.state.exclusions
        ):
            raise MetadataError(f"cannot capture {path!r}: inherited remove_paths excludes it")

    def _bounded_target_path(self, target: Path, relative: str) -> Path:
        normalized = normalize_relative_path(relative)
        destination = target.resolve()
        for part in normalized.split("/"):
            destination /= part
            if destination.is_symlink():
                raise MetadataError(f"layer payload path contains a symlink: {relative!r}")
        return destination

    def _payload_target_has_final_precedence(
        self, instance: str, context: InstanceContext, target: Path
    ) -> bool:
        overlay = self.state.feature_root(instance).resolve()
        if target.resolve() == overlay:
            return True
        if not context.resolution.trace:
            return False
        return Path(context.resolution.trace[-1].root).resolve() == target.resolve()

    @staticmethod
    def _batch_atomic_write(path: Path, content: bytes) -> None:
        """Atomic replace for a pre-journaled batch path with no redundant success unlink."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.batch-tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _batch_existing_package(
        self,
        context: InstanceContext,
        target: Path,
        package_id: str,
        destination: str,
        content: bytes,
    ) -> tuple[Path, dict[str, Any]]:
        package = context.resolution.state.packages[package_id]
        digest = hashlib.sha256(content).hexdigest()
        artifact = (
            target
            / METADATA_DIR
            / "artifacts"
            / f"{digest}{self._artifact_suffix(str(package.kind))}"
        )
        operation: dict[str, Any] = {
            "type": "package",
            "id": package.identity,
            "logical_id": package.logical_identity,
            "kind": package.kind,
            "activation": (
                "enabled"
                if package.activation in {Activation.ENABLED, Activation.TRANSITIVE}
                else package.activation.value
            ),
            "destination": destination,
            "source": {
                "provider": "local",
                "location": self._import_artifact_location(target, artifact),
                "sha256": digest,
            },
            "update_policy": package.update_policy.value,
            "automatic_requires": list(package.automatic_requires),
        }
        if package.alias is not None:
            operation["alias"] = package.alias
        if package.version is not None:
            operation["version"] = package.version
        return artifact, operation

    def _batch_local_package(
        self,
        target: Path,
        destination: str,
        content: bytes,
        reserved_ids: set[str],
    ) -> tuple[Path, dict[str, Any]]:
        digest = hashlib.sha256(content).hexdigest()
        canonical_destination = self._canonical_package_destination(destination)
        kind = self._infer_package_kind(canonical_destination)
        artifact = target / METADATA_DIR / "artifacts" / f"{digest}{self._artifact_suffix(kind)}"
        package_id = self._allocate_package_id(canonical_destination, digest, reserved_ids)
        reserved_ids.add(package_id)
        return artifact, {
            "type": "package",
            "id": package_id,
            "kind": kind,
            "activation": "disabled" if canonical_destination != destination else "enabled",
            "destination": canonical_destination,
            "source": {
                "provider": "local",
                "location": self._import_artifact_location(target, artifact),
                "sha256": digest,
            },
            "update_policy": "fixed",
        }

    def _same_layer_capture_reason(self, target: Path, item: dict[str, Any]) -> str | None:
        """Reject raw writes that conflict with a structured producer in the target."""
        marker_path = self._layer_marker_path(target)
        if marker_path is None:
            return None
        schema = (
            2 if marker_path.suffix != ".json"
            else int(JsonMetadataParser().read_object(marker_path).get("schema", 1))
        )
        metadata_paths = self._loader().metadata_files(target)
        package_id = item.get("package_id")
        if package_id is not None:
            for metadata_path in metadata_paths:
                document = self._decode_metadata_document(
                    metadata_path.read_bytes(), metadata_path
                )
                operations = document.get("operations") if isinstance(document, dict) else None
                if not isinstance(operations, list):
                    continue
                if any(
                    isinstance(raw, dict)
                    and raw.get("type") == "package"
                    and raw.get("id") == package_id
                    for raw in operations
                ):
                    # Existing package capture updates the declaration at its
                    # provenance source, regardless of shard name or depth.
                    return None
            return None
        affected = {normalize_relative_path(str(item["path"]))}
        if item.get("to_path"):
            affected.add(normalize_relative_path(str(item["to_path"])))
        for metadata_path in metadata_paths:
            source = metadata_path.relative_to(target).as_posix()
            document = self._decode_metadata_document(
                metadata_path.read_bytes(), metadata_path
            )
            operations = document.get("operations") if isinstance(document, dict) else None
            if not isinstance(operations, list):
                continue
            for raw in operations:
                if not isinstance(raw, dict):
                    continue
                canonical = (
                    raw if metadata_path.suffix == ".mcpack"
                    else decode_record_paths(raw, source, schema)
                )
                operation_type = canonical.get("type")
                operation_path = canonical.get("path")
                if (
                    operation_type in {"put", "remove", "patch_properties"}
                    and isinstance(operation_path, str)
                    and normalize_relative_path(operation_path) in affected
                ):
                    return (
                        f"existing {operation_type} operation in {metadata_path.name} needs "
                        "a provenance-aware edit; raw payload capture would create an "
                        "incompatible same-layer write"
                    )
                pattern = canonical.get("glob")
                if (
                    operation_type == "remove_paths"
                    and isinstance(pattern, str)
                    and any(matches(candidate, pattern) for candidate in affected)
                ):
                    return (
                        f"existing remove_paths operation in {metadata_path.name} excludes "
                        "this path and has no safe same-layer override"
                    )
        return None

    @staticmethod
    def _local_provider_candidate(content: bytes, version: str | None) -> dict[str, object]:
        hashes = file_hashes(content)
        return {
            "provider": "local",
            "project_id": "",
            "version_id": hashes["sha256"],
            "version": version,
            "download_url": "",
            "hashes": hashes,
            "confidence": "exact",
            "reason": "local exact-SHA256 fallback",
        }

    def _run_provider_lookup(self, content: bytes) -> ProviderLookupResult:
        lookup_all = getattr(self.provider_matcher, "lookup_all", None)
        if callable(lookup_all):
            result = lookup_all(content)
            order = {"modrinth": 0, "curseforge": 1}
            return ProviderLookupResult(
                tuple(sorted(result.matches, key=lambda match: order.get(match.provider, 2))),
                result.errors,
            )
        match_all = getattr(self.provider_matcher, "match_all", None)
        if callable(match_all):
            matches = tuple(match_all(content))
        else:
            match = self.provider_matcher.match(content)
            matches = () if match is None else (match,)
        order = {"modrinth": 0, "curseforge": 1}
        return ProviderLookupResult(
            tuple(sorted(matches, key=lambda match: order.get(match.provider, 2)))
        )

    def _schedule_package_prefetch(
        self, instance: str, hints: tuple[_PackagePrefetch, ...]
    ) -> None:
        with self._provider_lock:
            if self._closed:
                return
            pending = tuple(hint for hint in hints if hint not in self._provider_prefetches)
            if not pending:
                return
            self._provider_prefetches.update(pending)
            future = self._provider_prefetch_executor.submit(
                self._prefetch_package_paths, instance, pending
            )
            future.add_done_callback(partial(self._finish_package_prefetch, pending))

    def _finish_package_prefetch(
        self,
        hints: tuple[_PackagePrefetch, ...],
        future: Future[None],
    ) -> None:
        try:
            future.result()
        except Exception:
            pass
        finally:
            with self._provider_lock:
                self._provider_prefetches.difference_update(hints)

    def _prefetch_package_paths(
        self, instance: str, hints: tuple[_PackagePrefetch, ...]
    ) -> None:
        context = self.instance_context(instance)
        root = context.instance.path.resolve()
        for hint in hints:
            if hint.root != root:
                continue
            package = self._package_for_path(context, hint.path)
            self._prefetch_package_path(
                replace(
                    hint,
                    declared=package is not None or hint.declared,
                    kind=str(package.kind) if package is not None else hint.kind,
                )
            )

    def _prefetch_package_path(self, hint: _PackagePrefetch) -> None:
        target = hint.root.joinpath(*hint.path.split("/"))
        try:
            before = target.stat(follow_symlinks=False)
            if ("file", before.st_mtime_ns, before.st_size, before.st_ino) != hint.expected:
                return
            content = target.read_bytes()
            after = target.stat(follow_symlinks=False)
        except OSError:
            return
        if ("file", after.st_mtime_ns, after.st_size, after.st_ino) != hint.expected:
            return
        metadata = self._package_content_metadata(hint.path, content, kind=hint.kind)
        if metadata is None and not hint.declared:
            return
        version = metadata.version if hint.kind == "mod" and metadata is not None else None
        self._provider_enrichment(content, version, allow_remote=metadata is not None)

    def _finish_provider_lookup(
        self, digest: str, future: Future[ProviderLookupResult]
    ) -> None:
        try:
            result = future.result()
        except Exception as error:
            status = "failed"
            result = ProviderLookupResult(errors=(provider_lookup_error("remote", error),))
        else:
            status = "failed" if any(error["retryable"] for error in result.errors) else "ready"
        with self._provider_lock:
            lookup = self._provider_matches.get(digest)
            if lookup is not None and lookup.future is future:
                lookup.status = status
                lookup.matches = result.matches
                lookup.errors = result.errors

    def retry_failed_provider_lookups(self) -> int:
        """Forget completed provider failures so the next read starts a fresh lookup."""
        with self._provider_lock:
            failed = [
                digest
                for digest, lookup in self._provider_matches.items()
                if lookup.status == "failed"
            ]
            for digest in failed:
                del self._provider_matches[digest]
        return len(failed)

    def _provider_enrichment(
        self, content: bytes, version: str | None, *, allow_remote: bool = True
    ) -> tuple[str, list[dict[str, object]]]:
        local = self._local_provider_candidate(content, version)
        if not allow_remote:
            return "ready", [local]
        digest = str(local["hashes"]["sha512"])  # type: ignore[index]
        with self._provider_lock:
            if self._closed:
                raise RuntimeError("ProjectService is closed")
            lookup = self._provider_matches.get(digest)
            if lookup is None:
                lookup = _ProviderLookup()
                self._provider_matches[digest] = lookup
                future = self._provider_executor.submit(self._run_provider_lookup, content)
                lookup.future = future
                future.add_done_callback(partial(self._finish_provider_lookup, digest))
            status = lookup.status
            matches = lookup.matches
        return status, [local, *(match.document() for match in matches)]

    def _set_provider_fields(
        self,
        item: dict[str, Any],
        content: bytes,
        version: str | None,
        *,
        allow_remote: bool = True,
    ) -> None:
        status, candidates = self._provider_enrichment(
            content, version, allow_remote=allow_remote
        )
        item["provider_lookup"] = status
        item["provider_candidates"] = candidates
        local_hashes = candidates[0]["hashes"]
        assert isinstance(local_hashes, dict)
        digest = str(local_hashes["sha512"])
        with self._provider_lock:
            lookup = self._provider_matches.get(digest) if allow_remote else None
            item["provider_errors"] = list(lookup.errors) if lookup is not None else []

    def _provider_candidates(
        self, content: bytes, version: str | None, *, allow_remote: bool = True
    ) -> list[dict[str, object]]:
        """Compatibility wrapper for callers that only consume the candidate list."""
        return self._provider_enrichment(content, version, allow_remote=allow_remote)[1]

    def _enrich_package_item(
        self, context: InstanceContext, item: dict[str, Any], content: bytes
    ) -> None:
        path = normalize_relative_path(str(item["path"]))
        package = self._package_for_path(context, path)
        inferred_kind = self._package_kind_for_path(path)
        metadata = self._package_content_metadata(
            path,
            content,
            kind=str(package.kind) if package is not None else None,
        )
        if package is None and (inferred_kind is None or metadata is None):
            item["kind"] = "file"
            item["capture_modes"] = ["payload"]
            item["enriched"] = True
            return
        package_kind = (
            normalize_package_kind(str(package.kind))
            if package is not None
            else inferred_kind
        )
        if package_kind is None:
            package_kind = self._infer_package_kind(path)
        digest = hashlib.sha256(content).hexdigest()
        item["kind"] = "package"
        item["proposed_package_id"] = self._proposed_package_id(
            context, self._canonical_package_destination(path), digest
        )
        item["proposed_package_kind"] = package_kind
        item["capture_modes"] = ["package"] if package is not None else ["package", "payload"]
        version = metadata.version if package_kind == "mod" and metadata is not None else None
        self._set_provider_fields(item, content, version, allow_remote=metadata is not None)
        if package_kind == "mod" and metadata is not None:
            item["jar_metadata"] = metadata.document()
            item["actual_version"] = metadata.version
        if package is not None:
            item["desired_version"] = package.version
            item["logical_id"] = package.logical_identity
        item["enriched"] = True

    @staticmethod
    def _package_for_path(context: InstanceContext, path: str) -> Any:
        return next(
            (
                package
                for package in context.resolution.state.packages.values()
                if package.destination == path
                and package.activation in {Activation.ENABLED, Activation.TRANSITIVE}
            ),
            None,
        )

    @classmethod
    def _proposed_package_id(
        cls, context: InstanceContext, path: str, digest: str
    ) -> str:
        existing = cls._package_for_path(context, path)
        if existing is not None:
            return str(existing.identity)
        return cls._allocate_package_id(path, digest, set(context.resolution.state.packages))

    @staticmethod
    def _allocate_package_id(path: str, digest: str, reserved_ids: set[str]) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(path).stem).strip("-")
        identity = stem or f"imported-{digest[:12]}"
        if identity in reserved_ids:
            identity = f"{identity}-{digest[:8]}"
        candidate = identity
        suffix = 2
        while candidate in reserved_ids:
            candidate = f"{identity}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _infer_package_kind(path: str) -> str:
        if path.startswith("shaderpacks/"):
            return "shaderpack"
        if path.startswith("resourcepacks/"):
            return "resourcepack"
        if path.startswith("datapacks/"):
            return "datapack"
        return "mod"

    @classmethod
    def _package_kind_for_path(cls, path: str) -> str | None:
        normalized = path.casefold()
        kind = cls._infer_package_kind(normalized)
        if normalized.endswith((".jar", ".jar.disabled")):
            return "mod"
        if kind == "mod":
            return None
        return kind if normalized.endswith(".zip") else None

    @classmethod
    def _package_content_metadata(
        cls, path: str, content: bytes, *, kind: str | None = None
    ) -> Any:
        kind = (
            normalize_package_kind(kind)
            if kind is not None
            else cls._package_kind_for_path(path)
        )
        if kind == "mod":
            return cls._inspect_jar_content(content)
        if kind is None:
            return None
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                archive.infolist()
        except (OSError, zipfile.BadZipFile):
            return None
        return True

    @staticmethod
    def _artifact_suffix(kind: str) -> str:
        return ".jar" if kind == "mod" else ".zip"

    @staticmethod
    def _package_import_operations(
        context: InstanceContext,
        operation: dict[str, Any],
        *,
        source_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the metadata conversion used by both import preview and commit."""
        destination = operation.get("destination")
        removals = []
        for path in (source_path, destination):
            if (
                isinstance(path, str)
                and path in context.resolution.state.content.files
                and path not in removals
            ):
                removals.append(path)
        return [{"type": "remove", "path": path} for path in removals] + [operation]

    @staticmethod
    def _canonical_package_destination(path: str) -> str:
        normalized = normalize_relative_path(path)
        if normalized.casefold().endswith(".jar.disabled"):
            return normalized[: -len(".disabled")]
        return normalized

    @staticmethod
    def _delta_item(
        context: InstanceContext,
        status: str,
        path: str,
        *,
        package_id: str | None = None,
    ) -> dict[str, Any]:
        del context
        item: dict[str, Any] = {
            "id": ProjectService._delta_id(path),
            "kind": "package" if package_id is not None else "file",
            "status": status,
            "path": path,
            "editable": True,
            "capture_modes": [],
        }
        if package_id is not None:
            item["package_id"] = package_id
        return item

    @staticmethod
    def _delta_id(path: str) -> str:
        return hashlib.sha256(path.encode()).hexdigest()[:20]

    def _quick_delta_selection(
        self,
        context: InstanceContext,
        delta_id: str,
        *,
        respect_ignore: bool = True,
        expected_sha256: str | None = None,
    ) -> tuple[dict[str, Any], bytes | None]:
        transport = LocalFilesystemInstance(context.instance.path)
        existing = set(transport.list_files())
        desired = set(context.files)
        preserved = set(disabled_package_aliases(context.resolution.state.packages))
        matches_id = [
            path
            for path in existing | desired
            if path not in preserved and self._delta_id(path) == delta_id
        ]
        if len(matches_id) != 1:
            raise MetadataError(f"instance delta item does not exist: {delta_id!r}")
        path = matches_id[0]
        if respect_ignore and any(
            selector.contains(path) for selector in context.resolution.state.ignore
        ):
            raise MetadataError(f"instance delta item does not exist: {delta_id!r}")
        package = self._package_for_path(context, path)
        content = transport.read(path) if path in existing else None
        digest = hashlib.sha256(content).hexdigest() if content is not None else None
        if expected_sha256 is not None and digest != expected_sha256:
            raise MetadataError(f"instance delta file changed before mutation: {path!r}")
        if path not in existing:
            item = self._delta_item(
                context,
                "remove",
                path,
                package_id=package.identity if package else None,
            )
            item["desired_sha256"] = hashlib.sha256(context.files[path]).hexdigest()
            item["capture_modes"] = ["activation"] if package else ["remove"]
            item["enriched"] = False
            return item, None
        if path not in desired:
            item = self._delta_item(context, "add", path)
            if self._package_kind_for_path(path) is not None:
                item["kind"] = "package"
                item["capture_modes"] = ["package", "payload"]
            else:
                item["capture_modes"] = ["payload"]
            item["enriched"] = False
            return item, content
        if content == context.files[path]:
            raise MetadataError(f"instance delta item does not exist: {delta_id!r}")
        item = self._delta_item(
            context,
            "change",
            path,
            package_id=package.identity if package else None,
        )
        item["desired_sha256"] = hashlib.sha256(context.files[path]).hexdigest()
        item["actual_sha256"] = digest
        item["capture_modes"] = ["package"] if package else ["payload"]
        item["enriched"] = package is None
        if package is not None:
            item["desired_version"] = package.version
            item["logical_id"] = package.logical_identity
        return item, content

    def _cached_delta_selection(
        self,
        context: InstanceContext,
        snapshot: ExplorerSnapshot,
        delta_id: str,
    ) -> tuple[dict[str, Any], bytes | None] | None:
        if (
            not snapshot.context_verified
            or snapshot.scan.root != context.instance.path.resolve()
            or snapshot.context_fingerprint != self._context_fingerprint(context)
            or not self._snapshot_auto_payload_epochs_match(snapshot)
            or not scan_generation_matches(snapshot.scan, ())
        ):
            return None
        matches_id = [
            item for item in snapshot.delta_items if str(item.get("id")) == delta_id
        ]
        if len(matches_id) != 1:
            return None
        selected = dict(matches_id[0])
        try:
            path = normalize_relative_path(str(selected["path"]))
        except (KeyError, MetadataError):
            return None
        matching_paths = [
            candidate
            for candidate in set(snapshot.scan.files) | set(context.files)
            if self._delta_id(candidate) == delta_id
        ]
        if len(matching_paths) != 1 or matching_paths[0] != path:
            return None
        if any(selector.contains(path) for selector in context.resolution.state.ignore):
            raise MetadataError(f"instance delta item does not exist: {delta_id!r}")
        entry = snapshot.entries.get(path)
        if (
            entry is None
            or str(entry.get("delta_id")) != delta_id
            or entry.get("type") != "file"
        ):
            return None
        target = snapshot.scan.root.joinpath(*path.split("/"))
        expected = snapshot.scan.entry_generations.get(path)
        if expected is None:
            if entry.get("actual"):
                return None
            try:
                target.stat(follow_symlinks=False)
            except FileNotFoundError:
                return selected, None
            except OSError:
                return None
            return None
        if expected[0] != "file" or not entry.get("actual"):
            return None
        try:
            before = target.stat(follow_symlinks=False)
            if ("file", before.st_mtime_ns, before.st_size, before.st_ino) != expected:
                return None
            content = target.read_bytes()
            after = target.stat(follow_symlinks=False)
        except OSError:
            return None
        if ("file", after.st_mtime_ns, after.st_size, after.st_ino) != expected:
            return None
        return selected, content

    @staticmethod
    def _inspect_jar_content(content: bytes) -> Any:
        with tempfile.TemporaryDirectory(prefix="mcpack-jar-inspect-") as directory:
            path = Path(directory) / "selected.jar"
            path.write_bytes(content)
            return inspect_jar(path)

    def delta_detail(self, instance: str, delta_id: str) -> dict[str, Any]:
        context = self.instance_context(instance)
        snapshot = self._explorer_snapshots.get(instance)
        cached = (
            self._cached_delta_selection(context, snapshot, delta_id)
            if snapshot is not None
            else None
        )
        selected, content = (
            cached
            if cached is not None
            else self._quick_delta_selection(context, delta_id)
        )
        item = dict(selected)
        if content is None:
            item["enriched"] = True
            return item
        item["actual_sha256"] = hashlib.sha256(content).hexdigest()
        path = normalize_relative_path(str(item["path"]))
        package = self._package_for_path(context, path)
        if package is None and self._package_kind_for_path(path) is None:
            item["enriched"] = True
            return item
        self._enrich_package_item(context, item, content)
        metadata = self._package_content_metadata(
            path, content, kind=str(package.kind) if package is not None else None
        )
        if item["kind"] != "package":
            return item
        if (
            package is not None
            and package.kind == "mod"
            and metadata is not None
            and metadata.version is not None
            and package.version is not None
            and _version_is_newer(metadata.version, package.version)
            and (
                {
                    package.identity,
                    package.logical_identity,
                    *(tuple([package.alias]) if package.alias is not None else ()),
                }
                & set(metadata.mod_ids)
            )
        ):
            item["status"] = "update"
        return item

    def bootstrap(self, name: str, *, include_delta: bool = True) -> dict[str, Any]:
        context = self.instance_context(name)
        registry, _, _ = self.artifact_components()
        if self._pin_provider_selectors(
            context.resolution, registry, include_unselected=True
        ):
            context = self.instance_context(name)
        delta = (
            self.instance_diff(context)
            if include_delta
            else {
                "items": [],
                "summary": {
                    "add": 0,
                    "remove": 0,
                    "change": 0,
                    "update": 0,
                    "rename": 0,
                    "ignored": 0,
                },
                "loaded": False,
                "complete": False,
            }
        )
        declarations: list[dict[str, Any]] = []
        for trace in context.resolution.trace:
            for producer in trace.producers:
                for index, operation in enumerate(producer.operations):
                    data = operation_to_json(
                        operation,
                        file_digest=lambda content: content_digest(
                            content, context.content_digests
                        ),
                    )
                    declarations.append(
                        {
                            "id": hashlib.sha256(
                                f"{trace.layer}\0{producer.source}\0{index}".encode()
                            ).hexdigest()[:20],
                            "kind": data["type"],
                            "layer": trace.layer,
                            "producer": producer.producer,
                            "source": producer.source,
                            "operation_index": index,
                            "editable": producer.source != "<payload>",
                            "data": data,
                            "section_ids": [
                                section.identity
                                for section in context.resolution.state.sections.values()
                                if data.get("id") in section.members
                            ],
                        }
                    )
        metadata_files: list[dict[str, Any]] = []
        by_file: dict[tuple[str, str], dict[str, Any]] = {}
        for declaration in declarations:
            key = (str(declaration["layer"]), str(declaration["source"]))
            entry = by_file.get(key)
            if entry is None:
                entry = {
                    "layer": key[0],
                    "path": key[1],
                    "sections": [],
                    "declarations": [],
                }
                by_file[key] = entry
                metadata_files.append(entry)
            entry["declarations"].append(declaration["id"])
            for section_id in declaration["section_ids"]:
                if section_id not in entry["sections"]:
                    entry["sections"].append(section_id)
        packages = [
            {
                "id": package.identity,
                "logical_id": package.logical_identity,
                "kind": package.kind,
                "version": package.version,
                "activation": (
                    "enabled"
                    if package.activation is Activation.TRANSITIVE
                    else package.activation.value
                ),
                **(
                    {"activation_reason": "dependency"}
                    if package.activation is Activation.TRANSITIVE
                    else {}
                ),
                "destination": package.destination,
                "source": {
                    "provider": package.source.provider,
                    "location": package.source.location,
                    "sha256": package.source.sha256,
                },
                "source_file": package.source_file,
                "editable": package.source_file is not None,
                "unsatisfied_dependencies": sorted(
                    dependency.target
                    for dependency in context.resolution.state.dependencies
                    if dependency.source == package.identity
                    and (
                        (
                            dependency.target in context.resolution.state.packages
                            and context.resolution.state.packages[dependency.target].activation
                            is Activation.DISABLED
                        )
                        or (
                            dependency.target in context.resolution.state.groups
                            and context.resolution.state.groups[dependency.target].activation
                            is Activation.DISABLED
                        )
                    )
                ),
            }
            for package in sorted(
                context.resolution.state.packages.values(), key=lambda item: item.identity
            )
        ]
        writable = [
            {"id": branch, "path": str(root), "local": False, "virtual": False}
            for branch, root in self._writable_layer_roots(context).items()
        ]
        overlay = self.state.feature_root(name)
        writable.append(
            {
                "id": self._layer_reference(overlay),
                "path": str(overlay),
                "local": True,
                "virtual": True,
            }
        )
        return {
            "schema": 1,
            "instance": {"name": name, "path": str(context.instance.path)},
            "link": {
                "branch": context.link.branch,
                "effective_layer": context.link.effective_layer,
                "features": list(context.features),
            },
            "packages": packages,
            "declarations": declarations,
            "metadata_files": metadata_files,
            "groups": [
                {
                    "id": item.identity,
                    "name": item.name,
                    "description": item.description,
                    "activation": item.activation.value,
                }
                for item in sorted(
                    context.resolution.state.groups.values(), key=lambda item: item.identity
                )
            ],
            "sections": [
                {
                    "id": item.identity,
                    "title": item.title,
                    "members": list(item.members),
                    "order": item.order,
                }
                for item in sorted(
                    context.resolution.state.sections.values(), key=lambda item: item.order
                )
            ],
            "delta": delta,
            "writable_layers": writable,
            "feature_candidates": [
                item
                for item in self.branches()
                if item != context.link.branch and item not in context.features
            ],
            "capabilities": {
                "activation": True,
                "features": True,
                "capture": True,
                "package_import": True,
                "files": True,
                "sync": True,
                "shutdown": True,
            },
        }

    def explain(self, instance: str, target: str) -> dict[str, Any]:
        context = self.instance_context(instance)
        state = context.resolution.state
        match_ids: set[str] = set()
        dependency_pair: tuple[str, str] | None = None
        file_path: str | None = None
        package_ids: set[str] = set()
        if target in state.packages:
            kind = "package"
            package = state.packages[target]
            reason = f"package is {package.activation.value} at {package.destination}"
            match_ids.add(target)
        elif target in state.groups:
            kind = "group"
            group = state.groups[target]
            reason = f"group is {group.activation.value}"
            match_ids.add(target)
        elif "->" in target:
            source, destination = (part.strip() for part in target.split("->", 1))
            kind = "dependency"
            reason = f"dependency edge {source} -> {destination}"
            dependency_pair = (source, destination)
        else:
            normalized = normalize_relative_path(target)
            kind = "file"
            present = normalized in context.files
            reason = f"file is {'present' if present else 'absent'} in the desired state"
            file_path = normalized
            package_ids = {
                package.identity
                for package in state.packages.values()
                if package.destination == normalized
            }

        def is_relevant(data: dict[str, Any]) -> bool:
            if data.get("id") in match_ids | package_ids:
                return True
            if data.get("type") == "dependency":
                edge = (str(data.get("source")), str(data.get("target")))
                if dependency_pair is not None:
                    return edge == dependency_pair
                return bool(match_ids & set(edge))
            return bool(
                file_path is not None
                and (
                    data.get("path") == file_path
                    or (isinstance(data.get("glob"), str) and matches(file_path, str(data["glob"])))
                )
            )

        provenance: list[dict[str, Any]] = []
        for trace in context.resolution.trace:
            for producer in trace.producers:
                for index, operation in enumerate(producer.operations):
                    data = operation_to_json(
                        operation,
                        file_digest=lambda content: content_digest(
                            content, context.content_digests
                        ),
                    )
                    if is_relevant(data):
                        provenance.append(
                            {
                                "layer": trace.layer,
                                "root": trace.root,
                                "producer": producer.producer,
                                "source": producer.source,
                                "operation_index": index,
                                "operation": data,
                            }
                        )
        outgoing = [item.target for item in state.dependencies if item.source == target]
        incoming = [item.source for item in state.dependencies if item.target == target]
        return {
            "target": target,
            "kind": kind,
            "reason": reason,
            "provenance": provenance,
            "dependencies": {"incoming": incoming, "outgoing": outgoing},
            "dependency_paths": _dependency_paths(state, target),
        }

    @_metadata_edit
    def set_activation(
        self, instance: str, package_id: str, state: str, target_layer: str | None = None,
        *, move_disabled_mod: bool = False,
    ) -> None:
        try:
            activation = Activation(state)
        except ValueError as exc:
            raise MetadataError(f"invalid activation: {state!r}") from exc
        if activation is Activation.TRANSITIVE:
            raise MetadataError("transitive activation is resolver-owned")
        context = self.instance_context(instance)
        if package_id not in context.resolution.state.packages:
            raise MetadataError(f"package is not declared: {package_id!r}")
        package = context.resolution.state.packages[package_id]
        disabled_mod = (
            self._disabled_mod_move(context.instance.path, package)
            if move_disabled_mod and activation is Activation.DISABLED
            else None
        )
        target = self._editable_target(instance, context, target_layer)
        before: bytes | None
        if (
            package.source_file is not None
            and Path(package.source_file).resolve().is_relative_to(target)
        ):
            path = confined_path(target, Path(package.source_file).relative_to(target).as_posix())
            before = path.read_bytes()
            self._record_edit_paths(path)
            if path.suffix == ".mcpack":
                self._edit_mcpack_declaration_activation(
                    path, package_id, package.source_index, activation
                )
            else:
                self._edit_declaration_activation(
                    path, package_id, package.source_index, activation
                )
        else:
            path = target / METADATA_DIR / "local-edits.json"
            before = path.read_bytes() if path.exists() else None
            self._append_operation(
                target,
                {"type": "activation", "id": package_id, "state": activation.value},
                instance=instance,
            )
        try:
            self.instance_context(instance)
            self._apply_disabled_mod_move(disabled_mod)
        except Exception:
            self._undo_disabled_mod_move(disabled_mod)
            self._restore_file(path, before)
            raise

    @_metadata_edit
    def remove_package(
        self,
        instance: str,
        package_id: str,
        *,
        mode: str = "disable",
        target_layer: str | None = None,
        move_disabled_mod: bool = False,
    ) -> None:
        """Disable a package or remove its declaration when owned by the target layer."""
        if mode not in {"disable", "remove"}:
            raise MetadataError("package removal mode must be 'disable' or 'remove'")
        context = self.instance_context(instance)
        package = context.resolution.state.packages.get(package_id)
        if package is None:
            raise MetadataError(f"package is not declared: {package_id!r}")
        disabled_mod = (
            self._disabled_mod_move(context.instance.path, package)
            if move_disabled_mod and mode == "disable"
            else None
        )
        target = self._editable_target(instance, context, target_layer)
        source = Path(package.source_file) if package.source_file else None
        owns = source is not None and source.resolve().is_relative_to(target.resolve())
        before: bytes | None
        if (
            mode == "disable"
            and owns
            and source is not None
            and source.suffix == ".mcpack"
            and package.source_index is not None
        ):
            source = confined_path(target, source.relative_to(target).as_posix())
            before = source.read_bytes()
            self._record_edit_paths(source)
            try:
                self._edit_mcpack_declaration_activation(
                    source, package_id, package.source_index, Activation.DISABLED
                )
                self.instance_context(instance)
                self._apply_disabled_mod_move(disabled_mod)
            except Exception:
                self._undo_disabled_mod_move(disabled_mod)
                self._restore_file(source, before)
                raise
            return
        if mode == "remove" and owns and package.source_index is not None:
            assert source is not None
            source = confined_path(target, source.relative_to(target).as_posix())
            before = source.read_bytes()
            if source.suffix == ".mcpack":
                try:
                    self._remove_mcpack_declaration(
                        source, package_id, package.source_index
                    )
                    self.instance_context(instance)
                except Exception:
                    self._restore_file(source, before)
                    raise
                return
            try:
                document = dict(self.metadata.decode_object(before, source))
                operations = document.get("operations")
                if not isinstance(operations, list):
                    raise MetadataError(f"invalid operations document: {source}")
                index = package.source_index
                if (
                    index < 0 or index >= len(operations)
                    or not isinstance(operations[index], dict)
                    or operations[index].get("type") != "package"
                    or operations[index].get("id") != package_id
                ):
                    raise MetadataError(f"package declaration moved for {package_id!r}")
                del operations[index]
                atomic_write(
                    source,
                    self.metadata.encode_object(document, destination=source),
                )
                self.instance_context(instance)
            except Exception:
                self._restore_file(source, before)
                raise
        else:
            path = target / METADATA_DIR / "local-edits.json"
            before = path.read_bytes() if path.exists() else None
            self._append_operation(
                target, {"type": "activation", "id": package_id, "state": "disabled"},
                instance=instance,
            )
            try:
                self.instance_context(instance)
                self._apply_disabled_mod_move(disabled_mod)
            except Exception:
                self._undo_disabled_mod_move(disabled_mod)
                self._restore_file(path, before)
                raise

    @_metadata_edit
    def set_group_activation(
        self, instance: str, group_id: str, state: str, target_layer: str | None = None
    ) -> None:
        activation = self._parse_edit_activation(state)
        context = self.instance_context(instance)
        group = context.resolution.state.groups.get(group_id)
        if group is None:
            raise MetadataError(f"group is not declared: {group_id!r}")
        target = self._editable_target(instance, context, target_layer)
        path = target / METADATA_DIR / "local-edits.json"
        before = path.read_bytes() if path.exists() else None
        operation: dict[str, Any] = {
            "type": "group",
            "id": group.identity,
            "activation": activation.value,
        }
        if group.name is not None:
            operation["name"] = group.name
        if group.description is not None:
            operation["description"] = group.description
        self._append_operation(target, operation, instance=instance)
        try:
            self.instance_context(instance)
        except Exception:
            self._restore_file(path, before)
            raise

    @_metadata_edit
    def set_section_activation(
        self, instance: str, section_id: str, state: str, target_layer: str | None = None
    ) -> None:
        activation = self._parse_edit_activation(state)
        context = self.instance_context(instance)
        section = context.resolution.state.sections.get(section_id)
        if section is None:
            raise MetadataError(f"section is not declared: {section_id!r}")
        members = [item for item in section.members if item in context.resolution.state.packages]
        if not members:
            raise MetadataError(f"section {section_id!r} contains no package declarations")
        target = self._editable_target(instance, context, target_layer)
        path = target / METADATA_DIR / "local-edits.json"
        before = path.read_bytes() if path.exists() else None
        self._append_operations(
            target,
            [
                {"type": "activation", "id": package_id, "state": activation.value}
                for package_id in members
            ],
            instance=instance,
        )
        try:
            self.instance_context(instance)
        except Exception:
            self._restore_file(path, before)
            raise

    @_metadata_edit
    def capture_delta(
        self,
        instance: str,
        delta_id: str,
        target_layer: str | None = None,
        *,
        expected_sha256: str | None = None,
        require_expected_sha256: bool = False,
        rename_to_path: str | None = None,
    ) -> None:
        context = self.instance_context(instance)
        item, content = self._quick_delta_selection(
            context,
            delta_id,
            expected_sha256=expected_sha256,
        )
        if require_expected_sha256 and content is not None and expected_sha256 is None:
            raise MetadataError("expected_sha256 is required for an existing delta file")
        if rename_to_path is not None:
            desired_path = normalize_relative_path(rename_to_path)
            existing = set(LocalFilesystemInstance(context.instance.path).list_files())
            if (
                item["status"] != "add"
                or content is None
                or desired_path not in context.files
                or desired_path in existing
                or hashlib.sha256(content).digest()
                != hashlib.sha256(context.files[desired_path]).digest()
                or any(
                    selector.contains(desired_path) for selector in context.resolution.state.ignore
                )
            ):
                raise MetadataError("rename hint does not match the selected delta file")
            source_path = normalize_relative_path(str(item["path"]))
            package = self._package_for_path(context, desired_path)
            item = self._delta_item(
                context,
                "rename",
                source_path,
                package_id=package.identity if package else None,
            )
            item.update(
                {
                    "from_path": source_path,
                    "to_path": desired_path,
                    "desired_sha256": hashlib.sha256(context.files[desired_path]).hexdigest(),
                    "actual_sha256": hashlib.sha256(content).hexdigest(),
                    "capture_modes": ["rename"],
                    "enriched": True,
                }
            )
        target = self._editable_target(instance, context, target_layer)
        path = normalize_relative_path(item["path"])
        package_id = item.get("package_id")
        if package_id is not None and content is not None:
            package = context.resolution.state.packages[str(package_id)]
            if (
                package.kind != "mod"
                and self._package_content_metadata(path, content, kind=str(package.kind)) is None
            ):
                raise MetadataError(f"package capture requires a readable ZIP: {path!r}")
            self._capture_package_artifact(context, target, str(package_id), path, content)
            return
        if item["status"] == "rename" and content is not None:
            desired_path = normalize_relative_path(str(item["to_path"]))
            payload_path = target.joinpath(*path.split("/"))
            payload_before = payload_path.read_bytes() if payload_path.exists() else None
            operations_path = target / METADATA_DIR / "local-edits.json"
            operations_before = operations_path.read_bytes() if operations_path.exists() else None
            self._record_edit_paths(payload_path)
            atomic_write(payload_path, content)
            self._append_operation(
                target, {"type": "remove", "path": desired_path}, instance=instance,
                origin="instance_capture",
            )
            try:
                refreshed = self.instance_context(instance)
                remaining = {
                    entry.get("path")
                    for entry in self.instance_diff(refreshed, enrich_actual=False)["items"]
                }
                if path in remaining or desired_path in remaining:
                    raise MetadataError(f"captured rename did not reconcile {path!r}")
            except Exception:
                self._restore_file(payload_path, payload_before)
                self._restore_file(operations_path, operations_before)
                raise
            return
        if package_id is not None:
            changed_path = target / METADATA_DIR / "local-edits.json"
            before = changed_path.read_bytes() if changed_path.exists() else None
            self._append_operation(
                target, {"type": "activation", "id": package_id, "state": "disabled"},
                instance=instance, origin="instance_capture",
            )
        elif content is not None:
            changed_path = target.joinpath(*path.split("/"))
            before = changed_path.read_bytes() if changed_path.exists() else None
            atomic_write(changed_path, content)
        else:
            changed_path = target / METADATA_DIR / "local-edits.json"
            before = changed_path.read_bytes() if changed_path.exists() else None
            self._append_operation(
                target, {"type": "remove", "path": path}, instance=instance,
                origin="instance_capture",
            )
        try:
            refreshed = self.instance_context(instance)
            if any(
                entry["path"] == path
                for entry in self.instance_diff(refreshed, enrich_actual=False)["items"]
            ):
                raise MetadataError(f"captured change did not reconcile {path!r}")
        except Exception:
            if package_id is None or content is None:
                self._restore_file(changed_path, before)
            raise

    @_metadata_edit
    def import_package(
        self,
        instance: str,
        delta_id: str,
        target_layer: str | None = None,
        provider_match: object = None,
        *,
        expected_sha256: str | None = None,
    ) -> None:
        context = self.instance_context(instance)
        item, content = self._quick_delta_selection(
            context,
            delta_id,
            expected_sha256=expected_sha256,
        )
        item_path = normalize_relative_path(str(item["path"]))
        destination = self._canonical_package_destination(item_path)
        inferred_kind = self._package_kind_for_path(item_path)
        existing_package = self._package_for_path(context, item_path)
        if item["status"] == "remove" or (inferred_kind is None and existing_package is None):
            raise MetadataError("package import requires an existing changed or extra package")
        target = self._editable_target(instance, context, target_layer)
        metadata = (
            self._package_content_metadata(
                item_path,
                content,
                kind=str(existing_package.kind) if existing_package is not None else None,
            )
            if content is not None
            else None
        )
        package_kind = (
            normalize_package_kind(str(existing_package.kind))
            if existing_package is not None
            else inferred_kind
        )
        if (
            content is None
            or package_kind is None
            or (metadata is None and (existing_package is None or package_kind != "mod"))
        ):
            raise MetadataError("package import requires a valid changed or extra package")
        digest = hashlib.sha256(content).hexdigest()
        match = self._selected_provider_match(provider_match, content)
        if match is not None:
            self._validate_provider_kind(match, package_kind)
        artifact: Path | None = None
        artifact_existed = False
        selection: PackageSelection | None = None
        provider_default_destination: str | None = None
        if match is None:
            artifact = (
                target
                / METADATA_DIR
                / "artifacts"
                / f"{digest}{self._artifact_suffix(package_kind)}"
            )
            artifact_existed = artifact.exists()
            source = {
                "provider": "local",
                "location": self._import_artifact_location(target, artifact),
                "sha256": digest,
            }
        else:
            source, selection, provider_default_destination = self._native_provider_match(
                match, package_kind, destination, digest
            )
        package_id = self._proposed_package_id(context, destination, digest)
        version = (
            (selection.selector if selection is not None else None)
            or (metadata.version if package_kind == "mod" and metadata is not None else None)
            or (existing_package.version if existing_package is not None else None)
        )
        operations_path = target / METADATA_DIR / "local-edits.json"
        owns_declaration = (
            existing_package is not None
            and existing_package.source_file is not None
            and Path(existing_package.source_file).resolve().is_relative_to(target.resolve())
        )
        if owns_declaration:
            assert existing_package is not None and existing_package.source_file is not None
            operations_path = confined_path(
                target, Path(existing_package.source_file).relative_to(target).as_posix()
            )
        operations_before = operations_path.read_bytes() if operations_path.exists() else None
        declaration_document = None
        declaration_mcpack: tuple[McpackDocument, Any] | None = None
        if owns_declaration:
            assert existing_package is not None
            if existing_package.source_index is None or existing_package.source_index < 0:
                raise MetadataError(f"package declaration has no source index: {package_id!r}")
            if operations_before is None:
                raise MetadataError(f"cannot locate package declaration {package_id!r}")
            if operations_path.suffix == ".mcpack":
                document = McpackParser().parse(operations_before, operations_path)
                line = next(
                    (
                        line for line in document.lines
                        if line.line_number == existing_package.source_index
                    ),
                    None,
                )
                declaration = line.record if line is not None else None
                if line is None or not isinstance(declaration, dict):
                    raise MetadataError(f"cannot locate package declaration {package_id!r}")
                declaration_mcpack = (document, line)
            else:
                try:
                    declaration_document = dict(
                        self.metadata.decode_object(operations_before, operations_path)
                    )
                    declaration = declaration_document["operations"][existing_package.source_index]
                except (UnicodeError, ValueError, KeyError, IndexError, TypeError) as exc:
                    raise MetadataError(
                        f"cannot locate package declaration {package_id!r}"
                    ) from exc
            if (
                not isinstance(declaration, dict)
                or declaration.get("type") != "package"
                or declaration.get("id") != package_id
            ):
                raise MetadataError(f"package declaration moved for {package_id!r}")
            if declaration_mcpack is not None:
                existing_record = {
                    key: value for key, value in declaration.items() if not key.startswith("_")
                }
                existing_record["source"] = source
                existing_record["update_policy"] = "fixed"
                if version:
                    existing_record["version"] = version
                if (
                    provider_default_destination is not None
                    and existing_record.get("destination") == provider_default_destination
                ):
                    existing_record.pop("destination", None)
                document, line = declaration_mcpack
                declaration_mcpack = (
                    document,
                    replace(line, record=existing_record),
                )
            else:
                assert declaration_document is not None
                declaration["source"] = source
                declaration["update_policy"] = "fixed"
                if version:
                    declaration["version"] = version
        payload_path = target.joinpath(*str(item["path"]).split("/"))
        payload_before: bytes | None = None
        if (
            item["path"] in context.resolution.state.content.files
            and payload_path.is_file()
        ):
            payload_before = payload_path.read_bytes()
        package_operation = {
            "type": "package",
            "id": package_id,
            "kind": package_kind,
            **(
                {
                    "logical_id": existing_package.logical_identity,
                    "automatic_requires": list(existing_package.automatic_requires),
                    **({"alias": existing_package.alias} if existing_package.alias else {}),
                }
                if existing_package is not None
                else {}
            ),
            "activation": "disabled" if destination != item_path else "enabled",
            "source": source,
            "update_policy": "fixed",
            **({"version": version} if version else {}),
        }
        if (
            provider_default_destination is None
            or provider_default_destination != destination
        ):
            package_operation["destination"] = destination
        operations = self._package_import_operations(
            context, package_operation, source_path=item_path
        )
        exact_match_seed = None
        try:
            if artifact is not None:
                atomic_write(artifact, content)
            if payload_before is not None:
                payload_path.unlink()
            if declaration_mcpack is not None:
                document, line = declaration_mcpack
                updated_record = {
                    key: value for key, value in (line.record or {}).items()
                    if not key.startswith("_")
                }
                updated_record.update(
                    {
                        "source": source,
                        "update_policy": "fixed",
                        **({"version": version} if version else {}),
                    }
                )
                if (
                    provider_default_destination is not None
                    and updated_record.get("destination") == provider_default_destination
                ):
                    updated_record.pop("destination", None)
                expression = format_operation(updated_record)
                atomic_write(
                    operations_path,
                    document.replace_expression(line.line_id, line.expression, expression),
                )
            elif declaration_document is not None:
                atomic_write(
                    operations_path,
                    self.metadata.encode_object(
                        declaration_document, destination=operations_path
                    ),
                )
            else:
                self._append_operations(
                    target, operations, instance=instance,
                    operation_contexts={package_id: self._provider_routing_context(match)}
                    if match is not None else None,
                    origin="instance_capture",
                )
            if selection is not None:
                logical_context = self.instance_context(instance, prepare=False)
                package = self._resolved_import_package(
                    logical_context,
                    existing_package.identity if existing_package is not None else package_id,
                    source,
                    selection,
                    destination,
                )
                if package is None:
                    raise MetadataError(
                        f"imported provider package did not resolve: {package_id!r}"
                    )
                exact_match_seed = ArtifactCache(self.state.root / "cache").seed_exact_match(
                    package, selection, content
                )
            refreshed = self.instance_context(instance)
            if any(
                entry["path"] == item["path"]
                for entry in self.instance_diff(refreshed, enrich_actual=False)["items"]
            ):
                raise MetadataError(f"imported package did not reconcile {item['path']!r}")
        except Exception:
            self._restore_file(operations_path, operations_before)
            if payload_before is not None:
                self._restore_file(payload_path, payload_before)
            if artifact is not None and not artifact_existed and artifact.exists():
                artifact.unlink()
            if exact_match_seed is not None:
                ArtifactCache(self.state.root / "cache").restore_exact_match_seed(
                    exact_match_seed
                )
            raise

    def _selected_provider_match(self, value: object, content: bytes) -> ProviderMatch | None:
        if value is None or value == "local" or (
            isinstance(value, dict) and value.get("provider") == "local"
        ):
            return None
        if isinstance(value, str) and value in {"modrinth", "curseforge"}:
            match_provider = getattr(self.provider_matcher, "match_provider", None)
            candidate = (
                match_provider(value, content)
                if callable(match_provider)
                else self.provider_matcher.match(content)
            )
            if candidate is not None and candidate.provider != value:
                candidate = None
            return candidate if candidate is not None and candidate.confidence == "exact" else None
        return confirmed_provider_match(value, content)

    @staticmethod
    def _native_provider_match(
        match: ProviderMatch,
        package_kind: str,
        destination: str,
        digest: str,
    ) -> tuple[dict[str, str], PackageSelection, str | None]:
        location: str | None
        selector: str | None
        if match.provider == "modrinth":
            location = match.project_slug or match.project_id
            selector = match.version
        elif match.provider == "curseforge":
            location = match.project_id or match.project_slug
            selector = match.version_id
        else:
            raise MetadataError(f"unsupported exact package provider: {match.provider!r}")
        if (
            not isinstance(location, str)
            or not location
            or not isinstance(selector, str)
            or not selector
        ):
            raise MetadataError(f"exact {match.provider} match is missing its project or version")
        filename = match.filename
        if filename:
            filename = PurePosixPath(filename.replace("\\", "/")).name
        else:
            url_path = urllib.parse.urlsplit(match.download_url).path
            filename = PurePosixPath(urllib.parse.unquote(url_path)).name
        can_omit_destination = bool(filename)
        filename = filename or PurePosixPath(destination).name
        if not filename or filename in {".", ".."}:
            raise MetadataError(f"exact {match.provider} match is missing its filename")
        directories = {
            "mod": "mods",
            "resourcepack": "resourcepacks",
            "shaderpack": "shaderpacks",
            "datapack": "datapacks",
        }
        default_destination = (
            f"{directories[package_kind]}/{filename}" if can_omit_destination else None
        )
        selection = PackageSelection(
            selector,
            match.download_url,
            filename,
            digest,
            logical_identity=match.project_id,
        )
        return {"provider": match.provider, "location": location}, selection, default_destination

    @staticmethod
    def _resolved_import_package(
        context: InstanceContext,
        proposed_id: str,
        source: dict[str, str],
        selection: PackageSelection,
        destination: str,
    ) -> Any | None:
        packages = context.resolution.state.packages
        direct = packages.get(proposed_id)
        if direct is not None:
            return direct
        return next(
            (
                package for package in packages.values()
                if package.source.provider == source["provider"]
                and package.source.location == source["location"]
                and package.version == selection.selector
                and (not package.destination or package.destination == destination)
            ),
            None,
        )

    def _import_artifact_location(self, target: Path, artifact: Path) -> str:
        marker = self._layer_marker_path(target)
        if marker is not None and marker.suffix != ".json":
            return artifact.relative_to(target).as_posix()
        return artifact.relative_to(self.project).as_posix()

    @staticmethod
    def _validate_provider_kind(match: ProviderMatch, expected_kind: str) -> None:
        if match.kind is None:
            return
        provider_kind = normalize_package_kind(match.kind)
        if (
            provider_kind not in {"mod", "shaderpack", "resourcepack", "datapack"}
            or provider_kind != expected_kind
        ):
            raise MetadataError("provider package kind does not match the selected package")

    @staticmethod
    def _provider_routing_context(match: ProviderMatch) -> dict[str, Any]:
        package: dict[str, Any] = {
            "provider": match.provider, "categories": list(match.categories),
            "tags": list(match.tags),
        }
        if match.kind is not None:
            package["kind"] = match.kind
        return {"package": package}

    def safe_build_output(self, branch: str, output: Path | str | None) -> Path:
        default = (self.project / "build" / branch).resolve()
        candidate = Path(output).resolve() if output is not None else default
        protected = [self.state.root, self.branch_root(branch)]
        protected.extend(item.path for item in self.state.instances().values())
        protected.extend(self.branch_root(item) for item in self.branches())
        if candidate == default:
            if any(_intersects(candidate, item) for item in protected if item != self.project):
                raise MetadataError(f"default build output intersects protected path: {candidate}")
            return candidate
        protected.append(self.project)
        conflict = next((item for item in protected if _intersects(candidate, item)), None)
        if conflict is not None:
            raise MetadataError(f"build output {candidate} intersects protected path {conflict}")
        return candidate

    def _write_overlay(self, instance: str, branch: str, features: list[str]) -> LinkRecord:
        overlay = self.state.feature_root(instance)
        overlay_metadata = overlay / METADATA_DIR
        modern = self._new_overlay_schema(branch) == 2
        marker_path = overlay_metadata / (MARKER_FILE if modern else LEGACY_MARKER_FILE)
        self._record_edit_paths(marker_path, self.state.links_path)
        overlay_metadata.mkdir(parents=True, exist_ok=True)
        parents = [
            str(self.branch_root(branch)),
            *(str(self.branch_root(item)) for item in features),
        ]
        if modern:
            text = f'layer({json.dumps(f"local-{instance}", ensure_ascii=False)})\n'
            text += "".join(
                f"parent({json.dumps(parent, ensure_ascii=False)})\n" for parent in parents
            )
            atomic_write(marker_path, text.encode("utf-8"))
            with self.state.lock():
                links = self.state.links()
                current = links.get(instance)
                if current is None or current.branch != branch:
                    raise MetadataError("instance link changed while editing features")
                links[instance] = LinkRecord(instance, branch, self._layer_reference(overlay))
                self.state.write_links(links)
            return links[instance]
        marker = {
            "schema": (
                self._overlay_marker(overlay).get("schema", 1)
                if marker_path.is_file() else self._new_overlay_schema(branch)
            ),
            "id": f"local-{instance}",
            "parents": parents,
            "features": features,
        }
        # ``features`` is local bookkeeping and is stripped before the ordinary loader sees it.
        atomic_write(
            marker_path,
            (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
        )
        with self.state.lock():
            links = self.state.links()
            current = links.get(instance)
            if current is None or current.branch != branch:
                raise MetadataError("instance link changed while editing features")
            links[instance] = LinkRecord(instance, branch, self._layer_reference(overlay))
            self.state.write_links(links)
        return links[instance]

    def _new_overlay_schema(self, branch: str) -> int:
        marker = self._layer_marker_path(self.branch_root(branch))
        if marker is None or marker.suffix != ".json":
            return 2
        return int(JsonMetadataParser().read_object(marker).get("schema", 1))

    def _change_features(
        self, instance: str, context: InstanceContext, features: list[str]
    ) -> LinkRecord:
        overlay = self.state.feature_root(instance)
        marker_path = self._layer_marker_path(overlay)
        if marker_path is None:
            marker_name = (
                MARKER_FILE
                if self._new_overlay_schema(context.link.branch) == 2
                else LEGACY_MARKER_FILE
            )
            marker_path = overlay / METADATA_DIR / marker_name
        marker_before = marker_path.read_bytes() if marker_path.exists() else None
        previous_link = context.link
        changed = self._write_overlay(instance, context.link.branch, features)
        try:
            self.instance_context(instance, prepare=False)
        except Exception:
            self._restore_file(marker_path, marker_before)
            with self.state.lock():
                links = self.state.links()
                links[instance] = previous_link
                self.state.write_links(links)
            raise
        return changed

    def _ensure_overlay(self, instance: str, context: InstanceContext) -> Path:
        overlay = self.state.feature_root(instance)
        if self._layer_marker_path(overlay) is None:
            self._write_overlay(instance, context.link.branch, list(context.features))
        return overlay

    def _effective_root(self, instance: str, link: LinkRecord) -> Path:
        if link.effective_layer == link.branch:
            return self.branch_root(link.branch)
        overlay = self.state.feature_root(instance).resolve()
        expected = self._layer_reference(overlay)
        if link.effective_layer != expected or not overlay.is_relative_to(
            self.state.root.resolve()
        ):
            raise MetadataError(f"instance {instance!r} has an unsafe effective layer")
        self._validate_overlay(instance, link, overlay)
        return overlay

    def _overlay_features(self, instance: str, link: LinkRecord) -> tuple[str, ...]:
        if link.effective_layer == link.branch:
            return ()
        marker = self._overlay_marker(self.state.feature_root(instance))
        features = marker.get("features", [])
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise MetadataError("virtual layer features must be a string array")
        return tuple(features)

    def _validate_overlay(self, instance: str, link: LinkRecord, overlay: Path) -> None:
        marker = self._overlay_marker(overlay)
        allowed = {"schema", "id", "parents", "features"}
        if set(marker) != allowed or marker.get("schema") not in (1, 2):
            raise MetadataError(f"invalid virtual layer marker for {instance!r}")
        features = marker["features"]
        expected = [str(self.branch_root(link.branch))]
        if not isinstance(features, list):
            raise MetadataError("virtual layer features must be an array")
        expected.extend(str(self.branch_root(str(item))) for item in features)
        if marker["parents"] != expected:
            raise MetadataError("virtual layer parents are not confined project branches")

    def _overlay_marker(self, overlay: Path) -> dict[str, Any]:
        path = self._layer_marker_path(overlay) or overlay / METADATA_DIR / LEGACY_MARKER_FILE
        if path.suffix != ".json":
            document = McpackParser().parse(path.read_bytes(), path)
            identity = None
            parents = []
            for line in document.lines:
                record = line.record
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "layer":
                    identity = record.get("id")
                elif record.get("type") == "parent":
                    value = record.get("path")
                    if isinstance(value, str):
                        parent = Path(value)
                        if not parent.is_absolute():
                            parent = (overlay / parent).resolve()
                        parents.append(str(parent))
            known = {str(self.branch_root(name)): name for name in self.branches()}
            features = [known[parent] for parent in parents[1:] if parent in known]
            return {"schema": 2, "id": identity, "parents": parents, "features": features}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"cannot read virtual layer {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise MetadataError(f"{path}: marker must be an object")
        return value

    def _editable_target(
        self, instance: str, context: InstanceContext, target_layer: str | None
    ) -> Path:
        if target_layer is None:
            return self._ensure_overlay(instance, context)
        overlay = self.state.feature_root(instance).resolve()
        if target_layer == self._layer_reference(overlay):
            return self._ensure_overlay(instance, context)
        allowed = self._writable_layer_roots(context)
        if target_layer not in allowed:
            raise MetadataError(
                f"target layer {target_layer!r} is not writable in this instance composition"
            )
        return allowed[target_layer]

    def _target_path(
        self, instance: str, context: InstanceContext, target_layer: str | None
    ) -> Path:
        """Resolve a writable target without creating a virtual overlay."""
        overlay = self.state.feature_root(instance).resolve()
        if target_layer is None or target_layer == self._layer_reference(overlay):
            return overlay
        allowed = self._writable_layer_roots(context)
        if target_layer not in allowed:
            raise MetadataError(
                f"target layer {target_layer!r} is not writable in this instance composition"
            )
        return allowed[target_layer]

    def _writable_layer_roots(self, context: InstanceContext) -> dict[str, Path]:
        participating = {Path(trace.root).resolve() for trace in context.resolution.trace}
        return {
            branch: root
            for branch in self.branches()
            if (root := self.branch_root(branch)) in participating
        }

    def _append_operation(
        self, layer: Path, operation: dict[str, Any], *, instance: str | None = None,
        origin: str = "user",
    ) -> None:
        self._append_operations(layer, [operation], instance=instance, origin=origin)

    def _append_operations(
        self, layer: Path, additions: list[dict[str, Any]], *, instance: str | None = None,
        operation_contexts: dict[str, dict[str, Any]] | None = None, origin: str = "user",
    ) -> None:
        plan = self.plan_metadata_writes(
            layer, additions, instance=instance,
            operation_contexts=operation_contexts, origin=origin,
        )
        self._apply_metadata_plan(plan, layer)

    @contextmanager
    def metadata_destination(
        self, file: str | None = None, section: str | None = None
    ) -> Iterator[None]:
        """One request's explicit layer-root destination; never moves existing records."""
        if file is not None and (not isinstance(file, str) or not file):
            raise MetadataError("metadata destination must be a non-empty path")
        if section is not None and (not isinstance(section, str) or not section):
            raise MetadataError("section must be a non-empty string")
        token = _edit_destinations.set((*_edit_destinations.get(), (self, file, section)))
        try:
            yield
        finally:
            _edit_destinations.reset(token)

    def routing_options(self, instance: str, target_layer: str | None = None) -> dict[str, Any]:
        context = self.instance_context(instance, prepare=False)
        target = self._target_path(instance, context, target_layer)
        marker = self._layer_marker_path(target)
        exists = marker is not None
        source = target if exists else self._effective_root(instance, context.link)
        layer_rules = self._routing_rules_for_layer(source)
        instance_rules = self._routing_rules_for_instance(instance)
        schema = (
            2 if marker is not None and marker.suffix != ".json"
            else int(JsonMetadataParser().read_object(marker).get("schema", 1))
            if marker is not None
            else self._new_overlay_schema(context.link.branch)
        )
        files = (
            [
                path.relative_to(target).as_posix()
                for path in self._loader().metadata_files(target)
            ]
            if exists
            else []
        )
        own_rule_ids = (
            [
                operation.identity
                for producer in self._loader().load(target).producers
                for operation in producer.operations
                if isinstance(operation, RoutingRule)
            ]
            if exists
            else []
        )
        return {
            "target_layer": self._layer_reference(target),
            "schema": schema,
            "files": files,
            "sections": list(context.resolution.state.sections),
            "layer_rules": [operation_to_json(rule) for rule in layer_rules],
            "instance_rules": [operation_to_json(rule) for rule in instance_rules],
            "own_rule_ids": own_rule_ids,
            "effective_rules": [
                operation_to_json(rule) for rule in merge_routing_rules(layer_rules, instance_rules)
            ],
        }

    def preview_routing(
        self,
        instance: str,
        operation: dict[str, Any],
        target_layer: str | None = None,
        *,
        file: str | None = None,
        section: str | None = None,
        origin: str = "user",
    ) -> dict[str, Any]:
        context = self.instance_context(instance, prepare=False)
        target = self._target_path(instance, context, target_layer)
        additions = (
            self._package_import_operations(context, operation)
            if origin == "instance_capture" and operation.get("type") == "package"
            else [operation]
        )
        plan = self.plan_metadata_writes(
            target, additions, instance=instance, destination=file, section=section,
            origin=origin,
        )
        return {
            "target_layer": self._layer_reference(target),
            "writes": [
                {
                    "file": item.path.relative_to(target).as_posix(),
                    "document": self._decode_metadata_document(item.after, item.path),
                }
                for item in plan.writes
            ],
        }

    def _plan_schema_upgrade(self, target: Path) -> MetadataWritePlan:
        """Preserve schema-one meaning explicitly before enabling local-path defaults."""
        target = target.resolve()
        marker = self._layer_marker_path(target)
        if marker is None:
            raise MetadataError(
                f"layer marker does not exist: {target / METADATA_DIR / MARKER_FILE}"
            )
        if marker.suffix != ".json":
            return MetadataWritePlan((), target)
        marker_parser = JsonMetadataParser()
        document = dict(marker_parser.read_object(marker))
        schema = document.get("schema", 1)
        if schema == 2:
            return MetadataWritePlan((), target)
        if schema != 1:
            raise MetadataError(f"unsupported layer schema: {schema!r}")
        for directory in target.rglob(METADATA_DIR):
            relative = directory.relative_to(target)
            if directory == target / METADATA_DIR or METADATA_DIR in relative.parts[:-1]:
                continue
            if directory.is_symlink() or any(
                path.is_file() or path.is_symlink() for path in directory.rglob("*")
            ):
                raise MetadataError(
                    f"cannot upgrade a layer with existing nested metadata content: {relative}; "
                    "review these files before upgrading"
                )
        writes: list[MetadataWrite] = []
        for path in self._loader().metadata_files(target):
            source = path.relative_to(target).as_posix()
            path = self._metadata_destination(target, source, 1)
            original = path.read_bytes()
            metadata = dict(self.metadata.read_object(path))
            metadata["operations"] = [
                encode_record_paths(raw, source, 2, mode="layer")
                for raw in metadata.get("operations", [])
            ]
            writes.append(
                MetadataWrite(
                    path,
                    original,
                    self.metadata.encode_object(metadata, destination=path),
                )
            )
        document["schema"] = 2
        writes.append(
            MetadataWrite(marker, marker.read_bytes(), marker_parser.encode_object(document))
        )
        return MetadataWritePlan(tuple(writes), target)

    def preview_schema_upgrade(
        self, instance: str, target_layer: str | None = None
    ) -> dict[str, Any]:
        context = self.instance_context(instance, prepare=False)
        target = self._target_path(instance, context, target_layer)
        if self._layer_marker_path(target) is not None:
            plan = self._plan_schema_upgrade(target)
            files = [write.path.relative_to(target).as_posix() for write in plan.writes]
            current = not plan.writes
        else:
            # Previewing a prospective local overlay must not create it.
            current = self._new_overlay_schema(context.link.branch) == 2
            marker = self._layer_marker_path(target)
            files = [] if current else [
                (
                    marker.relative_to(target).as_posix()
                    if marker
                    else f"{METADATA_DIR}/{LEGACY_MARKER_FILE}"
                )
            ]
        return {
            "target_layer": self._layer_reference(target),
            "from_schema": 2 if current else 1,
            "to_schema": 2,
            "files": files,
            "already_current": current,
        }

    @_metadata_edit
    def upgrade_layer_schema(
        self, instance: str, target_layer: str | None = None
    ) -> dict[str, Any]:
        context = self.instance_context(instance, prepare=False)
        target = self._target_path(instance, context, target_layer)
        with self._target_layer_edit_lock(target):
            target = self._editable_target(instance, context, target_layer)
            baseline = self.instance_context(instance, prepare=False).resolution
            rules = self._routing_rules_for_layer(target)
            plan = self._plan_schema_upgrade(target)
            self._apply_metadata_plan(plan, target)
            refreshed = self.instance_context(instance, prepare=False).resolution
            if (
                refreshed.state != baseline.state
                or refreshed.active_packages != baseline.active_packages
                or self._routing_rules_for_layer(target) != rules
            ):
                raise MetadataError(
                    "schema upgrade changed layer semantics; changes were rolled back"
                )
        return self.routing_options(instance, target_layer)

    @_metadata_edit
    def save_routing_rule(
        self,
        instance: str,
        raw: dict[str, Any],
        scope: str,
        target_layer: str | None = None,
        *,
        file: str | None = None,
    ) -> dict[str, Any]:
        if scope not in {"layer", "instance"}:
            raise MetadataError("routing rule scope must be layer or instance")
        context = self.instance_context(instance, prepare=False)
        target = self._target_path(instance, context, target_layer)
        source = normalize_metadata_destination(file or ".pack/routing.json")
        rule = operation_from_json(
            raw, source=source if scope == "layer" else ".pack/instance-routing.json", index=0
        )
        if not isinstance(rule, RoutingRule):
            raise MetadataError("expected a routing_rule record")
        if scope == "instance":
            rules = [
                item
                for item in self.state.routing_rules(instance)
                if item.get("id") != rule.identity
            ]
            self._record_edit_paths(self.state.routing_rules_path(instance))
            self.state.write_routing_rules(instance, [*rules, raw])
        else:
            with self._target_layer_edit_lock(target):
                target = self._editable_target(instance, context, target_layer)
                plan = self.plan_metadata_writes(
                    target, [raw], instance=instance, destination=source
                )
                self._apply_metadata_plan(plan, target)
                self.instance_context(instance, prepare=False)
        return self.routing_options(instance, target_layer)

    @_metadata_edit
    def remove_routing_rule(
        self, instance: str, identity: str, scope: str, target_layer: str | None = None
    ) -> dict[str, Any]:
        if scope not in {"layer", "instance"}:
            raise MetadataError("routing rule scope must be layer or instance")
        context = self.instance_context(instance, prepare=False)
        target = self._target_path(instance, context, target_layer)
        if scope == "instance":
            rules = self.state.routing_rules(instance)
            remaining = [raw for raw in rules if raw.get("id") != identity]
            if len(remaining) == len(rules):
                raise MetadataError("instance routing rule does not exist")
            self._record_edit_paths(self.state.routing_rules_path(instance))
            self.state.write_routing_rules(instance, [*remaining])
        else:
            with self._target_layer_edit_lock(target):
                if self._layer_marker_path(target) is None:
                    raise MetadataError("inherited routing rules cannot be removed")
                definition = self._loader().load(target)
                matches = [
                    (producer.source, index)
                    for producer in definition.producers
                    for index, operation in enumerate(producer.operations)
                    if isinstance(operation, RoutingRule) and operation.identity == identity
                ]
                if len(matches) != 1:
                    raise MetadataError("routing rule is inherited, missing, or ambiguous")
                source, index = matches[0]
                path = confined_path(target, source)
                before = path.read_bytes()
                if path.suffix == ".mcpack":
                    document = McpackParser().parse(before, path)
                    line = next(
                        (
                            item for item in document.lines
                            if isinstance(item.record, dict)
                            and item.record.get("type") == "routing_rule"
                            and item.record.get("id") == identity
                        ),
                        None,
                    )
                    if line is None:
                        raise MetadataError("routing rule moved before removal")
                    physical = document.text.splitlines(keepends=True)
                    del physical[line.line_number - 1]
                    after = "".join(physical).encode("utf-8")
                    plan = MetadataWritePlan((MetadataWrite(path, before, after),), target)
                    self._apply_metadata_plan(plan, target)
                    self.instance_context(instance, prepare=False)
                    return self.routing_options(instance, target_layer)
                metadata_document = dict(self.metadata.read_object(path))
                raw = metadata_document["operations"][index]
                if raw.get("type") != "routing_rule" or raw.get("id") != identity:
                    raise MetadataError("routing rule moved before removal")
                del metadata_document["operations"][index]
                plan = MetadataWritePlan(
                    (
                        MetadataWrite(
                            path,
                            before,
                            self.metadata.encode_object(metadata_document, destination=path),
                        ),
                    ),
                    target,
                )
                self._apply_metadata_plan(plan, target)
                self.instance_context(instance, prepare=False)
        return self.routing_options(instance, target_layer)

    def _record_edit_paths(self, *paths: Path) -> None:
        for journal in _edit_journals.get():
            if journal.service is self:
                for path in paths:
                    journal.record(path)

    def _apply_metadata_plan(self, plan: MetadataWritePlan, layer: Path) -> None:
        plan.validate_paths()
        paths = [write.path for write in plan.writes]
        self._record_edit_paths(*paths)
        with _BeforeImageJournal(self, layer, paths):
            plan.apply()

    def _metadata_destination(self, layer: Path, relative: str, schema: int) -> Path:
        path = confined_path(layer, relative)
        relative_parts = path.relative_to(layer).parts
        directory_parts = relative_parts[:-1]
        extensions = set(self.metadata.extensions) | {".mcpack"}
        if (
            path.suffix not in extensions
            or directory_parts.count(METADATA_DIR) != 1
            or path.name in {MARKER_FILE, LEGACY_MARKER_FILE}
        ):
            supported = ", ".join(sorted(extensions))
            raise MetadataError(
                "destination must be a non-control .pack metadata file "
                f"with a supported extension ({supported})"
            )
        if schema == 1 and path.parent != layer / METADATA_DIR:
            raise MetadataError("nested metadata destinations require layer schema 2")
        return path

    def _plan_mcpack_metadata_writes(
        self,
        layer: Path,
        additions: list[dict[str, Any]],
        *,
        resolved: Resolution,
        loader: WorkspaceLoader,
        rule_source: Path,
        instance: str | None,
        destination: str | None,
        section: str | None,
        operation_contexts: dict[str, dict[str, Any]] | None,
        origin: str,
    ) -> MetadataWritePlan:
        parser = McpackParser()
        originals: dict[Path, bytes | None] = {}
        contents: dict[Path, bytes] = {}
        lines_by_path: dict[Path, dict[tuple[str, str], Any]] = {}
        section_lines: dict[Path, dict[str, Any]] = {}
        for source in loader.metadata_files(layer):
            if source.suffix != ".mcpack":
                continue
            path = confined_path(layer, source.relative_to(layer).as_posix())
            content = path.read_bytes()
            originals[path] = content
            contents[path] = content
            document = parser.parse(content, path)
            records: dict[tuple[str, str], Any] = {}
            sections: dict[str, Any] = {}
            for line in document.lines:
                record = line.record
                if not isinstance(record, dict):
                    continue
                identity = record.get("id")
                kind = record.get("type")
                if kind == "section" and isinstance(identity, str):
                    sections[identity] = line
                elif isinstance(identity, str):
                    records[(str(kind), identity)] = line
            lines_by_path[path] = records
            section_lines[path] = sections

        rules = self._routing_rules_for_layer(rule_source, loader=loader)
        if instance is not None:
            rules = merge_routing_rules(rules, self._routing_rules_for_instance(instance))
        engine = RoutingEngine(
            tuple(replace(rule, section=section) for rule in rules)
            if section is not None else rules
        )
        changed: set[Path] = set()

        def document_for(path: Path) -> McpackDocument:
            if path not in originals:
                originals[path] = path.read_bytes() if path.exists() else None
                contents[path] = originals[path] or b""
                lines_by_path[path] = {}
                section_lines[path] = {}
            return parser.parse(contents[path], path)

        def append_expression(path: Path, expression: str, section_id: str | None) -> None:
            document = document_for(path)
            physical = document.text.splitlines(keepends=True)
            newline = "\r\n" if "\r\n" in document.text else "\n"
            insertion = len(physical)
            section_record = None
            if section_id is not None:
                section_record = resolved.state.sections.get(section_id)
                section_line = section_lines[path].get(section_id)
                if section_line is not None:
                    next_section = next(
                        (
                            line for line in document.lines
                            if line.line_number > section_line.line_number
                            and line.section is not None
                        ),
                        None,
                    )
                    insertion = next_section.line_number - 1 if next_section else len(physical)
                else:
                    if section_record is not None:
                        section_expr = format_section({
                            "type": "section", "id": section_record.identity,
                            "title": section_record.title, "members": list(section_record.members),
                            "order": section_record.order,
                        })
                    else:
                        section_expr = format_section({"type": "section", "id": section_id})
                    if physical and not physical[-1].endswith(("\n", "\r")):
                        physical[-1] += newline
                    physical.extend((section_expr + newline, expression + newline))
                    contents[path] = "".join(physical).encode("utf-8")
                    changed.add(path)
                    refreshed = parser.parse(contents[path], path)
                    section_lines[path][section_id] = next(
                        line for line in refreshed.lines
                        if isinstance(line.record, dict)
                        and line.record.get("type") == "section"
                        and line.record.get("id") == section_id
                    )
                    return
            if insertion == len(physical) and physical and not physical[-1].endswith(("\n", "\r")):
                physical[-1] += newline
            physical.insert(insertion, expression + newline)
            contents[path] = "".join(physical).encode("utf-8")
            changed.add(path)

        for raw in additions:
            operation = deepcopy(raw)
            identity = operation.get("id")
            state = operation.get("state")
            if operation.get("type") == "group" and isinstance(identity, str):
                group_line = next(
                    (records["group", identity] for records in lines_by_path.values()
                     if ("group", identity) in records),
                    None,
                )
                if group_line is not None:
                    path = next(
                        path for path, records in lines_by_path.items()
                        if records.get(("group", identity)) == group_line
                    )
                    document = document_for(path)
                    contents[path] = document.replace_expression(
                        group_line.line_id,
                        group_line.expression,
                        format_operation(operation),
                    )
                    changed.add(path)
                    continue
            if operation.get("type") == "activation" and isinstance(identity, str):
                package_line = next(
                    (records["package", identity] for records in lines_by_path.values()
                     if ("package", identity) in records),
                    None,
                )
                if package_line is not None and state in {"enabled", "available", "disabled"}:
                    path = next(
                        path for path, records in lines_by_path.items()
                        if records.get(("package", identity)) == package_line
                    )
                    document = document_for(path)
                    replacement = self._package_declaration_star(
                        package_line.expression, enabled=state == "enabled"
                    )
                    contents[path] = document.replace_expression(
                        package_line.line_id, package_line.expression, replacement
                    )
                    changed.add(path)
                    for activation_path, records in lines_by_path.items():
                        previous_activation = records.get(("activation", identity))
                        if previous_activation is None:
                            continue
                        activation_document = document_for(activation_path)
                        current_activation = next(
                            (
                                line for line in activation_document.lines
                                if isinstance(line.record, dict)
                                and line.record.get("type") == "activation"
                                and line.record.get("id") == identity
                            ),
                            None,
                        )
                        if current_activation is not None:
                            contents[activation_path] = activation_document.replace_expression(
                                current_activation.line_id,
                                current_activation.expression,
                                "",
                            )
                            changed.add(activation_path)
                    continue
                activation_line = next(
                    (records["activation", identity] for records in lines_by_path.values()
                     if ("activation", identity) in records),
                    None,
                )
                if activation_line is not None and state in {"enabled", "disabled"}:
                    path = next(
                        path for path, records in lines_by_path.items()
                        if records.get(("activation", identity)) == activation_line
                    )
                    document = document_for(path)
                    contents[path] = document.replace_expression(
                        activation_line.line_id,
                        activation_line.expression,
                        format_operation(operation),
                    )
                    changed.add(path)
                    continue
                activation_line = next(
                    (records["activation", identity] for records in lines_by_path.values()
                     if ("activation", identity) in records),
                    None,
                )
                if activation_line is not None:
                    path = next(
                        path for path, records in lines_by_path.items()
                        if records.get(("activation", identity)) == activation_line
                    )
                    document = document_for(path)
                    contents[path] = document.replace_expression(
                        activation_line.line_id,
                        activation_line.expression,
                        format_operation(operation),
                    )
                    changed.add(path)
                    continue

            if operation.get("type") == "package" and isinstance(identity, str):
                package_line = next(
                    (records["package", identity] for records in lines_by_path.values()
                     if ("package", identity) in records),
                    None,
                )
                if package_line is not None:
                    path = next(
                        path for path, records in lines_by_path.items()
                        if records.get(("package", identity)) == package_line
                    )
                    document = document_for(path)
                    existing = {
                        key: value for key, value in (package_line.record or {}).items()
                        if not key.startswith("_")
                    }
                    updated_record = {**existing, **operation}
                    updated_record["activation"] = existing.get("activation", "available")
                    provider_default_destination = (
                        (operation_contexts or {}).get(identity, {})
                        .get("package", {})
                        .get("default_destination")
                    )
                    if updated_record.get("destination") == provider_default_destination:
                        updated_record.pop("destination", None)
                    contents[path] = document.replace_expression(
                        package_line.line_id,
                        package_line.expression,
                        format_operation(updated_record),
                    )
                    changed.add(path)
                    continue

            package = (
                resolved.state.packages.get(str(identity))
                if operation.get("type") in {"package", "activation"}
                else None
            )
            package_context: dict[str, Any] = {}
            if package is not None:
                package_context = {
                    "id": package.identity,
                    "kind": package.kind,
                    "provider": package.source.provider,
                    "activation": package.activation.value,
                    "destination": package.destination,
                }
            routing_context: dict[str, Any] = {"origin": origin, "package": package_context}
            if operation.get("type") in {"package", "activation"}:
                supplied = (operation_contexts or {}).get(str(identity), {})
                package_context.update(supplied.get("package", {}))
                routing_context["origin"] = supplied.get("origin", origin)
                requested_activation = operation.get("activation", operation.get("state"))
                if requested_activation is not None:
                    package_context["activation"] = requested_activation
            selected = None if destination is not None else engine.destination(
                operation, context=routing_context
            )
            relative = destination or (selected[0] if selected else None)
            if relative is None:
                legacy_default = self._default_metadata_destination(
                    operation, 2, origin=routing_context["origin"]
                )
                relative = str(Path(legacy_default).with_suffix(".mcpack")).replace("\\", "/")
            relative = normalize_metadata_destination(relative, default_extension=".mcpack")
            path = self._metadata_destination(layer, relative, 2)
            section_id = section if section is not None else selected[1] if selected else None
            if path.suffix == ".json":
                metadata_document = (
                    dict(self.metadata.read_object(path))
                    if path.exists()
                    else {"operations": []}
                )
                operations = metadata_document.setdefault("operations", [])
                if not isinstance(operations, list):
                    raise MetadataError(f"{path}: operations must be an array")
                operations.append(
                    encode_record_paths(operation, path.relative_to(layer).as_posix(), 2)
                )
                if section_id is not None and isinstance(identity, str):
                    section_records = [
                        record for record in operations
                        if isinstance(record, dict) and record.get("type") == "section"
                        and record.get("id") == section_id
                    ]
                    if len(section_records) > 1:
                        raise MetadataError(f"{path}: duplicate section {section_id!r}")
                    if section_records:
                        section_record = section_records[0]
                    else:
                        inherited = resolved.state.sections.get(section_id)
                        section_record = {
                            "type": "section", "id": section_id,
                            "title": inherited.title if inherited else section_id,
                            "members": list(inherited.members) if inherited else [],
                            "order": inherited.order if inherited else 0,
                        }
                        operations.append(section_record)
                    members = section_record.setdefault("members", [])
                    if identity not in members:
                        members.append(identity)
                originals.setdefault(path, path.read_bytes() if path.exists() else None)
                contents[path] = self.metadata.encode_object(
                    metadata_document, destination=path
                )
                changed.add(path)
                continue
            activation = operation.get("activation")
            if operation.get("type") == "package" and activation == "disabled":
                # New declarations are available stars; disable() only overrides inheritance.
                declaration = {**operation, "activation": "available"}
                append_expression(path, format_operation(declaration), section_id)
            else:
                append_expression(path, format_operation(operation), section_id)
        return MetadataWritePlan(
            tuple(
                MetadataWrite(path, originals[path], contents[path])
                for path in sorted(changed)
            ),
            layer,
        )

    def _decode_metadata_document(self, content: bytes, path: Path) -> dict[str, Any]:
        if path.suffix != ".mcpack":
            return dict(self.metadata.decode_object(content, path))
        document = McpackParser().parse(content, path)
        operations = [
            {
                key: value
                for key, value in line.record.items()
                if not key.startswith("_")
            }
            for line in document.lines
            if isinstance(line.record, dict)
        ]
        return {"operations": operations, "text": document.text}

    @staticmethod
    def _package_declaration_star(expression: str, *, enabled: bool) -> str:
        match = re.search(r"\b(?:modrinth|curseforge|local|direct_url)\s*\(", expression)
        if match is None:
            raise MetadataError("package declaration no longer contains a provider call")
        prefix = expression[:match.start()]
        star = prefix.rfind("*")
        if enabled:
            return expression[:star] + expression[star + 1:] if star >= 0 else expression
        return (
            expression
            if star >= 0
            else expression[: match.start()] + "*" + expression[match.start() :]
        )

    @staticmethod
    def _default_metadata_destination(
        operation: dict[str, Any], schema: int, *, origin: str = "user"
    ) -> str:
        if schema == 1:
            return f"{METADATA_DIR}/local-edits.json"
        kind = operation.get("type")
        if kind == "ignore":
            return f"{METADATA_DIR}/ignore.json"
        if kind in {"remove", "remove_paths"}:
            subject = operation.get("path", operation.get("glob", ""))
            parent = Path(str(subject)).parent.as_posix()
            return f"{parent}/{METADATA_DIR}/remove.json" if parent != "." else ".pack/remove.json"
        if kind == "activation":
            return "mods/.pack/disabled.json"
        if kind == "package":
            folder = {
                "mod": "mods",
                "shaderpack": "shaderpacks",
                "resourcepack": "resourcepacks",
                "datapack": "datapacks",
            }.get(str(operation.get("kind", "mod")), "mods")
            filename = "mods" if folder == "mods" else "packs"
            if origin == "automatic_dependency":
                filename = "dependencies"
            elif operation.get("activation", "available") in {"available", "disabled"}:
                filename = "disabled"
            return f"{folder}/.pack/{filename}.json"
        return f"{METADATA_DIR}/local-edits.json"

    def plan_metadata_writes(
        self,
        layer: Path,
        additions: list[dict[str, Any]],
        *,
        instance: str | None = None,
        destination: str | None = None,
        section: str | None = None,
        operation_contexts: dict[str, dict[str, Any]] | None = None,
        origin: str = "user",
    ) -> MetadataWritePlan:
        """Preview exact changes without writing; existing records retain their source."""
        layer = layer.resolve()
        for owner, chosen_file, chosen_section in reversed(_edit_destinations.get()):
            if owner is self:
                destination = destination if destination is not None else chosen_file
                section = section if section is not None else chosen_section
                break
        if section is not None and not any(
            operation.get("type") in {"package", "activation", "group"}
            and isinstance(operation.get("id"), str)
            for operation in additions
        ):
            raise MetadataError(
                "manual section requires package, activation, or group records with ids"
            )
        cache = self._contexts.get(instance) if instance is not None else None
        loader = cache.loader if cache is not None else self._loader()
        marker = self._layer_marker_path(layer)
        virtual = marker is None
        if virtual:
            if instance is None or layer != self.state.feature_root(instance).resolve():
                raise MetadataError(f"layer marker does not exist: {marker}")
            instance_context = self.instance_context(instance, prepare=False)
            resolved = instance_context.resolution
            rule_source = self._effective_root(instance, instance_context.link)
            metadata_files: tuple[Path, ...] = ()
            schema = self._new_overlay_schema(instance_context.link.branch)
        else:
            assert marker is not None
            definition = loader.load(layer)
            schema = (
                2 if marker.suffix != ".json"
                else int(JsonMetadataParser().read_object(marker).get("schema", 1))
            )
            metadata_files = loader.metadata_files(layer)
            rule_source = layer
            resolved = LayerComposer(loader).resolve(definition)
        mcpack_layer = marker is not None and marker.suffix != ".json"
        if (
            not mcpack_layer
            and instance is not None
            and layer == self.state.feature_root(instance).resolve()
            and self._new_overlay_schema(self.state.links()[instance].branch) == 2
        ):
            mcpack_layer = True
        if mcpack_layer:
            if destination is not None:
                destination = normalize_metadata_destination(
                    destination, default_extension=".mcpack"
                )
            return self._plan_mcpack_metadata_writes(
                layer,
                additions,
                resolved=resolved,
                loader=loader,
                rule_source=rule_source,
                instance=instance,
                destination=destination,
                section=section,
                operation_contexts=operation_contexts,
                origin=origin,
            )
        if destination is not None:
            destination = normalize_metadata_destination(destination)
        documents: dict[Path, dict[str, Any]] = {}
        originals: dict[Path, bytes | None] = {}
        for source in metadata_files:
            path = self._metadata_destination(layer, source.relative_to(layer).as_posix(), schema)
            originals[path] = path.read_bytes()
            documents[path] = dict(self.metadata.read_object(path))
        local: dict[tuple[str, str], tuple[Path, dict[str, Any]]] = {}
        for path, document in documents.items():
            for record in document.get("operations", []):
                if isinstance(record, dict) and isinstance(record.get("id"), str):
                    key = (str(record.get("type")), record["id"])
                    if key in local:
                        raise MetadataError(f"duplicate local declaration: {key}")
                    local[key] = (path, record)
        rules = self._routing_rules_for_layer(rule_source, loader=loader)
        if instance is not None:
            rules = merge_routing_rules(rules, self._routing_rules_for_instance(instance))
        engine = RoutingEngine(
            tuple(replace(rule, section=section) for rule in rules)
            if section is not None
            else rules
        )
        # Resolve only logical metadata; no artifact downloads or materialization.
        changed: set[Path] = set()
        for operation in additions:
            operation = deepcopy(operation)
            identity = operation.get("id")
            key = (str(operation.get("type")), str(identity))
            existing = local.get(key)
            if operation.get("type") == "activation":
                existing = local.get(("package", str(identity))) or existing
            if existing is not None:
                path, record = existing
                if operation.get("type") == "activation" and record.get("type") == "package":
                    record["activation"] = operation["state"]
                else:
                    source_reference = path.relative_to(layer).as_posix()
                    mode = record.get("path_mode", "local")
                    canonical = decode_record_paths(record, source_reference, schema)
                    canonical.update(operation)
                    encoded = encode_record_paths(canonical, source_reference, schema, mode=mode)
                    if "path_mode" in record:
                        encoded["path_mode"] = mode
                    record.clear()
                    record.update(encoded)
                changed.add(path)
                continue
            package = (
                resolved.state.packages.get(str(identity))
                if operation.get("type") in {"package", "activation"}
                else None
            )
            package_context: dict[str, Any] = {}
            if package is not None:
                package_context = {
                    "id": package.identity,
                    "kind": package.kind,
                    "provider": package.source.provider,
                    "activation": package.activation.value,
                    "destination": package.destination,
                }
            routing_context: dict[str, Any] = {"origin": origin, "package": package_context}
            if operation.get("type") in {"package", "activation"}:
                supplied = (operation_contexts or {}).get(str(identity), {})
                package_context.update(supplied.get("package", {}))
                routing_context["origin"] = supplied.get("origin", origin)
                requested_activation = operation.get("activation", operation.get("state"))
                if requested_activation is not None:
                    package_context["activation"] = requested_activation
            # A manual destination is a complete one-shot choice; conflicting
            # automatic candidates must not prevent the user's explicit choice.
            selected = (
                None
                if destination is not None
                else engine.destination(operation, context=routing_context)
            )
            relative = destination or (
                selected[0]
                if selected
                else self._default_metadata_destination(
                    operation, schema, origin=routing_context["origin"]
                )
            )
            section_id = section if section is not None else selected[1] if selected else None
            path = self._metadata_destination(layer, relative, schema)
            if path not in documents:
                originals[path] = path.read_bytes() if path.exists() else None
                documents[path] = (
                    dict(self.metadata.read_object(path))
                    if path.exists()
                    else {"operations": []}
                )
            encoded = encode_record_paths(operation, path.relative_to(layer).as_posix(), schema)
            documents[path].setdefault("operations", []).append(encoded)
            changed.add(path)
            if isinstance(identity, str):
                local[key] = (path, encoded)
            if (
                section_id is None
                or not isinstance(identity, str)
                or operation.get("type") == "section"
            ):
                continue
            section_record = local.get(("section", section_id))
            if section_record is None:
                inherited = resolved.state.sections.get(section_id)
                record = {
                    "type": "section",
                    "id": section_id,
                    "title": inherited.title if inherited else section_id,
                    "members": list(inherited.members) if inherited else [],
                    "order": inherited.order if inherited else 0,
                }
                documents[path]["operations"].append(record)
                local[("section", section_id)] = (path, record)
                section_path = path
            else:
                section_path, record = section_record
            members = record.setdefault("members", [])
            if identity not in members:
                members.append(identity)
                changed.add(section_path)
        return MetadataWritePlan(
            tuple(
                MetadataWrite(
                    path,
                    originals[path],
                    self.metadata.encode_object(documents[path], destination=path),
                )
                for path in sorted(changed)
            ),
            layer,
        )

    def _routing_rules_for_layer(
        self, layer: Path, *, loader: WorkspaceLoader | None = None
    ) -> tuple[RoutingRule, ...]:
        """Replay the loader's parent graph, retaining ordered ancestor applications."""
        loader = loader or self._loader()

        def read_rules(definition: Any, stack: tuple[str, ...]) -> tuple[RoutingRule, ...]:
            if definition.root in stack:
                raise ResolutionError(f"layer cycle while resolving routing: {definition.root}")
            result: tuple[RoutingRule, ...] = ()
            for reference in definition.parents:
                parent = loader.resolve_parent(definition, reference)
                result = merge_routing_rules(result, read_rules(parent, (*stack, definition.root)))
            own = tuple(
                operation
                for producer in definition.producers
                for operation in producer.operations
                if isinstance(operation, RoutingRule)
            )
            return merge_routing_rules(result, own)

        return read_rules(loader.load(layer), ())

    def _routing_rules_for_instance(self, instance: str) -> tuple[RoutingRule, ...]:
        """Parse untracked instance-local rules; these override layer rules."""
        rules: list[RoutingRule] = []
        source = ".pack/instance-routing.json"
        for index, raw in enumerate(self.state.routing_rules(instance)):
            parsed = operation_from_json(
                {**raw, "type": "routing_rule"}, source=source, index=index
            )
            if isinstance(parsed, RoutingRule):
                rules.append(parsed)
        return merge_routing_rules(rules)

    @staticmethod
    def _parse_edit_activation(value: str) -> Activation:
        try:
            activation = Activation(value)
        except ValueError as exc:
            raise MetadataError(f"invalid activation: {value!r}") from exc
        if activation is Activation.TRANSITIVE:
            raise MetadataError("transitive activation is resolver-owned")
        return activation

    def _capture_package_artifact(
        self,
        context: InstanceContext,
        target: Path,
        package_id: str,
        destination: str,
        content: bytes,
    ) -> None:
        package = context.resolution.state.packages[package_id]
        digest = hashlib.sha256(content).hexdigest()
        artifact = (
            target
            / METADATA_DIR
            / "artifacts"
            / f"{digest}{self._artifact_suffix(str(package.kind))}"
        )
        artifact_existed = artifact.exists()
        self._record_edit_paths(artifact)
        atomic_write(artifact, content)
        relative = artifact.relative_to(self.project).as_posix()
        operation = {
            "type": "package",
            "id": package.identity,
            "logical_id": package.logical_identity,
            "kind": package.kind,
            "activation": (
                "enabled"
                if package.activation in {Activation.ENABLED, Activation.TRANSITIVE}
                else package.activation.value
            ),
            "destination": destination,
            "source": {"provider": "local", "location": relative, "sha256": digest},
            "update_policy": package.update_policy.value,
            "automatic_requires": list(package.automatic_requires),
        }
        if package.alias is not None:
            operation["alias"] = package.alias
        if package.version is not None:
            operation["version"] = package.version
        operations_path = target / METADATA_DIR / "local-edits.json"
        before = operations_path.read_bytes() if operations_path.exists() else None
        self._append_operation(
            target, operation, instance=context.instance.name, origin="instance_capture"
        )
        try:
            refreshed = self.instance_context(context.instance.name)
            if any(
                entry["path"] == destination
                for entry in self.instance_diff(refreshed, enrich_actual=False)["items"]
            ):
                raise MetadataError(f"captured package did not reconcile {destination!r}")
        except Exception:
            self._restore_file(operations_path, before)
            if not artifact_existed:
                artifact.unlink(missing_ok=True)
            raise

    def _edit_declaration_activation(
        self,
        path: Path,
        package_id: str,
        source_index: int | None,
        activation: Activation,
    ) -> None:
        if source_index is None:
            raise MetadataError(f"package {package_id!r} has no editable source index")
        try:
            document = dict(self.metadata.read_object(path))
            operation = document["operations"][source_index]
        except (
            MetadataError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise MetadataError(f"cannot locate package declaration {package_id!r}") from exc
        if operation.get("type") != "package" or operation.get("id") != package_id:
            raise MetadataError(f"package declaration moved for {package_id!r}")
        operation["activation"] = activation.value
        atomic_write(path, self.metadata.encode_object(document, destination=path))

    def _edit_mcpack_declaration_activation(
        self,
        path: Path,
        package_id: str,
        source_index: int | None,
        activation: Activation,
    ) -> None:
        if source_index is None:
            raise MetadataError(f"package {package_id!r} has no editable source line")
        parser = McpackParser()
        before = path.read_bytes()
        document = parser.parse(before, path)
        line = next((item for item in document.lines if item.line_number == source_index), None)
        if (
            line is None
            or not isinstance(line.record, dict)
            or line.record.get("type") != "package"
            or line.record.get("id") != package_id
        ):
            raise MetadataError(f"package declaration moved for {package_id!r}")
        replacement = self._package_declaration_star(
            line.expression, enabled=activation is Activation.ENABLED
        )
        atomic_write(
            path,
            document.replace_expression(line.line_id, line.expression, replacement),
        )

    @staticmethod
    def _remove_mcpack_declaration(
        path: Path, package_id: str, source_index: int
    ) -> None:
        document = McpackParser().parse(path.read_bytes(), path)
        line = next((item for item in document.lines if item.line_number == source_index), None)
        if (
            line is None
            or not isinstance(line.record, dict)
            or line.record.get("type") != "package"
            or line.record.get("id") != package_id
        ):
            raise MetadataError(f"package declaration moved for {package_id!r}")
        physical = document.text.splitlines(keepends=True)
        del physical[line.line_number - 1]
        atomic_write(path, "".join(physical).encode("utf-8"))

    @staticmethod
    def _restore_file(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, content)

    @staticmethod
    def _disabled_mod_move(
        instance_path: Path, package: Any
    ) -> tuple[Path, Path] | None:
        if package.kind != "mod" or not package.destination.casefold().endswith(".jar"):
            return None
        source = confined_path(instance_path, package.destination)
        destination = Path(f"{source}.disabled")
        if source.exists() and destination.exists():
            raise MetadataError(
                f"cannot disable {package.identity!r}: both {package.destination!r} "
                f"and {package.destination + '.disabled'!r} exist in the instance"
            )
        return (source, destination) if source.is_file() else None

    @staticmethod
    def _apply_disabled_mod_move(move: tuple[Path, Path] | None) -> None:
        if move is None:
            return
        source, destination = move
        if destination.exists():
            raise MetadataError(f"cannot disable mod: destination already exists: {destination}")
        if source.is_file():
            source.rename(destination)

    @staticmethod
    def _undo_disabled_mod_move(move: tuple[Path, Path] | None) -> None:
        if move is None:
            return
        source, destination = move
        if destination.is_file() and not source.exists():
            destination.rename(source)

    def _layer_reference(self, path: Path) -> str:
        return path.resolve().relative_to(self.project).as_posix()

    @staticmethod
    def _layer_marker_path(root: Path) -> Path | None:
        modern = root / METADATA_DIR / MARKER_FILE
        legacy = root / METADATA_DIR / LEGACY_MARKER_FILE
        if modern.is_file():
            return modern
        if legacy.is_file():
            return legacy
        return None

    def _rewrite_overlay_identity(self, instance: str, overlay: Path) -> None:
        path = self._layer_marker_path(overlay)
        if path is not None and path.suffix != ".json":
            document = McpackParser().parse(path.read_bytes(), path)
            line = next(
                (
                    item for item in document.lines
                    if isinstance(item.record, dict) and item.record.get("type") == "layer"
                ),
                None,
            )
            if line is None:
                raise MetadataError(f"virtual layer marker has no layer() declaration: {path}")
            expression = format_operation({"type": "layer", "id": f"local-{instance}"})
            atomic_write(
                path,
                document.replace_expression(line.line_id, line.expression, expression),
            )
            return
        marker = self._overlay_marker(overlay)
        marker["id"] = f"local-{instance}"
        atomic_write(
            overlay / METADATA_DIR / LEGACY_MARKER_FILE,
            (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
        )

    def _renamed_overlay_sources(self, old: Path, new: Path) -> dict[Path, bytes]:
        updates = {}
        for path in self._loader().metadata_files(old):
            document = dict(self.metadata.read_object(path))
            operations = document.get("operations", [])
            if not isinstance(operations, list):
                raise MetadataError(f"{path}: operations must be an array")
            changed = False
            for operation in operations:
                if not isinstance(operation, dict) or operation.get("type") != "package":
                    continue
                source = operation.get("source")
                if not isinstance(source, dict) or source.get("provider") != "local":
                    continue
                location = source.get("location")
                if not isinstance(location, str):
                    continue
                original = Path(location)
                source_path = self.project / original
                absolute = Path(os.path.abspath(source_path))
                if not absolute.is_relative_to(old):
                    continue
                resolved = source_path.resolve()
                if not resolved.is_relative_to(old):
                    continue
                renamed = new / resolved.relative_to(old)
                source["location"] = (
                    str(renamed) if original.is_absolute() else self._layer_reference(renamed)
                )
                changed = True
            if changed:
                updates[path] = self.metadata.encode_object(document, destination=path)
        return updates

    def _require_web_stopped(self, instance: str) -> None:
        from .web_process import web_is_alive

        if web_is_alive(self.state, instance):
            raise MetadataError(f"stop the web server for {instance!r} before changing it")

    def _validate_instance_path(self, path: Path) -> None:
        protected = [
            self.project,
            self.state.root.resolve(),
            (self.project / "build").resolve(),
            *(self.branch_root(branch) for branch in self.branches()),
        ]
        conflict = next((item for item in protected if _intersects(path, item)), None)
        if conflict is not None:
            raise MetadataError(
                f"instance path {path} intersects protected project path {conflict}"
            )

    @staticmethod
    def _rename_optional(source: Path, destination: Path) -> None:
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)

    @staticmethod
    def _remove_local_tree(root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()


class _BeforeImageJournal:
    """Request-scoped before-images for a declared, bounded write set."""

    def __init__(
        self,
        service: ProjectService,
        target: Path,
        paths: list[Path] | tuple[Path, ...] = (),
    ):
        self.service = service
        self.target = target.resolve()
        self.paths = tuple(Path(os.path.abspath(path)) for path in paths)
        self.before: dict[Path, tuple[str, bytes | str | None]] = {}
        self.absent_directories: set[Path] = set()
        self.target_existed = self.target.exists()

    def __enter__(self) -> _BeforeImageJournal:
        for path in self.paths:
            self.record(path)
        return self

    def record(self, path: Path) -> None:
        normalized = Path(os.path.abspath(path))
        if normalized in self.before:
            return
        if not self.target_existed and normalized.is_relative_to(self.target):
            self.before[normalized] = ("absent", None)
            ancestor = normalized.parent
            while not ancestor.exists():
                self.absent_directories.add(ancestor)
                ancestor = ancestor.parent
            return
        try:
            mode = os.lstat(normalized).st_mode
        except FileNotFoundError:
            self.before[normalized] = ("absent", None)
            ancestor = normalized.parent
            while not ancestor.exists():
                self.absent_directories.add(ancestor)
                ancestor = ancestor.parent
        else:
            if stat.S_ISLNK(mode):
                self.before[normalized] = ("symlink", os.readlink(normalized))
            elif stat.S_ISREG(mode):
                self.before[normalized] = ("file", normalized.read_bytes())
            else:
                raise MetadataError(f"journal write target is not a file: {normalized}")

    def __exit__(self, error_type: object, error: object, traceback: object) -> None:
        del error, traceback
        if error_type is None:
            return
        for path, (kind, value) in reversed(tuple(self.before.items())):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                if next(path.iterdir(), None) is not None:
                    raise MetadataError(f"cannot roll back non-empty write target: {path}")
                path.rmdir()
            if kind == "file":
                if not isinstance(value, bytes):
                    raise RuntimeError("invalid file before-image")
                atomic_write(path, value)
            elif kind == "symlink":
                if not isinstance(value, str):
                    raise RuntimeError("invalid symlink before-image")
                path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(value, path)
        for directory in sorted(
            self.absent_directories, key=lambda item: len(item.parts), reverse=True
        ):
            if directory.is_dir() and next(directory.iterdir(), None) is None:
                directory.rmdir()


def _intersects(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _version_is_newer(actual: str, desired: str) -> bool:
    actual_numbers = tuple(int(item) for item in re.findall(r"\d+", actual))
    desired_numbers = tuple(int(item) for item in re.findall(r"\d+", desired))
    return bool(actual_numbers and desired_numbers and actual_numbers > desired_numbers)


def _dependency_paths(state: PackState, target: str) -> list[list[str]]:
    roots = {
        item.identity for item in state.packages.values() if item.activation is Activation.ENABLED
    }
    roots.update(
        item.identity for item in state.groups.values() if item.activation is Activation.ENABLED
    )
    adjacency: dict[str, list[str]] = {}
    for dependency in state.dependencies:
        adjacency.setdefault(dependency.source, []).append(dependency.target)
    paths: list[list[str]] = []
    queue = [[root] for root in sorted(roots)]
    while queue:
        path = queue.pop(0)
        node = path[-1]
        if node == target:
            paths.append(path)
            continue
        for child in sorted(adjacency.get(node, [])):
            if child not in path:
                queue.append([*path, child])
    return paths
