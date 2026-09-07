"""Command-line entry point for project-root workflows."""

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
from .project import ProjectService
from .resolver import LayerComposer
from .sync import Synchronizer
from .transport import LocalFilesystemInstance, SftpInstance
from .update import UpdateService
from .web_process import start_web, stop_web


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mcpack")
    subcommands = result.add_subparsers(dest="command", required=True)
    init = subcommands.add_parser("init")
    init.add_argument("layer")
    resolve = subcommands.add_parser("resolve")
    _branch_arguments(resolve)
    build = subcommands.add_parser("build")
    _branch_arguments(build)
    build.add_argument("-o", "--output", type=Path)
    update = subcommands.add_parser("update")
    _branch_arguments(update)
    update.add_argument("package_ids", nargs="+")
    sync = subcommands.add_parser("sync")
    sync.add_argument("instance")
    create = subcommands.add_parser("createinstance")
    create.add_argument("instance")
    create.add_argument("path", type=Path)
    link = subcommands.add_parser("link")
    link.add_argument("branch")
    link.add_argument("instance")
    link.add_argument("-p", "--path", type=Path)
    unlink = subcommands.add_parser("unlink")
    unlink.add_argument("instance")
    rename = subcommands.add_parser("renameinstance")
    rename.add_argument("instance")
    rename.add_argument("new_name")
    delete = subcommands.add_parser("deleteinstance")
    delete.add_argument("instance")
    feature = subcommands.add_parser("feature")
    feature.add_argument("action", choices=("add", "remove", "list"))
    feature.add_argument("instance")
    feature.add_argument("feature", nargs="?")
    web = subcommands.add_parser("web")
    web.add_argument("target", help="instance name, or 'stop'")
    web.add_argument("instance", nargs="?")
    diff = subcommands.add_parser("diff")
    diff.add_argument("instance")
    explain = subcommands.add_parser("explain")
    explain.add_argument("instance")
    explain.add_argument("target", nargs="?")
    import_command = subcommands.add_parser("import")
    import_command.add_argument("instance")
    import_command.add_argument("delta_id", nargs="?")
    import_command.add_argument("--target-layer")
    import_command.add_argument("--provider-match", choices=("modrinth", "local"), default="local")
    return result


def _branch_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("branch")
    command.add_argument("--parameters", default="{}", help="JSON object")
    command.add_argument("--no-auto-dependencies", action="store_true")


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    try:
        legacy = _legacy_invocation(raw)
        if legacy is not None:
            return legacy
        arguments = parser().parse_args(raw)
        service = ProjectService(Path.cwd())
        if arguments.command == "init":
            root = service.init_layer(arguments.layer)
            print(f"initialized layer {arguments.layer}: {root}")
        elif arguments.command in {"resolve", "build", "update"}:
            parameters = parse_parameters(arguments.parameters)
            resolution = service.resolve_branch(
                arguments.branch,
                parameters,
                automatic_dependencies=not arguments.no_auto_dependencies,
            )
            registry, cache, builder = service.artifact_components()
            if arguments.command == "resolve":
                prepared, _ = builder.prepare(resolution)
                print(json.dumps(manifest_document(prepared), indent=2, sort_keys=True))
            elif arguments.command == "build":
                output = service.safe_build_output(arguments.branch, arguments.output)
                prepared, report = builder.build(resolution, output)
                print_report(report)
                print(f"manifest files: {len(prepared.manifest)}")
                print(f"output: {output}")
            else:
                update_service = UpdateService(registry, cache)
                for package_id in arguments.package_ids:
                    package = resolution.state.packages.get(package_id)
                    if package is None:
                        raise MetadataError(f"package is not declared: {package_id!r}")
                    update_result = update_service.update(package)
                    print(
                        f"{package_id}: {'updated' if update_result.changed else 'already current'}"
                    )
        elif arguments.command == "sync":
            context = service.instance_context(arguments.instance)
            report = Synchronizer().sync(
                context.files,
                LocalFilesystemInstance(context.instance.path),
                context.resolution.state.content.prune,
            )
            print_report(report)
        elif arguments.command == "createinstance":
            record = service.create_instance(arguments.instance, arguments.path)
            print(f"created instance {record.name}: {record.path}")
        elif arguments.command == "link":
            link = service.link(arguments.branch, arguments.instance, arguments.path)
            print(f"linked {link.instance} -> {link.effective_layer}")
        elif arguments.command == "unlink":
            service.unlink(arguments.instance)
            print(f"unlinked {arguments.instance}")
        elif arguments.command == "renameinstance":
            service.rename_instance(arguments.instance, arguments.new_name)
            print(f"renamed {arguments.instance} -> {arguments.new_name}")
        elif arguments.command == "deleteinstance":
            service.delete_instance(arguments.instance)
            print(
                f"deleted instance registration {arguments.instance}; game files were not deleted"
            )
        elif arguments.command == "feature":
            if arguments.action == "list":
                context = service.instance_context(arguments.instance, prepare=False)
                print("\n".join(context.features))
            else:
                if arguments.feature is None:
                    raise MetadataError("feature name is required for add/remove")
                if arguments.action == "add":
                    service.add_feature(arguments.instance, arguments.feature)
                else:
                    service.remove_feature(arguments.instance, arguments.feature)
                print(f"{arguments.action} feature {arguments.feature} for {arguments.instance}")
        elif arguments.command == "web":
            if arguments.target == "stop":
                if arguments.instance is None:
                    raise MetadataError("usage: mcpack web stop <instance>")
                stopped = stop_web(service.project, arguments.instance)
                print("stopped" if stopped else "already stopped")
            else:
                if arguments.instance is not None:
                    raise MetadataError("usage: mcpack web <instance>")
                service.instance_context(arguments.target, prepare=False)
                url = start_web(service.project, arguments.target)
                print(url)
                print(f"stop with: mcpack web stop {arguments.target}")
        elif arguments.command in {"diff", "explain"}:
            context = service.instance_context(arguments.instance)
            document = service.instance_diff(context)
            if arguments.command == "explain":
                if arguments.target is not None:
                    document = service.explain(arguments.instance, arguments.target)
                else:
                    document = {
                        "instance": arguments.instance,
                        "branch": context.link.branch,
                        "effective_layer": context.link.effective_layer,
                        "features": list(context.features),
                        "layers": [item.layer for item in context.resolution.trace],
                        "prune": list(context.resolution.state.prune),
                        "ignore": [
                            selector.document() for selector in context.resolution.state.ignore
                        ],
                        "delta": document,
                    }
            print(json.dumps(document, indent=2, sort_keys=True))
        elif arguments.command == "import":
            context = service.instance_context(arguments.instance)
            items = service.instance_diff(context)["items"]
            if arguments.delta_id is not None:
                items = [item for item in items if item["id"] == arguments.delta_id]
                if not items:
                    raise MetadataError(
                        f"instance delta item does not exist: {arguments.delta_id!r}"
                    )
            imported = 0
            for item in items:
                if item["status"] == "remove":
                    continue
                if item["path"].lower().endswith(".jar") and item.get("package_id") is None:
                    service.import_package(
                        arguments.instance,
                        item["id"],
                        arguments.target_layer,
                        arguments.provider_match,
                    )
                else:
                    service.capture_delta(
                        arguments.instance,
                        item["id"],
                        arguments.target_layer,
                        rename_to_path=item.get("to_path"),
                    )
                imported += 1
            print(f"imported={imported}")
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


