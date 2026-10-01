"""Modrinth project-version resolver and exact artifact downloader."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from .artifacts import PackageSelection, PackageUpdate
from .errors import ArtifactError
from .model import PackageDeclaration
from .provider_common import normalize_loader, object_list, read_bytes, read_json


class ModrinthPackageProvider:
    versions_endpoint = "https://api.modrinth.com/v2/project/{project}/version"

    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout

    def select(self, package: PackageDeclaration) -> PackageSelection:
        if not package.minecraft or not package.loader:
            raise ArtifactError(
                f"package {package.identity!r} needs Minecraft and loader context for Modrinth"
            )
        project = urllib.parse.quote(package.source.location, safe="")
        query = urllib.parse.urlencode(
            {
                "game_versions": json.dumps([package.minecraft]),
                "loaders": json.dumps([package.loader]),
            }
        )
        raw = read_json(
            self.versions_endpoint.format(project=project) + "?" + query,
            timeout=self.timeout,
        )
        versions = object_list(raw, "version list")
        matches = [
            item for item in versions if _compatible(item, package.minecraft, package.loader)
        ]
        if package.version:
            matches = [item for item in matches if item.get("version_number") == package.version]
            if len(matches) != 1:
                raise ArtifactError(
                    f"Modrinth version {package.version!r} is not uniquely compatible for "
                    f"package {package.identity!r}"
                )
            version = matches[0]
        else:
            if not matches:
                raise ArtifactError(f"no compatible Modrinth version for {package.identity!r}")
            matches.sort(
                key=lambda item: (
                    _timestamp(item.get("date_published")),
                    str(item.get("version_number", "")),
                    str(item.get("id", "")),
                ),
                reverse=True,
            )
            version = matches[0]
        files = object_list(version.get("files"), "version files")
        primary = [item for item in files if item.get("primary") is True]
        if len(primary) == 1:
            selected = primary[0]
        elif not primary and len(files) == 1:
            selected = files[0]
        else:
            raise ArtifactError(f"Modrinth version file is ambiguous for {package.identity!r}")
        number = version.get("version_number")
        filename = selected.get("filename")
        url = selected.get("url")
        if (
            not isinstance(number, str)
            or not number
            or not isinstance(filename, str)
            or not filename
        ):
            raise ArtifactError(
                f"Modrinth returned incomplete version data for {package.identity!r}"
            )
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ArtifactError(
                f"Modrinth returned an invalid download URL for {package.identity!r}"
            )
        hashes = selected.get("hashes")
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        dependencies = version.get("dependencies", [])
        requires = tuple(
            sorted(
                {
                    item["project_id"]
                    for item in dependencies
                    if isinstance(item, dict)
                    and item.get("dependency_type") == "required"
                    and isinstance(item.get("project_id"), str)
                }
            )
        )
        return PackageSelection(
            number,
            url,
            filename,
            sha256 if isinstance(sha256, str) else None,
            (
                None
                if package.logical_identity_explicit
                else str(version.get("project_id") or package.logical_identity)
            ),
            None if package.automatic_requires_explicit else requires,
        )

    def fetch(self, package: PackageDeclaration) -> bytes:
        if package.artifact_url is None:
            raise ArtifactError(f"Modrinth package {package.identity!r} was not selected")
        return read_bytes(package.artifact_url, timeout=self.timeout)

    def find_update(self, package: PackageDeclaration) -> PackageUpdate | None:
        latest = ModrinthPackageProvider(timeout=self.timeout).select(
            replace_version_empty(package)
        )
        if latest.selector == package.version:
            return None
        return PackageUpdate(
            package.source,
            latest.selector,
            latest.download_url,
            latest.filename,
            latest.sha256,
            latest.logical_identity,
            latest.automatic_requires,
        )


def replace_version_empty(package: PackageDeclaration) -> PackageDeclaration:
    from dataclasses import replace

    return replace(package, version=None)


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _compatible(item: dict[str, Any], minecraft: str, loader: str) -> bool:
    game_versions = item.get("game_versions")
    loaders = item.get("loaders")
    return (
        isinstance(game_versions, list)
        and minecraft in game_versions
        and isinstance(loaders, list)
        and any(
            isinstance(value, str) and normalize_loader(value) == normalize_loader(loader)
            for value in loaders
        )
    )
