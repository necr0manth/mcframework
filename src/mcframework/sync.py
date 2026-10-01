"""Stateless desired-state synchronization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import TransportError
from .model import Activation, PackageDeclaration
from .paths import matches
from .transport import InstanceTransport


@dataclass(frozen=True)
class SyncReport:
    created: tuple[str, ...]
    updated: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: tuple[str, ...]


class Synchronizer:
    def sync(
        self,
        desired: Mapping[str, bytes],
        transport: InstanceTransport,
        prune: tuple[str, ...] = (),
        preserve: tuple[str, ...] = (),
    ) -> SyncReport:
        existing = set(transport.list_files())
        preserved = set(preserve)
        # Validate every managed path before applying the first mutation. Local
        # transports use this to reject leaf and intermediate symlinks.
        for path in sorted(desired):
            transport.stat(path)
        created: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        disabled_aliases = tuple(
            sorted(path for path in preserved if path.casefold().endswith(".jar.disabled"))
        )
        active_disabled_sources = tuple(
            (alias[: -len(".disabled")], alias)
            for alias in disabled_aliases
            if alias[: -len(".disabled")] in existing
        )
        conflict = next(
            (alias for _source, alias in active_disabled_sources if alias in existing), None
        )
        if conflict is not None:
            raise TransportError(
                f"disabled package conflict: both {conflict[: -len('.disabled')]!r} "
                f"and {conflict!r} exist"
            )
        for source, alias in active_disabled_sources:
            transport.stat(source)
            transport.stat(alias)
        for source, alias in active_disabled_sources:
            transport.move_or_replace(source, alias)
            existing.discard(source)
            existing.add(alias)
            created.append(alias)
        for path in sorted(desired):
            if path not in existing:
                transport.write(path, desired[path])
                created.append(path)
            elif transport.read(path) != desired[path]:
                transport.write(path, desired[path])
                updated.append(path)
            else:
                unchanged.append(path)
        deleted: list[str] = []
        for path in sorted(existing - set(desired)):
            if path not in preserved and any(matches(path, pattern) for pattern in prune):
                transport.delete(path)
                deleted.append(path)
        return SyncReport(tuple(created), tuple(updated), tuple(deleted), tuple(unchanged))


def disabled_package_aliases(
    packages: Mapping[str, PackageDeclaration],
) -> tuple[str, ...]:
    """Return exact preserved paths for inactive mods with known JAR destinations."""
    return tuple(
        sorted(
            f"{package.destination}.disabled"
            for package in packages.values()
            if package.kind == "mod"
            and package.activation in {Activation.AVAILABLE, Activation.DISABLED}
            and package.destination.casefold().endswith(".jar")
        )
    )