def _legacy_invocation(arguments: list[str]) -> int | None:
    """Keep the 0.1 positional form usable while the project-root CLI rolls out."""
    if len(arguments) < 3 or arguments[0] not in {"resolve", "build", "sync", "update"}:
        return None
    workspace = Path(arguments[1]).resolve()
    if not workspace.is_dir() or not (workspace / arguments[2]).is_dir():
        return None
    command, layer = arguments[0], arguments[2]
    tail = arguments[3:]
    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("--cache", type=Path)
    legacy.add_argument("--parameters", default="{}")
    legacy.add_argument("--no-auto-dependencies", action="store_true")
    if command == "build":
        legacy.add_argument("output", type=Path)
    elif command == "sync":
        target = legacy.add_mutually_exclusive_group(required=True)
        target.add_argument("--local", type=Path)
        target.add_argument("--sftp-config", type=Path)
    elif command == "update":
        legacy.add_argument("package_ids", nargs="+")
    parsed = legacy.parse_args(tail)
    resolution = LayerComposer(
        WorkspaceLoader(workspace), automatic_dependencies=not parsed.no_auto_dependencies
    ).resolve(layer, parse_parameters(parsed.parameters))
    cache_path = parsed.cache or workspace / ".mcpack" / "cache"
    registry = default_registry(workspace)
    builder = Builder(ArtifactResolver(registry, ArtifactCache(cache_path)))
    if command == "resolve":
        prepared, _ = builder.prepare(resolution)
        print(json.dumps(manifest_document(prepared), indent=2, sort_keys=True))
    elif command == "build":
        prepared, report = builder.build(resolution, parsed.output)
        print_report(report)
        print(f"manifest files: {len(prepared.manifest)}")
    elif command == "sync":
        _, files = builder.prepare(resolution)
        transport = make_transport(parsed)
        try:
            print_report(Synchronizer().sync(files, transport, resolution.state.prune))
        finally:
            close = getattr(transport, "close", None)
            if close is not None:
                close()
    else:
        service = UpdateService(registry, ArtifactCache(cache_path))
        for package_id in parsed.package_ids:
            package = resolution.state.packages.get(package_id)
            if package is None:
                raise MetadataError(f"package is not declared: {package_id!r}")
            update = service.update(package)
            print(f"{package_id}: {'updated' if update.changed else 'already current'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
