"""Bounded local JAR metadata parsing and exact provider lookup adapters."""

from __future__ import annotations

import hashlib
import json
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import MetadataError

MAX_METADATA_BYTES = 2 * 1024 * 1024
USER_AGENT = "minecraft-pack-framework/0.1 (+https://github.com/)"


@dataclass(frozen=True)
class JarMetadata:
    loader: str | None
    mod_ids: tuple[str, ...]
    name: str | None
    version: str | None
    dependencies: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "loader": self.loader,
            "mod_ids": list(self.mod_ids),
            "name": self.name,
            "version": self.version,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True)
class ProviderMatch:
    provider: str
    project_id: str
    version_id: str
    version: str | None
    download_url: str
    hashes: dict[str, str]
    confidence: str = "exact"
    reason: str = "provider returned the exact file hash"
    kind: str | None = None
    categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "version": self.version,
            "download_url": self.download_url,
            "hashes": dict(self.hashes),
            "confidence": self.confidence,
            "reason": self.reason,
            "kind": self.kind,
            "categories": list(self.categories),
            "tags": list(self.tags),
        }


class ProviderMatcher(Protocol):
    def match(self, content: bytes) -> ProviderMatch | None: ...


def file_hashes(content: bytes) -> dict[str, str]:
    return {
        "sha1": hashlib.sha1(content).hexdigest(),  # noqa: S324 - provider identity only
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha512": hashlib.sha512(content).hexdigest(),
    }


def inspect_jar(path: Path) -> JarMetadata | None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "fabric.mod.json" in names:
                raw = _read_json(archive, "fabric.mod.json")
                dependencies = raw.get("depends", {})
                return JarMetadata(
                    "fabric",
                    _one_string(raw.get("id")),
                    _optional_text(raw.get("name")),
                    _optional_text(raw.get("version")),
                    tuple(sorted(dependencies)) if isinstance(dependencies, dict) else (),
                )
            if "quilt.mod.json" in names:
                raw = _read_json(archive, "quilt.mod.json")
                loader = raw.get("quilt_loader", {})
                metadata = raw.get("metadata", {})
                if not isinstance(loader, dict):
                    loader = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                dependencies = loader.get("depends", [])
                return JarMetadata(
                    "quilt",
                    _one_string(loader.get("id")),
                    _optional_text(metadata.get("name")),
                    _optional_text(loader.get("version")),
                    _dependency_ids(dependencies),
                )
            for metadata_path, loader_name in (
                ("META-INF/neoforge.mods.toml", "neoforge"),
                ("META-INF/mods.toml", "forge"),
            ):
                if metadata_path in names:
                    raw = tomllib.loads(_read_bounded(archive, metadata_path).decode("utf-8"))
                    mods = raw.get("mods", [])
                    if not isinstance(mods, list):
                        mods = []
                    ids = tuple(
                        str(item["modId"])
                        for item in mods
                        if isinstance(item, dict) and isinstance(item.get("modId"), str)
                    )
                    first = mods[0] if mods and isinstance(mods[0], dict) else {}
                    dependencies = raw.get("dependencies", {})
                    dependency_ids: list[str] = []
                    if isinstance(dependencies, dict):
                        for entries in dependencies.values():
                            if isinstance(entries, list):
                                dependency_ids.extend(
                                    str(item["modId"])
                                    for item in entries
                                    if isinstance(item, dict) and isinstance(item.get("modId"), str)
                                )
                    return JarMetadata(
                        loader_name,
                        ids,
                        _optional_text(first.get("displayName")),
                        _optional_text(first.get("version")),
                        tuple(sorted(set(dependency_ids))),
                    )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        zipfile.BadZipFile,
    ):
        return None
    return None


