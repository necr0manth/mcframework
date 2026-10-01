"""Small HTTP helpers shared by package provider integrations."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .errors import ArtifactError

USER_AGENT = "minecraft-pack-framework/0.1"


def read_json(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"package provider request failed: {type(exc).__name__}") from exc


def read_bytes(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())
    except (OSError, urllib.error.URLError) as exc:
        raise ArtifactError(f"package download failed: {type(exc).__name__}") from exc


def object_list(value: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ArtifactError(f"package provider returned invalid {key}")
    return value


def compatible_values(value: Any, minecraft: str, loader: str) -> bool:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False
    wanted_loader = normalize_loader(loader)
    normalized = {normalize_loader(item) for item in value}
    return minecraft in value and wanted_loader in normalized


def normalize_loader(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())
