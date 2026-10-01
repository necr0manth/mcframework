"""Bounded local JAR metadata parsing and exact provider lookup adapters."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
    project_slug: str | None = None
    filename: str | None = None

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
            "project_slug": self.project_slug,
            "filename": self.filename,
        }


class ProviderMatcher(Protocol):
    def match(self, content: bytes) -> ProviderMatch | None: ...


@dataclass(frozen=True)
class ProviderLookupResult:
    matches: tuple[ProviderMatch, ...] = ()
    errors: tuple[dict[str, Any], ...] = ()


def provider_lookup_error(provider: str, error: Exception) -> dict[str, Any]:
    """Public diagnostics must never include exception URLs or credentials."""
    code = "unavailable"
    message = "Package source lookup failed. Retry the lookup."
    if isinstance(error, urllib.error.HTTPError):
        code = f"http_{error.code}"
        if error.code in {401, 403}:
            message = "Access denied by the package source. Check its API key and permissions."
        elif error.code == 429:
            message = "The package source rate limit was reached. Wait and retry."
        else:
            message = f"The package source returned HTTP {error.code}. Retry the lookup."
    elif isinstance(error, TimeoutError) or (
        isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)
    ):
        code = "timeout"
        message = "The package source timed out. Retry the lookup."
    elif isinstance(error, (OSError, urllib.error.URLError)):
        code = "network"
        message = "Cannot connect to the package source. Check the connection and retry."
    return {"provider": provider, "code": code, "message": message, "retryable": True}


def normalize_package_kind(value: str | None) -> str | None:
    """Return the package-kind spelling used by metadata declarations."""
    if value == "shader":
        return "shaderpack"
    if value in {"mod", "resourcepack", "datapack", "modpack", "shaderpack"}:
        return value
    return None


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
    """Official anonymous exact-hash lookup."""

    endpoint = "https://api.modrinth.com/v2/version_file/{digest}?algorithm=sha512"
    provider = "modrinth"
    project_endpoint = "https://api.modrinth.com/v2/project/{project_id}"
    project_cache_limit = 128

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._project_metadata: dict[str, tuple[str, tuple[str, ...], str | None]] = {}
        self._project_lock = threading.Lock()

    def _project(self, project_id: str) -> tuple[str, tuple[str, ...], str | None] | None:
        with self._project_lock:
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
            raw_kind = raw.get("project_type")
            kind = normalize_package_kind(raw_kind if isinstance(raw_kind, str) else None)
            if kind is None:
                return None
            categories = _string_list(raw.get("categories"), "categories")
            additional = _string_list(raw.get("additional_categories"), "additional_categories")
        except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError, MetadataError):
            return None  # Optional project metadata must not discard an exact file match.
        slug = _nonempty_string(raw.get("slug"))
        result = kind, tuple(dict.fromkeys((*categories, *additional))), slug
        with self._project_lock:
            if (
                project_id not in self._project_metadata
                and len(self._project_metadata) >= self.project_cache_limit
            ):
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
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
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
        loaders = raw.get("loaders")
        version_kind = (
            "datapack"
            if isinstance(loaders, list) and "datapack" in loaders
            else None
        )
        return ProviderMatch(
            "modrinth",
            project_id,
            version_id,
            _optional_text(raw.get("version_number")),
            str(selected["url"]),
            hashes,
            kind=version_kind or (project[0] if project else None),
            categories=project[1] if project else (),
            tags=project[1] if project else (),
            project_slug=project[2] if project else None,
            filename=_nonempty_string(selected.get("filename")),
        )


def curseforge_fingerprint(content: bytes) -> int:
    """CurseForge's unsigned MurmurHash2 over whitespace-filtered bytes."""
    filtered = bytes(value for value in content if value not in {9, 10, 13, 32})
    length = len(filtered)
    value = (1 ^ length) & 0xFFFFFFFF
    offset = 0
    while length >= 4:
        block = int.from_bytes(filtered[offset : offset + 4], "little")
        block = (block * 0x5BD1E995) & 0xFFFFFFFF
        block ^= block >> 24
        block = (block * 0x5BD1E995) & 0xFFFFFFFF
        value = ((value * 0x5BD1E995) ^ block) & 0xFFFFFFFF
        offset += 4
        length -= 4
    if length == 3:
        value ^= filtered[offset + 2] << 16
    if length >= 2:
        value ^= filtered[offset + 1] << 8
    if length >= 1:
        value ^= filtered[offset]
        value = (value * 0x5BD1E995) & 0xFFFFFFFF
    value ^= value >> 13
    value = (value * 0x5BD1E995) & 0xFFFFFFFF
    value ^= value >> 15
    return value & 0xFFFFFFFF