class ModrinthMatcher:
    """Official anonymous exact-hash lookup. Network failures are a normal no-match."""

    endpoint = "https://api.modrinth.com/v2/version_file/{digest}?algorithm=sha512"
    project_endpoint = "https://api.modrinth.com/v2/project/{project_id}"
    project_cache_limit = 128

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._project_metadata: dict[str, tuple[str, tuple[str, ...]]] = {}

    def _project(self, project_id: str) -> tuple[str, tuple[str, ...]] | None:
        if project_id in self._project_metadata:
            return self._project_metadata[project_id]
        request = urllib.request.Request(
            self.project_endpoint.format(project_id=urllib.parse.quote(project_id, safe="")),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read())
            if not isinstance(raw, dict) or raw.get("id") != project_id:
                return None
            kind = raw.get("project_type")
            if not isinstance(kind, str) or kind not in (
                "mod", "resourcepack", "datapack", "modpack", "shader"
            ):
                return None
            categories = _string_list(raw.get("categories"), "categories")
            additional = _string_list(raw.get("additional_categories"), "additional_categories")
        except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError, MetadataError):
            return None  # Optional project metadata must not discard an exact file match.
        result = kind, tuple(dict.fromkeys((*categories, *additional)))
        if len(self._project_metadata) >= self.project_cache_limit:
            self._project_metadata.pop(next(iter(self._project_metadata)))
        self._project_metadata[project_id] = result
        return result

    def match(self, content: bytes) -> ProviderMatch | None:
        hashes = file_hashes(content)
        request = urllib.request.Request(
            self.endpoint.format(digest=hashes["sha512"]),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        files = raw.get("files", [])
        if not isinstance(files, list):
            return None
        exact = [
            item
            for item in files
            if isinstance(item, dict)
            and isinstance(item.get("hashes"), dict)
            and item["hashes"].get("sha512") == hashes["sha512"]
            and isinstance(item.get("url"), str)
        ]
        if not exact:
            return None
        selected = next((item for item in exact if item.get("primary") is True), exact[0])
        project_id = raw.get("project_id")
        version_id = raw.get("id")
        if not isinstance(project_id, str) or not isinstance(version_id, str):
            return None
        project = self._project(project_id)
        return ProviderMatch(
            "modrinth",
            project_id,
            version_id,
            _optional_text(raw.get("version_number")),
            str(selected["url"]),
            hashes,
            kind=project[0] if project else None,
            categories=project[1] if project else (),
            tags=project[1] if project else (),
        )


class CurseForgeFingerprintMatcher:
    """Optional official fingerprint endpoint; the caller must supply the integer fingerprint."""

    endpoint = "https://api.curseforge.com/v1/fingerprints"

    def __init__(self, api_key: str, timeout: float = 10.0):
        self.api_key = api_key
        self.timeout = timeout

    def lookup(self, fingerprint: int) -> dict[str, Any] | None:
        data = json.dumps({"fingerprints": [fingerprint]}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


def confirmed_provider_match(value: object, content: bytes) -> ProviderMatch | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MetadataError("provider_match must be an object")
    required = {"provider", "project_id", "version_id", "confidence"}
    if not required <= set(value):
        raise MetadataError("provider_match is missing required identity fields")
    confidence = value["confidence"]
    if confidence not in {"exact", "fuzzy"}:
        raise MetadataError("provider_match confidence must be exact or fuzzy")
    hashes = value.get("hashes")
    url = value.get("download_url")
    if not isinstance(hashes, dict) or not isinstance(url, str):
        raise MetadataError("confirmed provider_match needs download_url and hashes")
    actual = file_hashes(content)
    if not any(hashes.get(name) == actual[name] for name in ("sha512", "sha1", "sha256")):
        raise MetadataError("confirmed provider file hashes do not match the instance JAR")
    kind = value.get("kind")
    if kind is not None and (not isinstance(kind, str) or not kind):
        raise MetadataError("provider_match kind must be a non-empty string or null")
    categories = _string_list(value.get("categories", []), "categories")
    tags = _string_list(value.get("tags", []), "tags")
    return ProviderMatch(
        str(value["provider"]),
        str(value["project_id"]),
        str(value["version_id"]),
        _optional_text(value.get("version")),
        url,
        actual,
        str(confidence),
        "explicit user confirmation" if confidence == "fuzzy" else "confirmed exact match",
        kind,
        categories,
        tags,
    )


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise MetadataError(f"provider_match {field} must be an array of non-empty strings")
    return tuple(dict.fromkeys(value))


def _read_json(archive: zipfile.ZipFile, path: str) -> dict[str, Any]:
    value = json.loads(_read_bounded(archive, path))
    if not isinstance(value, dict):
        raise json.JSONDecodeError("metadata root must be an object", "", 0)
    return value


def _read_bounded(archive: zipfile.ZipFile, path: str) -> bytes:
    info = archive.getinfo(path)
    if info.file_size > MAX_METADATA_BYTES:
        raise OSError("JAR metadata entry is too large")
    return archive.read(info)


def _one_string(value: object) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) and value else ()


def _optional_text(value: object) -> str | None:
    return str(value) if isinstance(value, (str, int, float)) else None


def _dependency_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(item["id"])
    return tuple(sorted(set(result)))
