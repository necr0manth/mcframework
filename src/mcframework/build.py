"""Resolution materialization into a clean local directory."""

from __future__ import annotations

from pathlib import Path

from .artifacts import ArtifactResolver
from .manifest import materialized_files
from .model import Resolution
from .sync import Synchronizer, SyncReport
from .transport import LocalFilesystemInstance


class Builder:
    def __init__(self, artifacts: ArtifactResolver):
        self.artifacts = artifacts

    def prepare(self, resolution: Resolution) -> tuple[Resolution, dict[str, bytes]]:
        resolved = self.artifacts.resolve(resolution)
        resolved, files = materialized_files(resolved)
        return resolved, dict(files)

    def build(self, resolution: Resolution, output: Path | str) -> tuple[Resolution, SyncReport]:
        resolved, files = self.prepare(resolution)
        report = Synchronizer().sync(files, LocalFilesystemInstance(output), ("**",))
        return resolved, report
