"""High-level project, instance, feature, diff, and bounded-edit services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import (
    ArtifactCache,
    ArtifactRegistry,
    ArtifactResolver,
    DirectUrlArtifactProvider,
    LocalArtifactProvider,
)
from .build import Builder
from .errors import MetadataError
from .explorer import ExplorerSnapshot, build_snapshot, scan_generation_matches, scan_instance
from .loader import MARKER_FILE, METADATA_DIR, WorkspaceLoader
from .local_state import InstanceRecord, LinkRecord, LocalState, validate_name
from .model import Activation, IgnoreSelector, PackState, Resolution, SelectorKind
from .operations import AddIgnoreSelector, operation_from_json, operation_to_json
from .package_inspect import (
    ModrinthMatcher,
    ProviderMatcher,
    confirmed_provider_match,
    file_hashes,
    inspect_jar,
)
from .paths import atomic_write, matches, normalize_relative_path
from .resolver import Resolver
from .transport import LocalFilesystemInstance


@dataclass(frozen=True)
class InstanceContext:
    instance: InstanceRecord
    link: LinkRecord
    features: tuple[str, ...]
    parameters: dict[str, Any]
    resolution: Resolution
    files: dict[str, bytes]


class ProjectService:
    def __init__(
        self,
        project: Path | str | None = None,
        *,
        provider_matcher: ProviderMatcher | None = None,
    ):
        self.project = Path(project or Path.cwd()).resolve()
        self.state = LocalState(self.project)
        self.provider_matcher = provider_matcher or ModrinthMatcher()
        self._provider_matches: dict[str, list[dict[str, object]]] = {}
        self._explorer_snapshots: dict[str, ExplorerSnapshot] = {}

    def init_layer(self, layer: str) -> Path:
        validate_name(layer, "layer name")
        root = self.project / layer
        if root.exists():
            if not root.is_dir():
                raise MetadataError(f"layer path is not a directory: {root}")
            if any(root.iterdir()):
                raise MetadataError(f"layer directory is not empty: {layer!r}")
        marker = root / METADATA_DIR / MARKER_FILE
        document = {"schema": 1, "id": layer, "parents": []}
        atomic_write(
            marker,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        return root

    def branch_root(self, branch: str) -> Path:
        validate_name(branch, "branch name")
        root = (self.project / branch).resolve()
        if root.parent != self.project or not (root / METADATA_DIR / MARKER_FILE).is_file():
            raise MetadataError(f"branch does not exist at project root: {branch!r}")
        return root

    def branches(self) -> tuple[str, ...]:
        return tuple(
            child.name
            for child in sorted(self.project.iterdir(), key=lambda item: item.name)
            if child.is_dir()
            and child.name not in {"build", ".mcpack"}
            and (child / METADATA_DIR / MARKER_FILE).is_file()
        )

    def artifact_components(self) -> tuple[ArtifactRegistry, ArtifactCache, Builder]:
        registry = ArtifactRegistry()
        registry.register("local", LocalArtifactProvider(self.project))
        registry.register("direct_url", DirectUrlArtifactProvider())
        cache = ArtifactCache(self.state.root / "cache")
        return registry, cache, Builder(ArtifactResolver(registry, cache))

    def resolve_branch(
        self,
        branch: str,
        parameters: dict[str, Any] | None = None,
        *,
        automatic_dependencies: bool = True,
    ) -> Resolution:
        return Resolver(
            WorkspaceLoader(self.project), automatic_dependencies=automatic_dependencies
        ).resolve(self.branch_root(branch), parameters)

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
            if old in links:
                old_link = links.pop(old)
                effective = old_link.effective_layer
                old_overlay = self.state.feature_root(old)
                new_overlay = self.state.feature_root(new)
                if old_overlay.exists():
                    new_overlay.parent.mkdir(parents=True, exist_ok=True)
                    old_overlay.rename(new_overlay)
                    effective = self._layer_reference(new_overlay)
                    self._rewrite_overlay_identity(new, new_overlay)
                links[new] = LinkRecord(new, old_link.branch, effective)
            self.state.write_instances(instances)
            self.state.write_links(links)
            self._rename_optional(self.state.parameters_path(old), self.state.parameters_path(new))
            self._rename_optional(self.state.web_log_path(old), self.state.web_log_path(new))

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
        instances = self.state.instances()
        links = self.state.links()
        if name not in instances:
            raise MetadataError(f"instance does not exist: {name!r}")
        if name not in links:
            raise MetadataError(f"instance is not linked: {name!r}")
        link = links[name]
        self._validate_instance_path(instances[name].path)
        parameters, _ = self.state.parameters(name)
        layer = self._effective_root(name, link)
        features = self._overlay_features(name, link)
        resolution = Resolver(WorkspaceLoader(self.project)).resolve(layer, parameters)
        files: dict[str, bytes] = {}
        if prepare:
            _, _, builder = self.artifact_components()
            resolution, files = builder.prepare(resolution)
        return InstanceContext(instances[name], link, features, parameters, resolution, files)

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
            package_metadata = (
                inspect_jar(context.instance.path / Path(path))
                if enrich_actual and path.lower().endswith(".jar")
                else None
            )
            item = self._delta_item(context, "add", path)
            if not enrich_actual and path.lower().endswith(".jar"):
                item["kind"] = "package"
            if enrich_actual:
                item["actual_sha256"] = actual_hashes[path]
            item["capture_modes"] = (
                ["package", "payload"]
                if package_metadata is not None
                or (not enrich_actual and path.lower().endswith(".jar"))
                else ["payload"]
            )
            item["enriched"] = enrich_actual
            if package_metadata is not None:
                item["kind"] = "package"
                item["jar_metadata"] = package_metadata.document()
                item["provider_candidates"] = self._provider_candidates(
                    actual_content[path], package_metadata.version
                )
            items.append(item)
            summary["add"] += 1

        for path in sorted((existing & desired) - ignored):
            desired_content = context.files[path]
            current_content = transport.read(path)
            if desired_content == current_content:
                continue
            package = self._package_for_path(context, path)
            metadata = (
                inspect_jar(context.instance.path / Path(path))
                if enrich_actual and package
                else None
            )
            status = "change"
            if (
                package is not None
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
                status = "update"
            item = self._delta_item(
                context,
                status,
                path,
                package_id=package.identity if package else None,
            )
            item["desired_sha256"] = hashlib.sha256(desired_content).hexdigest()
            item["actual_sha256"] = hashlib.sha256(current_content).hexdigest()
            item["capture_modes"] = ["package"] if package else ["payload"]
            item["enriched"] = enrich_actual or package is None
            if package is not None:
                item["desired_version"] = package.version
                item["logical_id"] = package.logical_identity
                if enrich_actual:
                    item["provider_candidates"] = self._provider_candidates(
                        current_content,
                        metadata.version if metadata is not None else None,
                        allow_remote=metadata is not None,
                    )
            if metadata is not None:
                item["jar_metadata"] = metadata.document()
                item["actual_version"] = metadata.version
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
    def _context_fingerprint(context: InstanceContext) -> str:
        digest = hashlib.sha256()
        for path, content in sorted(context.files.items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(content).digest())
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
                                        operation_to_json(operation)
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
            if self._snapshot_auto_payload_epochs_match(cached) and scan_generation_matches(
                cached.scan, desired_paths
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
        page = self.explorer_snapshot(instance).page(
            parent,
            mode=mode,
            show_ignored=show_ignored,
            cursor=cursor,
            limit=limit,
        )
        page["ignore_rules"] = self.ignore_rules(instance, target_layer)
        return page

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
            and not entry["actionable"]
        ]
        return {"path": normalized, "items": items, "unsupported": conflicts}

    def ignore_rules(self, instance: str, target_layer: str | None = None) -> list[dict[str, Any]]:
        context = self.instance_context(instance)
        target = self._target_path(instance, context, target_layer)
        rules: list[dict[str, Any]] = []
        for trace in context.resolution.trace:
            trace_root = Path(trace.root).resolve()
            for producer in trace.producers:
                for index, operation in enumerate(producer.operations):
                    if not isinstance(operation, AddIgnoreSelector):
                        continue
                    own = (
                        trace_root == target
                        and producer.source == f"{METADATA_DIR}/local-edits.json"
                    )
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

    def add_ignore(
        self,
        instance: str,
        path: str,
        selector_kind: str,
        target_layer: str | None = None,
    ) -> None:
        context = self.instance_context(instance)
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
        with self.state.target_layer_lock(target), _BeforeImageJournal(self, target, journal_paths):
            editable = self._editable_target(instance, context, target_layer)
            self._append_operation(
                editable,
                {"type": "ignore", "selector": selector.document()},
            )
            refreshed = self.instance_context(instance)
        cached = self._explorer_snapshots.get(instance)
        if cached is not None:
            self._explorer_snapshots[instance] = cached.reproject(
                refreshed.resolution.state.ignore,
                context_fingerprint=self._context_fingerprint(refreshed),
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
        target = self._target_path(instance, context, target_layer)
        expected_target = self._layer_reference(target)
        if identity.get("target") != expected_target:
            raise MetadataError("ignore rule does not belong to the selected target layer")
        if identity.get("source") != f"{METADATA_DIR}/local-edits.json":
            raise MetadataError("inherited ignore rules cannot be removed")
        index = identity.get("index")
        if not isinstance(index, int) or index < 0:
            raise MetadataError("ignore rule identity has an invalid operation index")
        with self.state.target_layer_lock(target):
            path = target / METADATA_DIR / "local-edits.json"
            with _BeforeImageJournal(self, target, [target / METADATA_DIR / "local-edits.json"]):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    operations = document["operations"]
                    raw = operations[index]
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    KeyError,
                    IndexError,
                    TypeError,
                ) as exc:
                    raise MetadataError("ignore rule moved or no longer exists") from exc
                if not isinstance(raw, dict):
                    raise MetadataError("ignore rule moved or no longer exists")
                parsed = operation_from_json(
                    raw,
                    source=f"{METADATA_DIR}/local-edits.json",
                    index=index,
                )
                if not isinstance(
                    parsed, AddIgnoreSelector
                ) or parsed.selector.document() != identity.get("selector"):
                    raise MetadataError("ignore rule moved or no longer matches its identity")
                del operations[index]
                atomic_write(
                    path,
                    (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode(),
                )
                refreshed = self.instance_context(instance)
        cached = self._explorer_snapshots.get(instance)
        if cached is not None:
            self._explorer_snapshots[instance] = cached.reproject(
                refreshed.resolution.state.ignore,
                context_fingerprint=self._context_fingerprint(refreshed),
            )

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
            (item for item in scoped_entries if item["changed"] and not item["actionable"]),
            None,
        )
        if unsupported is not None:
            reason = unsupported["reason"] or "unsupported entry"
            raise MetadataError(f"cannot capture {unsupported['path']!r}: {reason}")
        delta_by_id = {
            str(item["id"]): dict(item)
            for item in snapshot.delta_items
            if self._delta_in_scope(item, normalized)
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

        with self.state.target_layer_lock(provisional_target):
            context = self.instance_context(instance)
            target = self._target_path(instance, context, target_layer)
            if target.resolve() != provisional_target.resolve():
                raise MetadataError("selected physical target changed before capture")
            if snapshot.context_fingerprint != self._context_fingerprint(context):
                raise MetadataError("file plan changed before capture; refresh it")
            if not scan_generation_matches(snapshot.scan, ()):
                raise MetadataError("instance directory changed before capture; refresh it")

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
                if not provider_is_local:
                    raise MetadataError(
                        "folder package capture cannot use remote provider enrichment; "
                        "import the package individually"
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
                if (
                    mode == "package"
                    and item.get("package_id") is None
                    and (content is None or self._inspect_jar_content(content) is None)
                ):
                    raise MetadataError(f"package capture requires a valid JAR: {item_path!r}")
                captured.append((item, mode, content))

            payload_writes: dict[Path, bytes] = {}
            artifact_writes: dict[Path, bytes] = {}
            additions: list[dict[str, Any]] = []
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
                    additions.append(operation)
                elif package_id is not None:
                    additions.append({"type": "activation", "id": package_id, "state": "disabled"})
                elif content is None:
                    additions.append({"type": "remove", "path": item_path})
                elif mode == "package":
                    artifact, operation = self._batch_local_package(
                        target, item_path, content, reserved_ids
                    )
                    artifact_writes[artifact] = content
                    additions.append(operation)
                else:
                    payload_writes[self._bounded_target_path(target, item_path)] = content

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
                    self._append_operations(target, additions)
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
                    for item_path, actual_content in reconciliations.items():
                        if final_context.files.get(item_path) != actual_content:
                            raise MetadataError(
                                f"captured folder did not reconcile changed path {item_path!r}"
                            )

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

        with self.state.target_layer_lock(provisional_target):
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
                item["enriched"] = package is None and not item_path.lower().endswith(".jar")
                if desired is not None:
                    item["desired_sha256"] = hashlib.sha256(desired).hexdigest()
                if package is not None:
                    item["capture_modes"] = ["package"]
                    item["desired_version"] = package.version
                    item["logical_id"] = package.logical_identity
                elif item_path.lower().endswith(".jar"):
                    item["kind"] = "package"
                    item["capture_modes"] = ["package", "payload"]
                else:
                    item["capture_modes"] = ["payload"]
                contents[item_path] = content
                items.append(item)

            target_reason: str | None = None
            if not target.is_dir() or not (target / METADATA_DIR / MARKER_FILE).is_file():
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
        artifact = target / METADATA_DIR / "artifacts" / f"{digest}.jar"
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
                "location": artifact.relative_to(self.project).as_posix(),
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
        artifact = target / METADATA_DIR / "artifacts" / f"{digest}.jar"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(destination).stem).strip("-")
        package_id = stem or f"imported-{digest[:12]}"
        if package_id in reserved_ids:
            package_id = f"{package_id}-{digest[:8]}"
        suffix = 2
        candidate = package_id
        while candidate in reserved_ids:
            candidate = f"{package_id}-{suffix}"
            suffix += 1
        package_id = candidate
        reserved_ids.add(package_id)
        return artifact, {
            "type": "package",
            "id": package_id,
            "activation": "enabled",
            "destination": destination,
            "source": {
                "provider": "local",
                "location": artifact.relative_to(self.project).as_posix(),
                "sha256": digest,
            },
            "update_policy": "fixed",
        }

    def _same_layer_capture_reason(self, target: Path, item: dict[str, Any]) -> str | None:
        """Reject raw writes that conflict with a structured producer in the target."""
        metadata_root = target / METADATA_DIR
        package_id = item.get("package_id")
        if package_id is not None:
            for metadata_path in sorted(metadata_root.glob("*.json")):
                try:
                    document = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise MetadataError(
                        f"cannot inspect same-layer provenance {metadata_path}: {exc}"
                    ) from exc
                operations = document.get("operations") if isinstance(document, dict) else None
                if not isinstance(operations, list):
                    continue
                if any(
                    isinstance(raw, dict)
                    and raw.get("type") == "package"
                    and raw.get("id") == package_id
                    for raw in operations
                ):
                    if metadata_path.name == "local-edits.json":
                        return None
                    return (
                        f"package {package_id!r} is declared in {metadata_path.name}; "
                        "capturing it here needs a provenance-aware declaration edit"
                    )
            return None
        affected = {normalize_relative_path(str(item["path"]))}
        if item.get("to_path"):
            affected.add(normalize_relative_path(str(item["to_path"])))
        for metadata_path in sorted(metadata_root.glob("*.json")):
            try:
                document = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MetadataError(
                    f"cannot inspect same-layer provenance {metadata_path}: {exc}"
                ) from exc
            operations = document.get("operations") if isinstance(document, dict) else None
            if not isinstance(operations, list):
                continue
            for raw in operations:
                if not isinstance(raw, dict):
                    continue
                operation_type = raw.get("type")
                operation_path = raw.get("path")
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
                pattern = raw.get("glob")
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

    def _provider_candidates(
        self, content: bytes, version: str | None, *, allow_remote: bool = True
    ) -> list[dict[str, object]]:
        hashes = file_hashes(content)
        cached = self._provider_matches.get(hashes["sha512"])
        if cached is not None:
            return cached
        candidates: list[dict[str, object]] = [
            {
                "provider": "local",
                "project_id": "",
                "version_id": hashes["sha256"],
                "version": version,
                "download_url": "",
                "hashes": hashes,
                "confidence": "exact",
                "reason": "local exact-SHA256 fallback",
            }
        ]
        if allow_remote:
            match = self.provider_matcher.match(content)
            if match is not None:
                candidates.append(match.document())
        self._provider_matches[hashes["sha512"]] = candidates
        return candidates

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
        matches_id = [path for path in existing | desired if self._delta_id(path) == delta_id]
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
            if path.lower().endswith(".jar"):
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

    @staticmethod
    def _inspect_jar_content(content: bytes) -> Any:
        with tempfile.TemporaryDirectory(prefix="mcpack-jar-inspect-") as directory:
            path = Path(directory) / "selected.jar"
            path.write_bytes(content)
            return inspect_jar(path)

    def delta_detail(self, instance: str, delta_id: str) -> dict[str, Any]:
        context = self.instance_context(instance)
        selected, content = self._quick_delta_selection(context, delta_id)
        item = dict(selected)
        if content is None:
            item["enriched"] = True
            return item
        item["actual_sha256"] = hashlib.sha256(content).hexdigest()
        path = normalize_relative_path(str(item["path"]))
        package = self._package_for_path(context, path)
        if not path.lower().endswith(".jar"):
            item["enriched"] = True
            return item
        metadata = self._inspect_jar_content(content)
        if metadata is None and package is None:
            item["kind"] = "file"
            item["capture_modes"] = ["payload"]
            item["enriched"] = True
            return item
        item["kind"] = "package"
        item["capture_modes"] = ["package"] if package is not None else ["package", "payload"]
        item["provider_candidates"] = self._provider_candidates(
            content,
            metadata.version if metadata is not None else None,
            allow_remote=metadata is not None,
        )
        if metadata is not None:
            item["jar_metadata"] = metadata.document()
            item["actual_version"] = metadata.version
        if package is not None:
            item["desired_version"] = package.version
            item["logical_id"] = package.logical_identity
            if (
                metadata is not None
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
        item["enriched"] = True
        return item

    def bootstrap(self, name: str, *, include_delta: bool = True) -> dict[str, Any]:
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
                    data = operation_to_json(operation)
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
                "activation": package.activation.value,
                "destination": package.destination,
                "source": {
                    "provider": package.source.provider,
                    "location": package.source.location,
                    "sha256": package.source.sha256,
                },
                "source_file": package.source_file,
                "editable": package.source_file is not None,
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
                    data = operation_to_json(operation)
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

    def set_activation(
        self, instance: str, package_id: str, state: str, target_layer: str | None = None
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
        target = self._editable_target(instance, context, target_layer)
        before: bytes | None
        if (
            target_layer is not None
            and package.source_file is not None
            and Path(package.source_file).resolve().is_relative_to(target)
        ):
            path = Path(package.source_file)
            before = path.read_bytes()
            self._edit_declaration_activation(path, package_id, package.source_index, activation)
        else:
            path = target / METADATA_DIR / "local-edits.json"
            before = path.read_bytes() if path.exists() else None
            self._append_operation(
                target,
                {"type": "activation", "id": package_id, "state": activation.value},
            )
        try:
            self.instance_context(instance)
        except Exception:
            self._restore_file(path, before)
            raise

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
        self._append_operation(target, operation)
        try:
            self.instance_context(instance)
        except Exception:
            self._restore_file(path, before)
            raise

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
        )
        try:
            self.instance_context(instance)
        except Exception:
            self._restore_file(path, before)
            raise

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
            self._capture_package_artifact(context, target, str(package_id), path, content)
            return
        if item["status"] == "rename" and content is not None:
            desired_path = normalize_relative_path(str(item["to_path"]))
            payload_path = target.joinpath(*path.split("/"))
            payload_before = payload_path.read_bytes() if payload_path.exists() else None
            operations_path = target / METADATA_DIR / "local-edits.json"
            operations_before = operations_path.read_bytes() if operations_path.exists() else None
            atomic_write(payload_path, content)
            self._append_operation(target, {"type": "remove", "path": desired_path})
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
                target, {"type": "activation", "id": package_id, "state": "disabled"}
            )
        elif content is not None:
            changed_path = target.joinpath(*path.split("/"))
            before = changed_path.read_bytes() if changed_path.exists() else None
            atomic_write(changed_path, content)
        else:
            changed_path = target / METADATA_DIR / "local-edits.json"
            before = changed_path.read_bytes() if changed_path.exists() else None
            self._append_operation(target, {"type": "remove", "path": path})
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
        if item["status"] == "remove" or not item["path"].lower().endswith(".jar"):
            raise MetadataError("package import requires an existing changed or extra JAR")
        target = self._editable_target(instance, context, target_layer)
        metadata = self._inspect_jar_content(content) if content is not None else None
        if content is None or metadata is None:
            raise MetadataError("package import requires a valid changed or extra JAR")
        digest = hashlib.sha256(content).hexdigest()
        match = None
        if (
            provider_match is None
            or provider_match == "local"
            or (isinstance(provider_match, dict) and provider_match.get("provider") == "local")
        ):
            match = None
        elif provider_match == "modrinth":
            candidate = self.provider_matcher.match(content)
            match = candidate if candidate is not None and candidate.confidence == "exact" else None
        else:
            match = confirmed_provider_match(provider_match, content)
        artifact: Path | None = None
        artifact_existed = False
        cached: Path | None = None
        if match is None:
            artifact = target / METADATA_DIR / "artifacts" / f"{digest}.jar"
            artifact_existed = artifact.exists()
            source = {
                "provider": "local",
                "location": artifact.relative_to(self.project).as_posix(),
                "sha256": digest,
            }
        else:
            source = {
                "provider": "direct_url",
                "location": match.download_url,
                "sha256": digest,
            }
            cached = self.state.root / "cache" / "sha256" / digest[:2] / digest
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(item["path"]).stem).strip("-")
        existing_package = self._package_for_path(context, str(item["path"]))
        package_id = (
            existing_package.identity if existing_package else stem or f"imported-{digest[:12]}"
        )
        existing_ids = set(context.resolution.state.packages)
        if existing_package is None and package_id in existing_ids:
            package_id = f"{package_id}-{digest[:8]}"
        version = (
            (match.version if match is not None else None)
            or metadata.version
            or (existing_package.version if existing_package is not None else None)
        )
        operations_path = target / METADATA_DIR / "local-edits.json"
        owns_declaration = (
            existing_package is not None
            and existing_package.source_file is not None
            and Path(existing_package.source_file).resolve().parent
            == (target / METADATA_DIR).resolve()
        )
        if owns_declaration:
            assert existing_package is not None and existing_package.source_file is not None
            operations_path = Path(existing_package.source_file)
        operations_before = operations_path.read_bytes() if operations_path.exists() else None
        declaration_document = None
        if owns_declaration:
            assert existing_package is not None
            if existing_package.source_index is None or existing_package.source_index < 0:
                raise MetadataError(f"package declaration has no source index: {package_id!r}")
            if operations_before is None:
                raise MetadataError(f"cannot locate package declaration {package_id!r}")
            try:
                declaration_document = json.loads(operations_before)
                declaration = declaration_document["operations"][existing_package.source_index]
            except (UnicodeError, ValueError, KeyError, IndexError, TypeError) as exc:
                raise MetadataError(f"cannot locate package declaration {package_id!r}") from exc
            if (
                not isinstance(declaration, dict)
                or declaration.get("type") != "package"
                or declaration.get("id") != package_id
            ):
                raise MetadataError(f"package declaration moved for {package_id!r}")
            declaration["source"] = source
            declaration["update_policy"] = "latest_compatible" if match is not None else "fixed"
            if version:
                declaration["version"] = version
        operations: list[dict[str, Any]] = []
        payload_path = target.joinpath(*str(item["path"]).split("/"))
        payload_before: bytes | None = None
        if item["path"] in context.resolution.state.files:
            operations.append({"type": "remove", "path": item["path"]})
            if payload_path.is_file():
                payload_before = payload_path.read_bytes()
        operations.append(
            {
                "type": "package",
                "id": package_id,
                **(
                    {
                        "logical_id": existing_package.logical_identity,
                        "kind": existing_package.kind,
                        "automatic_requires": list(existing_package.automatic_requires),
                        **({"alias": existing_package.alias} if existing_package.alias else {}),
                    }
                    if existing_package is not None
                    else {}
                ),
                "activation": "enabled",
                "destination": item["path"],
                "source": source,
                "update_policy": "latest_compatible" if match is not None else "fixed",
                **({"version": version} if version else {}),
            },
        )
        try:
            if artifact is not None:
                atomic_write(artifact, content)
            if cached is not None:
                atomic_write(cached, content)
            if payload_before is not None:
                payload_path.unlink()
            if declaration_document is not None:
                rendered = json.dumps(declaration_document, indent=2, ensure_ascii=False) + "\n"
                atomic_write(operations_path, rendered.encode())
            else:
                self._append_operations(target, operations)
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
            raise

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
        overlay_metadata.mkdir(parents=True, exist_ok=True)
        parents = [
            str(self.branch_root(branch)),
            *(str(self.branch_root(item)) for item in features),
        ]
        marker = {
            "schema": 1,
            "id": f"local-{instance}",
            "parents": parents,
            "features": features,
        }
        # ``features`` is local bookkeeping and is stripped before the ordinary loader sees it.
        atomic_write(
            overlay_metadata / MARKER_FILE,
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

    def _change_features(
        self, instance: str, context: InstanceContext, features: list[str]
    ) -> LinkRecord:
        marker_path = self.state.feature_root(instance) / METADATA_DIR / MARKER_FILE
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
        if not (overlay / METADATA_DIR / MARKER_FILE).is_file():
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
        if set(marker) != allowed or marker.get("schema") != 1:
            raise MetadataError(f"invalid virtual layer marker for {instance!r}")
        features = marker["features"]
        expected = [str(self.branch_root(link.branch))]
        if not isinstance(features, list):
            raise MetadataError("virtual layer features must be an array")
        expected.extend(str(self.branch_root(str(item))) for item in features)
        if marker["parents"] != expected:
            raise MetadataError("virtual layer parents are not confined project branches")

    def _overlay_marker(self, overlay: Path) -> dict[str, Any]:
        path = overlay / METADATA_DIR / MARKER_FILE
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

    def _append_operation(self, layer: Path, operation: dict[str, Any]) -> None:
        self._append_operations(layer, [operation])

    def _append_operations(self, layer: Path, additions: list[dict[str, Any]]) -> None:
        path = layer / METADATA_DIR / "local-edits.json"
        if path.exists():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise MetadataError(f"cannot edit {path}: {exc}") from exc
            if not isinstance(document, dict) or set(document) != {"operations"}:
                raise MetadataError(f"{path}: unsupported local edit document")
        else:
            document = {"operations": []}
        operations = document["operations"]
        if not isinstance(operations, list):
            raise MetadataError(f"{path}: operations must be an array")
        for operation in additions:
            if operation.get("type") in {"activation", "package", "group"}:
                operations[:] = [
                    item
                    for item in operations
                    if not (
                        isinstance(item, dict)
                        and item.get("type") == operation.get("type")
                        and item.get("id") == operation.get("id")
                    )
                ]
            operations.append(operation)
        atomic_write(path, (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode())

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
        artifact = target / METADATA_DIR / "artifacts" / f"{digest}.jar"
        artifact_existed = artifact.exists()
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
        self._append_operation(target, operation)
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

    @staticmethod
    def _edit_declaration_activation(
        path: Path, package_id: str, source_index: int | None, activation: Activation
    ) -> None:
        if source_index is None:
            raise MetadataError(f"package {package_id!r} has no editable source index")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            operation = document["operations"][source_index]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise MetadataError(f"cannot locate package declaration {package_id!r}") from exc
        if operation.get("type") != "package" or operation.get("id") != package_id:
            raise MetadataError(f"package declaration moved for {package_id!r}")
        operation["activation"] = activation.value
        atomic_write(path, (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode())

    @staticmethod
    def _restore_file(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, content)

    def _layer_reference(self, path: Path) -> str:
        return path.resolve().relative_to(self.project).as_posix()

    def _rewrite_overlay_identity(self, instance: str, overlay: Path) -> None:
        marker = self._overlay_marker(overlay)
        marker["id"] = f"local-{instance}"
        atomic_write(
            overlay / METADATA_DIR / MARKER_FILE,
            (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
        )

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
