"""Stateless desired-state synchronization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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
    ) -> SyncReport:
        existing = set(transport.list_files())
        # Validate every managed path before applying the first mutation. Local
        # transports use this to reject leaf and intermediate symlinks.
        for path in sorted(desired):
            transport.stat(path)
        created: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
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
            if any(matches(path, pattern) for pattern in prune):
                transport.delete(path)
                deleted.append(path)
        return SyncReport(tuple(created), tuple(updated), tuple(deleted), tuple(unchanged))
