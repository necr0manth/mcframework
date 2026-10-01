"""Explicit, provider-capability-gated metadata updates."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .artifacts import ArtifactCache, ArtifactRegistry, PackageSelection, explicit_update
from .errors import UpdateError
from .mcpack import McpackParser, pin_provider_selector
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
        provider = self.registry.provider(updated.source.provider)
        if updated.source.sha256:
            artifact = self.cache.obtain(updated, provider)
        else:
            if not updated.artifact_url:
                raise UpdateError("update provider did not return an exact download URL")
            artifact = self.cache.store(updated, provider.fetch(updated))
            updated = replace(
                updated,
                source=replace(updated.source, sha256=artifact.sha256),
            )
        if updated.version and updated.artifact_url and updated.filename:
            self.cache.store_selection(
                updated,
                PackageSelection(
                    updated.version,
                    updated.artifact_url,
                    updated.filename,
                    artifact.sha256,
                    updated.logical_identity,
                    updated.automatic_requires,
                ),
                artifact.sha256,
            )
        self._rewrite_source(package, updated)
        return UpdateResult(updated, True)

    @staticmethod
    def _rewrite_source(old: PackageDeclaration, new: PackageDeclaration) -> None:
        if old.source_file is None or old.source_index is None:
            raise UpdateError(f"package {old.identity!r} has no editable source location")
        path = Path(old.source_file)
        if path.suffix.casefold() == ".mcpack":
            UpdateService._rewrite_mcpack_source(path, old, new)
            return
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

    @staticmethod
    def _rewrite_mcpack_source(
        path: Path, old: PackageDeclaration, new: PackageDeclaration
    ) -> None:
        if old.source_index is None or new.version is None:
            raise UpdateError(f"package {old.identity!r} has no updateable selector")
        try:
            content = path.read_bytes()
            document = McpackParser().parse(content, path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise UpdateError(f"cannot read package source for {old.identity!r}") from exc
        line = next((item for item in document.lines if item.line_number == old.source_index), None)
        record = line.record if line is not None else None
        source = record.get("source") if isinstance(record, dict) else None
        if (
            line is None
            or not isinstance(record, dict)
            or record.get("type") != "package"
            or record.get("id") != old.identity
            or not isinstance(source, dict)
            or source.get("provider") != old.source.provider
            or source.get("location") != old.source.location
            or record.get("version") != old.version
        ):
            raise UpdateError(
                f"source declaration moved or changed for {old.identity!r}; reload before update"
            )
        replacement = pin_provider_selector(line.expression, new.version)
        atomic_write(path, document.replace_expression(line.line_id, line.expression, replacement))
