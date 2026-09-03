"""Bounded local instance file explorer snapshots."""

from __future__ import annotations

import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import MetadataError
from .model import IgnoreSelector
from .paths import normalize_relative_path

type SourceGenerations = tuple[tuple[Path, tuple[int, int, int, int] | None], ...]


@dataclass(frozen=True)
class ExplorerScan:
    root: Path
    entries: dict[str, dict[str, Any]]
    children: dict[str, tuple[str, ...]]
    files: frozenset[str]
    generation: tuple[int, int, int]
    entry_generations: dict[str, tuple[str, int, int, int]]


@dataclass(frozen=True)
class ExplorerSnapshot:
    entries: dict[str, dict[str, Any]]
    children: dict[str, tuple[str, ...]]
    scan: ExplorerScan
    delta_items: tuple[dict[str, Any], ...]
    selectors: tuple[IgnoreSelector, ...]
    context_fingerprint: str
    context_verified: bool = True
    auto_payload_epochs: tuple[tuple[str, str | None], ...] = ()
    source_generations: SourceGenerations = ()

    def page(
        self,
        parent: str,
        *,
        mode: str,
        show_ignored: bool,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        if parent:
            parent = normalize_relative_path(parent)
            entry = self.entries.get(parent)
            if entry is None or entry["type"] != "directory":
                raise MetadataError(f"explorer directory does not exist: {parent!r}")
        if mode not in {"all", "changed-first", "changed-only"}:
            raise MetadataError("explorer mode must be all, changed-first, or changed-only")
        if cursor < 0:
            raise MetadataError("explorer cursor must not be negative")
        if not 1 <= limit <= 500:
            raise MetadataError("explorer page limit must be between 1 and 500")
        values = [dict(self.entries[path]) for path in self.children.get(parent, ())]
        if not show_ignored:
            for item in values:
                item["changed_count"] = item["visible_changed_count"]
        if not show_ignored:
            values = [item for item in values if not item["ignored"]]
        if mode == "changed-only":
            values = [item for item in values if item["changed_count"] > 0]

        def alphabetical(item: dict[str, Any]) -> tuple[int, str, str]:
            return (
                0 if item["type"] == "directory" else 1,
                str(item["name"]).casefold(),
                str(item["name"]),
            )

        if mode == "changed-first":
            values.sort(key=lambda item: (0 if item["changed_count"] else 1, *alphabetical(item)))
        else:
            values.sort(key=alphabetical)
        page = values[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "parent": parent,
            "mode": mode,
            "show_ignored": show_ignored,
            "items": page,
            "next_cursor": next_cursor if next_cursor < len(values) else None,
            "total": len(values),
        }

    def reproject(
        self,
        selectors: tuple[IgnoreSelector, ...],
        *,
        context_fingerprint: str | None = None,
        source_generations: SourceGenerations | None = None,
    ) -> ExplorerSnapshot:
        entries = {path: dict(entry) for path, entry in self.entries.items()}
        for path, entry in entries.items():
            entry["ignored"] = any(selector.contains(path) for selector in selectors)
        _aggregate(entries, self.children)
        return ExplorerSnapshot(
            entries,
            self.children,
            self.scan,
            self.delta_items,
            selectors,
            context_fingerprint or self.context_fingerprint,
            self.context_verified,
            self.auto_payload_epochs,
            self.source_generations if source_generations is None else source_generations,
        )


def root_generation(root: Path) -> tuple[int, int, int]:
    try:
        stat = root.resolve().stat()
    except OSError as exc:
        raise MetadataError(f"cannot stat instance directory {root}: {exc}") from exc
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def scan_generation_matches(scan: ExplorerScan, desired_files: Collection[str]) -> bool:
    watched = {
        path: expected
        for path, expected in scan.entry_generations.items()
        if expected[0] == "directory" or path in desired_files
    }
    for path, expected in watched.items():
        target = scan.root if not path else scan.root.joinpath(*path.split("/"))
        try:
            current = target.stat(follow_symlinks=False)
        except OSError:
            return False
        if (expected[0], current.st_mtime_ns, current.st_size, current.st_ino) != expected:
            return False
    return True


def scan_instance(root: Path) -> ExplorerScan:
    root = root.resolve()
    entries: dict[str, dict[str, Any]] = {}
    children: dict[str, set[str]] = {"": set()}
    files: set[str] = set()
    generations: dict[str, tuple[str, int, int, int]] = {}

    root_stat = root.stat()
    generations[""] = (
        "directory",
        root_stat.st_mtime_ns,
        root_stat.st_size,
        root_stat.st_ino,
    )

    def add(path: str, entry_type: str) -> None:
        normalized = normalize_relative_path(path)
        entries[normalized] = {
            "path": normalized,
            "name": PurePosixPath(normalized).name,
            "type": entry_type,
            "actual": True,
            "synthetic": False,
            "actionable": entry_type in {"file", "directory"},
            "reason": "",
        }
        parent = PurePosixPath(normalized).parent.as_posix()
        parent = "" if parent == "." else parent
        children.setdefault(parent, set()).add(normalized)
        children.setdefault(normalized, set())

    def scan(directory: Path, relative: str) -> None:
        try:
            values = list(os.scandir(directory))
        except OSError as exc:
            raise MetadataError(f"cannot list instance directory {directory}: {exc}") from exc
        for item in values:
            path = f"{relative}/{item.name}" if relative else item.name
            normalized = normalize_relative_path(path)
            if item.is_symlink():
                add(normalized, "symlink")
                entry_type = "symlink"
                entries[normalized].update(
                    actionable=False,
                    reason="symbolic links are visible but are never followed or editable",
                )
            elif item.is_dir(follow_symlinks=False):
                add(normalized, "directory")
                entry_type = "directory"
                scan(Path(item.path), normalized)
            elif item.is_file(follow_symlinks=False):
                add(normalized, "file")
                entry_type = "file"
                files.add(normalized)
            else:
                add(normalized, "special")
                entry_type = "special"
                entries[normalized].update(
                    actionable=False,
                    reason="special filesystem entries are read-only",
                )
            item_stat = Path(item.path).stat(follow_symlinks=False)
            generations[normalized] = (
                entry_type,
                item_stat.st_mtime_ns,
                item_stat.st_size,
                item_stat.st_ino,
            )

    scan(root, "")
    return ExplorerScan(
        root,
        entries,
        {key: tuple(value) for key, value in children.items()},
        frozenset(files),
        root_generation(root),
        generations,
    )


def build_snapshot(
    root: Path,
    desired_files: dict[str, bytes],
    delta_items: list[dict[str, Any]],
    selectors: tuple[IgnoreSelector, ...],
    *,
    scan: ExplorerScan | None = None,
    context_fingerprint: str = "",
    context_verified: bool = True,
    auto_payload_epochs: tuple[tuple[str, str | None], ...] = (),
    source_generations: SourceGenerations = (),
) -> ExplorerSnapshot:
    root = root.resolve()
    physical = scan or scan_instance(root)
    entries = {path: dict(entry) for path, entry in physical.entries.items()}
    children: dict[str, set[str]] = {
        path: set(values) for path, values in physical.children.items()
    }

    for path, entry in entries.items():
        entry.update(
            desired=path in desired_files,
            ignored=any(selector.contains(path) for selector in selectors),
            status="unchanged",
            delta_id=None,
            capture_modes=[],
            provider_candidates=[],
            changed=False,
            changed_count=0,
            visible_changed_count=0,
        )

    def add(path: str, entry_type: str, *, actual: bool, synthetic: bool = False) -> None:
        normalized = normalize_relative_path(path)
        existing = entries.get(normalized)
        if existing is None:
            entries[normalized] = {
                "path": normalized,
                "name": PurePosixPath(normalized).name,
                "type": entry_type,
                "actual": actual,
                "desired": normalized in desired_files,
                "synthetic": synthetic,
                "ignored": any(selector.contains(normalized) for selector in selectors),
                "status": "unchanged",
                "delta_id": None,
                "capture_modes": [],
                "provider_candidates": [],
                "actionable": entry_type in {"file", "directory"},
                "reason": "",
                "changed": False,
                "changed_count": 0,
                "visible_changed_count": 0,
            }
        elif existing["type"] != entry_type and actual:
            existing.update(
                {
                    "type": "conflict",
                    "actual": True,
                    "status": "type-conflict",
                    "changed": True,
                    "actionable": False,
                    "reason": "actual and desired entry types conflict",
                }
            )
        parent = PurePosixPath(normalized).parent.as_posix()
        parent = "" if parent == "." else parent
        children.setdefault(parent, set()).add(normalized)
        children.setdefault(normalized, set())

    for desired in sorted(desired_files):
        parts = PurePosixPath(desired).parts
        for index in range(1, len(parts)):
            add("/".join(parts[:index]), "directory", actual=False, synthetic=True)
        add(desired, "file", actual=False, synthetic=desired not in entries)
        entries[desired]["desired"] = True

    by_path: dict[str, dict[str, Any]] = {}
    for item in delta_items:
        path = normalize_relative_path(str(item["path"]))
        by_path[path] = item
        if item.get("status") == "rename" and item.get("to_path"):
            target = normalize_relative_path(str(item["to_path"]))
            by_path[target] = item
    for path, item in by_path.items():
        delta_entry = entries.get(path)
        if delta_entry is None:
            parts = PurePosixPath(path).parts
            for index in range(1, len(parts)):
                add("/".join(parts[:index]), "directory", actual=False, synthetic=True)
            add(path, "file", actual=False, synthetic=True)
            delta_entry = entries[path]
        delta_entry.update(
            status=item["status"],
            delta_id=item["id"],
            capture_modes=list(item.get("capture_modes", [])),
            provider_candidates=list(item.get("provider_candidates", [])),
            changed=True,
            actionable=delta_entry["type"] in {"file", "directory"},
        )

    # A real directory where a desired file should be, or a real file in the
    # middle of a desired path, cannot be captured safely.
    for desired in desired_files:
        parts = PurePosixPath(desired).parts
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor in entries and entries[ancestor]["type"] not in {"directory"}:
                entries[ancestor].update(
                    status="type-conflict",
                    changed=True,
                    actionable=False,
                    reason="a non-directory blocks a desired descendant",
                )
                entries[desired].update(
                    status="type-conflict",
                    changed=True,
                    actionable=False,
                    reason=f"desired path is blocked by {ancestor!r}",
                )
        if (
            desired in entries
            and entries[desired]["actual"]
            and entries[desired]["type"] == "directory"
        ):
            entries[desired].update(
                type="conflict",
                status="type-conflict",
                changed=True,
                actionable=False,
                reason="a directory exists where a desired file is required",
            )

    projected_children = {key: tuple(value) for key, value in children.items()}
    _aggregate(entries, projected_children)
    return ExplorerSnapshot(
        entries,
        projected_children,
        physical,
        tuple(dict(item) for item in delta_items),
        selectors,
        context_fingerprint,
        context_verified,
        auto_payload_epochs,
        source_generations,
    )


def _aggregate(entries: dict[str, dict[str, Any]], children: dict[str, tuple[str, ...]]) -> None:
    # Aggregate unique delta identities so a rename shown in two folders is
    # counted and captured once.
    aggregate_ids: dict[str, set[str]] = {path: set() for path in entries}
    visible_aggregate_ids: dict[str, set[str]] = {path: set() for path in entries}
    for path in sorted(entries, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        entry = entries[path]
        identity = entry["delta_id"]
        if identity:
            aggregate_ids[path].add(str(identity))
            if not entry["ignored"]:
                visible_aggregate_ids[path].add(str(identity))
        elif entry["changed"]:
            aggregate_ids[path].add(f"conflict:{path}")
            if not entry["ignored"]:
                visible_aggregate_ids[path].add(f"conflict:{path}")
        for child in children.get(path, ()):
            aggregate_ids[path].update(aggregate_ids[child])
            if not entries[child]["ignored"]:
                visible_aggregate_ids[path].update(visible_aggregate_ids[child])
        entry["changed_count"] = len(aggregate_ids[path])
        entry["visible_changed_count"] = len(visible_aggregate_ids[path])
    for top in children[""]:
        if entries[top]["type"] == "directory":
            entries[top]["changed_count"] = len(aggregate_ids[top])
            entries[top]["visible_changed_count"] = len(visible_aggregate_ids[top])
