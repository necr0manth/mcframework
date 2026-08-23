"""Command-line entry point for the initial core workflow."""

from __future__ import annotations

import argparse
import json
import sys
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
from .errors import FrameworkError, MetadataError
from .loader import WorkspaceLoader, validate_parameter_value
from .manifest import manifest_document
from .resolver import Resolver
from .sync import Synchronizer
from .transport import LocalFilesystemInstance, SftpInstance
from .update import UpdateService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mcpack")
    subcommands = result.add_subparsers(dest="command", required=True)
    for name in ("resolve", "build", "sync", "update"):
        command = subcommands.add_parser(name)
        command.add_argument("workspace", type=Path)
        command.add_argument("layer")
        command.add_argument("--cache", type=Path)
        command.add_argument("--parameters", default="{}", help="JSON object")
        command.add_argument("--no-auto-dependencies", action="store_true")
        if name == "build":
            command.add_argument("output", type=Path)
        elif name == "sync":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--local", type=Path)
            target.add_argument("--sftp-config", type=Path)
        elif name == "update":
            command.add_argument("package_ids", nargs="+")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        workspace = arguments.workspace.resolve()
        parameters = parse_parameters(arguments.parameters)
        loader = WorkspaceLoader(workspace)
        resolution = Resolver(
            loader, automatic_dependencies=not arguments.no_auto_dependencies
        ).resolve(arguments.layer, parameters)
        cache_path = arguments.cache or workspace / ".mcpack-cache"
        registry = default_registry(workspace)
        artifacts = ArtifactResolver(registry, ArtifactCache(cache_path))
        builder = Builder(artifacts)
        if arguments.command == "resolve":
            prepared, _ = builder.prepare(resolution)
            print(json.dumps(manifest_document(prepared), indent=2, sort_keys=True))
        elif arguments.command == "build":
            prepared, report = builder.build(resolution, arguments.output)
            print_report(report)
            print(f"manifest files: {len(prepared.manifest)}")
        elif arguments.command == "sync":
            _, files = builder.prepare(resolution)
            transport = make_transport(arguments)
            try:
                report = Synchronizer().sync(files, transport, resolution.state.prune)
            finally:
                close = getattr(transport, "close", None)
                if close is not None:
                    close()
            print_report(report)
        elif arguments.command == "update":
            service = UpdateService(registry, ArtifactCache(cache_path))
            for package_id in arguments.package_ids:
                package = resolution.state.packages.get(package_id)
                if package is None:
                    raise MetadataError(f"package is not declared: {package_id!r}")
                result = service.update(package)
                print(f"{package_id}: {'updated' if result.changed else 'already current'}")
        return 0
    except FrameworkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def parse_parameters(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MetadataError(f"parameters are not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MetadataError("parameters must be a JSON object")
    validate_parameter_value(value)
    return value


def default_registry(workspace: Path) -> ArtifactRegistry:
    registry = ArtifactRegistry()
    registry.register("local", LocalArtifactProvider(workspace))
    registry.register("direct_url", DirectUrlArtifactProvider())
    return registry


def make_transport(arguments: argparse.Namespace) -> LocalFilesystemInstance | SftpInstance:
    if arguments.local is not None:
        return LocalFilesystemInstance(arguments.local)
    try:
        config = json.loads(arguments.sftp_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataError(f"cannot read SFTP config: {exc}") from exc
    if not isinstance(config, dict):
        raise MetadataError("SFTP config must be a JSON object")
    try:
        return SftpInstance.connect(**config)
    except TypeError as exc:
        raise MetadataError(f"invalid SFTP config keys: {exc}") from exc


def print_report(report: Any) -> None:
    print(
        f"created={len(report.created)} updated={len(report.updated)} "
        f"deleted={len(report.deleted)} unchanged={len(report.unchanged)}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
