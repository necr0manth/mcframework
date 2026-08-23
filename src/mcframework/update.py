"""Explicit, provider-capability-gated metadata updates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactCache, ArtifactRegistry, explicit_update
from .errors import UpdateError
from .model import PackageDeclaration
from .paths import atomic_write


@dataclass(frozen=True)
class UpdateResult:
    package: PackageDeclaration
    changed: bool


class UpdateService:
    def __init__(self, registry: ArtifactRegistry, cache: ArtifactCache):
        self.registry = registry
        self.cache = cache

    def update(self, package: PackageDeclaration) -> UpdateResult:
        updated = explicit_update(package, self.registry)
        if updated is None:
            return UpdateResult(package, False)
        # Verify exact availability and hash before changing tracked metadata.
        self.cache.obtain(updated, self.registry.provider(updated.source.provider))
        self._rewrite_source(package, updated)
        return UpdateResult(updated, True)

    @staticmethod
    def _rewrite_source(old: PackageDeclaration, new: PackageDeclaration) -> None:
        if old.source_file is None or old.source_index is None:
            raise UpdateError(f"package {old.identity!r} has no editable source location")
        path = Path(old.source_file)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            operation = document["operations"][old.source_index]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise UpdateError(f"cannot locate source declaration for {old.identity!r}") from exc
        if operation.get("type") != "package" or operation.get("id") != old.identity:
            raise UpdateError(
                f"source declaration moved for {old.identity!r}; reload before update"
            )
        operation["source"] = {
            "provider": new.source.provider,
            "location": new.source.location,
            "sha256": new.source.sha256,
        }
        if new.version is None:
            operation.pop("version", None)
        else:
            operation["version"] = new.version
        atomic_write(
            path,
            (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