class CurseForgeFingerprintRunner:
    """Run bounded CurseForge fingerprints outside the service process."""

    def __init__(self) -> None:
        self._executor: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(4)
        self._closed = False

    def __call__(self, content: bytes) -> int:
        self._slots.acquire()
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("CurseForge fingerprint runner is closed")
                if self._executor is None:
                    self._executor = ProcessPoolExecutor(
                        max_workers=4,
                        mp_context=multiprocessing.get_context("spawn"),
                    )
                future = self._executor.submit(curseforge_fingerprint, content)
            return future.result()
        finally:
            self._slots.release()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


def _usable_download_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlparse(value)
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else None


class CurseForgeFingerprintMatcher:
    """Optional official exact-fingerprint adapter."""

    endpoint = "https://api.curseforge.com/v1/fingerprints"
    provider = "curseforge"
    download_endpoint = "https://api.curseforge.com/v1/mods/{mod_id}/files/{file_id}/download-url"

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        *,
        fingerprinter: Callable[[bytes], int] = curseforge_fingerprint,
    ):
        self.api_key = api_key
        self.timeout = timeout
        self.fingerprinter = fingerprinter

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
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read())
        return value if isinstance(value, dict) else None

    def _download_url(self, mod_id: int | str, file_id: int | str) -> str | None:
        request = urllib.request.Request(
            self.download_endpoint.format(
                mod_id=urllib.parse.quote(str(mod_id), safe=""),
                file_id=urllib.parse.quote(str(file_id), safe=""),
            ),
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = json.loads(response.read())
        return _usable_download_url(raw.get("data")) if isinstance(raw, dict) else None

    def match(self, content: bytes) -> ProviderMatch | None:
        if not self.api_key.strip():
            return None
        fingerprint = self.fingerprinter(content)
        raw = self.lookup(fingerprint)
        data = raw.get("data") if isinstance(raw, dict) else None
        matches = data.get("exactMatches") if isinstance(data, dict) else None
        if not isinstance(matches, list):
            return None
        hashes = file_hashes(content)
        md5 = hashlib.md5(content).hexdigest()  # noqa: S324 - provider identity only
        for candidate in matches:
            if not isinstance(candidate, dict):
                continue
            file = candidate.get("file")
            if not isinstance(file, dict) or file.get("fileFingerprint") != fingerprint:
                continue
            remote_hashes = file.get("hashes")
            if not isinstance(remote_hashes, list):
                continue
            recognized = []
            for item in remote_hashes:
                if not isinstance(item, dict) or not isinstance(item.get("value"), str):
                    continue
                algorithm = item.get("algo")
                expected = (
                    {1: hashes["sha1"], 2: md5}.get(algorithm)
                    if isinstance(algorithm, int)
                    else None
                )
                if expected is not None:
                    recognized.append(item["value"].casefold() == expected)
            if not recognized or not all(recognized):
                continue
            mod_id = file.get("modId", candidate.get("id"))
            file_id = file.get("id")
            if not isinstance(mod_id, (str, int)) or not isinstance(file_id, (str, int)):
                continue
            download_url = _usable_download_url(file.get("downloadUrl"))
            if download_url is None:
                download_url = self._download_url(mod_id, file_id)
            if download_url is None:
                continue
            return ProviderMatch(
                "curseforge",
                str(mod_id),
                str(file_id),
                _optional_text(file.get("displayName")),
                download_url,
                hashes,
                reason="CurseForge returned the exact file fingerprint and hash",
                filename=_nonempty_string(file.get("fileName")),
            )
        return None


class CompositeProviderMatcher:
    """Ordered provider matcher used for enrichment and explicit selection."""

    def __init__(
        self, matchers: Iterable[ProviderMatcher], *,
        configuration_errors: tuple[dict[str, Any], ...] = ()
    ):
        self.matchers = tuple(matchers)
        self.configuration_errors = configuration_errors

    def lookup_all(self, content: bytes) -> ProviderLookupResult:
        """Keep successful matches and failures independently for the capture UI."""
        matches: list[ProviderMatch] = []
        errors = list(self.configuration_errors)
        if self.matchers:
            with ThreadPoolExecutor(
                max_workers=len(self.matchers), thread_name_prefix="mcpack-provider-adapter"
            ) as pool:
                futures = [
                    (matcher, pool.submit(matcher.match, content)) for matcher in self.matchers
                ]
                for matcher, future in futures:
                    try:
                        match = future.result()
                    except Exception as error:
                        errors.append(
                            provider_lookup_error(getattr(matcher, "provider", "remote"), error)
                        )
                    else:
                        if match is not None:
                            matches.append(match)
        return ProviderLookupResult(tuple(matches), tuple(errors))

    def match_all(self, content: bytes) -> tuple[ProviderMatch, ...]:
        if not self.matchers:
            return ()
        with ThreadPoolExecutor(
            max_workers=len(self.matchers), thread_name_prefix="mcpack-provider-adapter"
        ) as pool:
            futures = [pool.submit(matcher.match, content) for matcher in self.matchers]
            matches: list[ProviderMatch] = []
            failures: list[Exception] = []
            successful_lookups = 0
            for future in futures:
                try:
                    match = future.result()
                except Exception as error:
                    failures.append(error)
                else:
                    successful_lookups += 1
                    if match is not None:
                        matches.append(match)
        if successful_lookups == 0 and failures:
            raise failures[0]
        return tuple(matches)

    def match(self, content: bytes) -> ProviderMatch | None:
        failures: list[Exception] = []
        successful_lookups = 0
        for matcher in self.matchers:
            try:
                match = matcher.match(content)
            except Exception as error:
                failures.append(error)
                continue
            successful_lookups += 1
            if match is not None:
                return match
        if successful_lookups == 0 and failures:
            raise failures[0]
        return None

    def match_provider(self, provider: str, content: bytes) -> ProviderMatch | None:
        failures: list[Exception] = []
        successful_lookups = 0
        for matcher in self.matchers:
            declared = getattr(matcher, "provider", None)
            if declared is not None and declared != provider:
                continue
            try:
                match = matcher.match(content)
            except Exception as error:
                failures.append(error)
                continue
            successful_lookups += 1
            if match is not None and match.provider == provider:
                return match
        if successful_lookups == 0 and failures:
            raise failures[0]
        return None


def default_provider_matcher(
    *, fingerprinter: Callable[[bytes], int] = curseforge_fingerprint
) -> ProviderMatcher:
    matchers: list[ProviderMatcher] = [ModrinthMatcher()]
    api_key = os.environ.get("CURSEFORGE_API_KEY", "").strip()
    if api_key:
        matchers.append(CurseForgeFingerprintMatcher(api_key, fingerprinter=fingerprinter))
    configuration_errors = () if api_key else ({
        "provider": "curseforge", "code": "not_configured", "retryable": False,
        "message": "CurseForge lookup is disabled because CURSEFORGE_API_KEY is not configured.",
    },)
    return CompositeProviderMatcher(matchers, configuration_errors=configuration_errors)


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
    confirmed_url = _usable_download_url(url)
    if not isinstance(hashes, dict) or confirmed_url is None:
        raise MetadataError("confirmed provider_match needs download_url and hashes")
    actual = file_hashes(content)
    if not any(hashes.get(name) == actual[name] for name in ("sha512", "sha1", "sha256")):
        raise MetadataError("confirmed provider file hashes do not match the instance package")
    raw_kind = value.get("kind")
    kind = normalize_package_kind(raw_kind if isinstance(raw_kind, str) else None)
    if raw_kind is not None and kind is None:
        raise MetadataError("provider_match kind must be a non-empty string or null")
    categories = _string_list(value.get("categories", []), "categories")
    tags = _string_list(value.get("tags", []), "tags")
    project_slug = _optional_match_text(value.get("project_slug"), "project_slug")
    filename = _optional_match_text(value.get("filename"), "filename")
    return ProviderMatch(
        str(value["provider"]),
        str(value["project_id"]),
        str(value["version_id"]),
        _optional_text(value.get("version")),
        confirmed_url,
        actual,
        str(confidence),
        "explicit user confirmation" if confidence == "fuzzy" else "confirmed exact match",
        kind,
        categories,
        tags,
        project_slug,
        filename,
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


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_match_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MetadataError(f"provider_match {field} must be a string or null")
    return value or None


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
