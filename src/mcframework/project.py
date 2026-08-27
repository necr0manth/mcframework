"""High-level project, instance, feature, diff, and bounded-edit services."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
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
from .explorer import ExplorerSnapshot, build_snapshot
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
        self, context: InstanceContext, *, respect_ignore: bool = True
    ) -> dict[str, Any]:
        transport = LocalFilesystemInstance(context.instance.path)
        existing = set(transport.list_files())
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
        actual_content = {path: transport.read(path) for path in actual_only}
        desired_hashes = {
            path: hashlib.sha256(context.files[path]).hexdigest() for path in desired_only
        }
        actual_hashes = {
            path: hashlib.sha256(content).hexdigest() for path, content in actual_content.items()
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
            items.append(item)
            summary["remove"] += 1

        for path in sorted(actual_only):
            package_metadata = (
                inspect_jar(context.instance.path / Path(path))
                if path.lower().endswith(".jar")
                else None
            )
            item = self._delta_item(context, "add", path)
            item["actual_sha256"] = actual_hashes[path]
            item["capture_modes"] = ["package", "payload"] if package_metadata else ["payload"]
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
            metadata = inspect_jar(context.instance.path / Path(path)) if package else None
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
            if package is not None:
                item["desired_version"] = package.version
                item["logical_id"] = package.logical_identity
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
        return {"items": items, "summary": summary}

    def invalidate_explorer(self, instance: str) -> None:
        self._explorer_snapshots.pop(instance, None)

    def explorer_snapshot(self, instance: str, *, refresh: bool = False) -> ExplorerSnapshot:
        if not refresh and instance in self._explorer_snapshots:
            return self._explorer_snapshots[instance]
        context = self.instance_context(instance)
        underlying = self.instance_diff(context, respect_ignore=False)
        snapshot = build_snapshot(
            context.instance.path,
            context.files,
            underlying["items"],
            context.resolution.state.ignore,
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
        selected = snapshot.entries.get(normalized)
        if selected is None:
            raise MetadataError(f"explorer path does not exist: {normalized!r}")
        prefix = normalized + "/"
        context = self.instance_context(instance)
        physical_target = self._target_path(instance, context, target_layer)
        items = []
        seen: set[str] = set()
        for item in self.instance_diff(context, respect_ignore=False)["items"]:
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
        with self.state.target_layer_lock(target), _BeforeImageJournal(self, target):
            editable = self._editable_target(instance, context, target_layer)
            self._append_operation(
                editable,
                {"type": "ignore", "selector": selector.document()},
            )
            self.instance_context(instance)
        self.invalidate_explorer(instance)

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
            with _BeforeImageJournal(self, target):
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
                self.instance_context(instance)
        self.invalidate_explorer(instance)

    def capture_explorer_path(
        self,
        instance: str,
        path: str,
        target_layer: str | None = None,
        decisions: object = None,
    ) -> None:
        normalized = normalize_relative_path(path)
        context = self.instance_context(instance)
        target = self._target_path(instance, context, target_layer)
        snapshot = self.explorer_snapshot(instance, refresh=True)
        selected = snapshot.entries.get(normalized)
        if selected is None:
            raise MetadataError(f"explorer path does not exist: {normalized!r}")
        prefix = normalized + "/"
        scoped_entries = [
            item
            for item in snapshot.entries.values()
            if item["path"] == normalized or item["path"].startswith(prefix)
        ]
        unsupported = next(
            (item for item in scoped_entries if item["changed"] and not item["actionable"]),
            None,
        )
        if unsupported is not None:
            reason = unsupported["reason"] or "unsupported entry"
            raise MetadataError(f"cannot capture {unsupported['path']!r}: {reason}")
        delta_by_id: dict[str, dict[str, Any]] = {}
        underlying = self.instance_diff(context, respect_ignore=False)["items"]
        for item in underlying:
            item_path = normalize_relative_path(str(item["path"]))
            target_path = str(item.get("to_path", ""))
            if (
                item_path == normalized
                or item_path.startswith(prefix)
                or (
                    target_path
                    and (
                        normalize_relative_path(target_path) == normalized
                        or normalize_relative_path(target_path).startswith(prefix)
                    )
                )
            ):
                delta_by_id[str(item["id"])] = item
        if not delta_by_id:
            raise MetadataError(f"explorer path has no changed descendants: {normalized!r}")
        decision_map = decisions if isinstance(decisions, dict) else {}
        plan: list[tuple[dict[str, Any], str, object]] = []
        for item in sorted(delta_by_id.values(), key=lambda value: str(value["path"])):
            item_id = str(item["id"])
            raw_decision = decision_map.get(item_id)
            modes = [str(value) for value in item.get("capture_modes", [])]
            mode = modes[0] if len(modes) == 1 else ""
            provider_match: object = None
            if "package" in modes:
                if not isinstance(raw_decision, dict) or not isinstance(
                    raw_decision.get("mode"), str
                ):
                    raise MetadataError(
                        f"delta {item_id} requires an explicit package or payload decision"
                    )
                mode = str(raw_decision["mode"])
                provider_match = raw_decision.get("provider_match")
            if mode not in modes:
                raise MetadataError(f"capture mode {mode!r} is not available for {item['path']!r}")
            same_layer_reason = self._same_layer_capture_reason(target, item)
            if same_layer_reason is not None:
                raise MetadataError(
                    f"cannot capture {item['path']!r} into its owning layer: {same_layer_reason}"
                )
            if item["status"] == "add" and any(
                matches(str(item["path"]), pattern)
                for pattern in context.resolution.state.exclusions
            ):
                raise MetadataError(
                    f"cannot capture {item['path']!r}: inherited remove_paths excludes it"
                )
            plan.append((item, mode, provider_match))

        with self.state.target_layer_lock(target), _BeforeImageJournal(self, target):
            for original, mode, provider_match in plan:
                refreshed = self.instance_context(instance)
                current = next(
                    (
                        item
                        for item in self.instance_diff(refreshed, respect_ignore=False)["items"]
                        if item["id"] == original["id"]
                    ),
                    None,
                )
                if current is None:
                    raise MetadataError(
                        f"delta changed while capture was being planned: {original['path']!r}"
                    )
                if mode == "package" and current.get("package_id") is None:
                    self.import_package(
                        instance,
                        str(current["id"]),
                        target_layer,
                        provider_match,
                    )
                else:
                    self.capture_delta(instance, str(current["id"]), target_layer)
            final_context = self.instance_context(instance)
            remaining = self.instance_diff(final_context, respect_ignore=False)["items"]
            if any(
                str(item["path"]) == normalized
                or str(item["path"]).startswith(prefix)
                or str(item.get("to_path", "")) == normalized
                or str(item.get("to_path", "")).startswith(prefix)
                for item in remaining
            ):
                raise MetadataError("captured folder did not reconcile all changed descendants")
        self.invalidate_explorer(instance)

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
            "id": hashlib.sha256(f"{status}\0{path}".encode()).hexdigest()[:20],
            "kind": "package" if package_id is not None else "file",
            "status": status,
            "path": path,
            "editable": True,
            "capture_modes": [],
        }
        if package_id is not None:
            item["package_id"] = package_id
        return item

    def bootstrap(self, name: str) -> dict[str, Any]:
        context = self.instance_context(name)
        delta = self.instance_diff(context)
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

    def capture_delta(self, instance: str, delta_id: str, target_layer: str | None = None) -> None:
        context = self.instance_context(instance)
        item = next(
            (entry for entry in self.instance_diff(context)["items"] if entry["id"] == delta_id),
            None,
        )
        if item is None:
            raise MetadataError(f"instance delta item does not exist: {delta_id!r}")
        target = self._editable_target(instance, context, target_layer)
        path = normalize_relative_path(item["path"])
        source = context.instance.path / Path(path)
        package_id = item.get("package_id")
        if package_id is not None and source.is_file():
            self._capture_package_artifact(
                context, target, str(package_id), path, source.read_bytes()
            )
            return
        if item["status"] == "rename" and source.is_file():
            desired_path = normalize_relative_path(str(item["to_path"]))
            payload_path = target.joinpath(*path.split("/"))
            payload_before = payload_path.read_bytes() if payload_path.exists() else None
            operations_path = target / METADATA_DIR / "local-edits.json"
            operations_before = operations_path.read_bytes() if operations_path.exists() else None
            atomic_write(payload_path, source.read_bytes())
            self._append_operation(target, {"type": "remove", "path": desired_path})
            try:
                refreshed = self.instance_context(instance)
                remaining = {entry.get("path") for entry in self.instance_diff(refreshed)["items"]}
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
        elif source.is_file():
            changed_path = target.joinpath(*path.split("/"))
            before = changed_path.read_bytes() if changed_path.exists() else None
            atomic_write(changed_path, source.read_bytes())
        else:
            changed_path = target / METADATA_DIR / "local-edits.json"
            before = changed_path.read_bytes() if changed_path.exists() else None
            self._append_operation(target, {"type": "remove", "path": path})
        try:
            refreshed = self.instance_context(instance)
            if any(entry["path"] == path for entry in self.instance_diff(refreshed)["items"]):
                raise MetadataError(f"captured change did not reconcile {path!r}")
        except Exception:
            if package_id is None or not source.is_file():
                self._restore_file(changed_path, before)
            raise

    def import_package(
        self,
        instance: str,
        delta_id: str,
        target_layer: str | None = None,
        provider_match: object = None,
    ) -> None:
        context = self.instance_context(instance)
        item = next(
            (entry for entry in self.instance_diff(context)["items"] if entry["id"] == delta_id),
            None,
        )
        if item is None or item["status"] == "remove" or not item["path"].lower().endswith(".jar"):
            raise MetadataError("package import requires an existing changed or extra JAR")
        target = self._editable_target(instance, context, target_layer)
        content = (context.instance.path / Path(item["path"])).read_bytes()
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
        if match is None:
            artifact = target / METADATA_DIR / "artifacts" / f"{digest}.jar"
            artifact_existed = artifact.exists()
            atomic_write(artifact, content)
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
            atomic_write(cached, content)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(item["path"]).stem).strip("-")
        package_id = stem or f"imported-{digest[:12]}"
        existing_ids = set(context.resolution.state.packages)
        if package_id in existing_ids:
            package_id = f"{package_id}-{digest[:8]}"
        operations_path = target / METADATA_DIR / "local-edits.json"
        operations_before = operations_path.read_bytes() if operations_path.exists() else None
        self._append_operation(
            target,
            {
                "type": "package",
                "id": package_id,
                "activation": "enabled",
                "destination": item["path"],
                "source": source,
                "update_policy": "latest_compatible" if match is not None else "fixed",
                **({"version": match.version} if match is not None and match.version else {}),
            },
        )
        try:
            refreshed = self.instance_context(instance)
            if any(
                entry["path"] == item["path"] for entry in self.instance_diff(refreshed)["items"]
            ):
                raise MetadataError(f"imported package did not reconcile {item['path']!r}")
        except Exception:
            self._restore_file(operations_path, operations_before)
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
                entry["path"] == destination for entry in self.instance_diff(refreshed)["items"]
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
    """Temporary request-scoped filesystem before-images for bounded layer edits."""

    def __init__(self, service: ProjectService, target: Path):
        self.service = service
        self.target = target.resolve()
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self.target_existed = False
        self.cache_existed = False
        self.links_before: bytes | None = None
        self.absent_ancestors: list[Path] = []

    def __enter__(self) -> _BeforeImageJournal:
        self.service.state.root.mkdir(parents=True, exist_ok=True)
        state_root = self.service.state.root.resolve()
        if self.target.is_relative_to(state_root):
            ancestor = self.target.parent
            while ancestor != state_root:
                if not ancestor.exists():
                    self.absent_ancestors.append(ancestor)
                ancestor = ancestor.parent
        self.temporary = tempfile.TemporaryDirectory(
            prefix="explorer-journal-", dir=self.service.state.root
        )
        self.root = Path(self.temporary.name)
        self.target_existed = self.target.is_dir()
        if self.target_existed:
            shutil.copytree(self.target, self.root / "target", symlinks=True)
        cache = self.service.state.root / "cache"
        self.cache_existed = cache.is_dir()
        if self.cache_existed:
            shutil.copytree(cache, self.root / "cache", symlinks=True)
        links = self.service.state.links_path
        self.links_before = links.read_bytes() if links.exists() else None
        return self

    def __exit__(self, error_type: object, error: object, traceback: object) -> None:
        del error, traceback
        try:
            if error_type is not None:
                self._restore_tree(self.target, "target", self.target_existed)
                self._restore_tree(self.service.state.root / "cache", "cache", self.cache_existed)
                self.service._restore_file(self.service.state.links_path, self.links_before)
                for ancestor in self.absent_ancestors:
                    if ancestor.is_dir() and next(ancestor.iterdir(), None) is None:
                        ancestor.rmdir()
        finally:
            if self.temporary is not None:
                self.temporary.cleanup()

    def _restore_tree(self, destination: Path, name: str, existed: bool) -> None:
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination)
        if existed:
            if self.root is None:
                raise RuntimeError("journal was not entered")
            shutil.copytree(self.root / name, destination, symlinks=True)


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
