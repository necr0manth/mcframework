"""CurseForge project/file resolver and exact artifact downloader."""

from __future__ import annotations

import os
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from .artifacts import PackageSelection, PackageUpdate
from .errors import ArtifactError
from .model import PackageDeclaration
from .provider_common import normalize_loader, object_list, read_bytes, read_json


class CurseForgePackageProvider:
    search_endpoint = "https://api.curseforge.com/v1/mods/search"
    files_endpoint = "https://api.curseforge.com/v1/mods/{mod_id}/files"
    download_endpoint = "https://api.curseforge.com/v1/mods/{mod_id}/files/{file_id}/download-url"

    def __init__(self, api_key: str | None = None, *, timeout: float = 30.0):
        self.api_key = api_key if api_key is not None else os.getenv("CURSEFORGE_API_KEY", "")
        self.timeout = timeout

    def select(self, package: PackageDeclaration) -> PackageSelection:
        if not self.api_key.strip():
            raise ArtifactError("CURSEFORGE_API_KEY is required for CurseForge packages")
        if not package.minecraft or not package.loader:
            raise ArtifactError(
                f"package {package.identity!r} needs Minecraft and loader context for CurseForge"
            )
        mod_id = self._project_id(package.source.location)
        files = [item for item in self._files(mod_id) if item.get("isAvailable", True) is True]
        applicable = [
            item for item in files if _compatible(item, package.minecraft, package.loader)
        ]
        selector = package.version or ""
        if selector.isdecimal():
            matches = [item for item in applicable if str(item.get("id")) == selector]
        elif selector:
            matches = [item for item in applicable if item.get("fileName") == selector]
        else:
            matches = applicable
        if selector and len(matches) != 1:
            raise ArtifactError(
                f"CurseForge file {selector!r} is not uniquely compatible for "
                f"package {package.identity!r}"
            )
        if not matches:
            raise ArtifactError(f"no compatible CurseForge file for {package.identity!r}")
        matches.sort(
            key=lambda item: (_timestamp(item.get("fileDate")), str(item.get("id", ""))),
            reverse=True,
        )
        selected = matches[0]
        file_id = selected.get("id")
        filename = selected.get("fileName")
        url = selected.get("downloadUrl")
        if not isinstance(file_id, (str, int)) or not isinstance(filename, str) or not filename:
            raise ArtifactError(
                f"CurseForge returned incomplete file data for {package.identity!r}"
            )
        if not isinstance(url, str) or not url.startswith("https://"):
            url = self._download_url(mod_id, str(file_id))
        if url is None:
            raise ArtifactError(
                f"CurseForge did not provide a download URL for {package.identity!r}"
            )
        requires = tuple(
            sorted(
                {
                    str(item["modId"])
                    for item in selected.get("dependencies", [])
                    if isinstance(item, dict)
                    and item.get("relationType") == 3
                    and isinstance(item.get("modId"), (str, int))
                }
            )
        )
        selected_selector = selector or str(file_id)
        return PackageSelection(
            selected_selector,
            url,
            filename,
            None,
            None if package.logical_identity_explicit else str(mod_id),
            None if package.automatic_requires_explicit else requires,
        )

    def fetch(self, package: PackageDeclaration) -> bytes:
        if package.artifact_url is None:
            raise ArtifactError(f"CurseForge package {package.identity!r} was not selected")
        return read_bytes(package.artifact_url, timeout=self.timeout)

    def find_update(self, package: PackageDeclaration) -> PackageUpdate | None:
        from dataclasses import replace

        latest = self.select(replace(package, version=None))
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

    def _project_id(self, value: str) -> int:
        if value.isdecimal():
            return int(value)
        query = urllib.parse.urlencode({"gameId": 432, "classId": 6, "slug": value, "pageSize": 50})
        raw = read_json(
            self.search_endpoint + "?" + query,
            timeout=self.timeout,
            headers={"x-api-key": self.api_key},
        )
        data = raw.get("data") if isinstance(raw, dict) else None
        mods = object_list(data, "project search")
        matches = [item for item in mods if item.get("slug") == value]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise ArtifactError(f"CurseForge project slug {value!r} is not unique")
        return int(matches[0]["id"])

    def _files(self, mod_id: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        index = 0
        while True:
            query = urllib.parse.urlencode({"index": index, "pageSize": 50})
            raw = read_json(
                self.files_endpoint.format(mod_id=mod_id) + "?" + query,
                timeout=self.timeout,
                headers={"x-api-key": self.api_key},
            )
            data = raw.get("data") if isinstance(raw, dict) else None
            page = object_list(data, "project files")
            items.extend(page)
            pagination = raw.get("pagination") if isinstance(raw, dict) else None
            total = pagination.get("totalCount") if isinstance(pagination, dict) else None
            if not page or len(page) < 50 or (isinstance(total, int) and len(items) >= total):
                return items
            index += len(page)

    def _download_url(self, mod_id: int, file_id: str) -> str | None:
        url = self.download_endpoint.format(
            mod_id=mod_id, file_id=urllib.parse.quote(file_id, safe="")
        )
        raw = read_json(url, timeout=self.timeout, headers={"x-api-key": self.api_key})
        data = raw.get("data") if isinstance(raw, dict) else None
        return data if isinstance(data, str) and data.startswith("https://") else None


def _compatible(file: dict[str, Any], minecraft: str, loader: str) -> bool:
    versions = file.get("gameVersions")
    if not isinstance(versions, list) or minecraft not in versions:
        return False
    wanted = normalize_loader(loader)
    return any(isinstance(item, str) and normalize_loader(item) == wanted for item in versions)


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
