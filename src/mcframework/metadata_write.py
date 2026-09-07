"""Explicit, optimistic write plans for metadata documents.

Planning is side-effect free. The workflow owns the transaction so validation of
the resulting layer occurs before the before-images are discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import MetadataError
from .paths import atomic_write, confined_path


@dataclass(frozen=True)
class MetadataWrite:
    path: Path
    before: bytes | None
    after: bytes


@dataclass(frozen=True)
class MetadataWritePlan:
    writes: tuple[MetadataWrite, ...]
    layer: Path

    def validate_paths(self) -> None:
        if self.layer.is_symlink() or self.layer.resolve() != self.layer:
            raise MetadataError(f"metadata layer changed after planning: {self.layer}")
        for write in self.writes:
            confined_path(self.layer, write.path.relative_to(self.layer).as_posix())

    def validate(self) -> None:
        self.validate_paths()
        for write in self.writes:
            actual = write.path.read_bytes() if write.path.exists() else None
            if actual != write.before:
                raise MetadataError(f"metadata changed after planning: {write.path}")

    def apply(self) -> None:
        self.validate()
        for write in self.writes:
            self.validate_paths()
            atomic_write(write.path, write.after)
