"""Format-independent metadata document frontends."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .errors import MetadataError


class MetadataParser(Protocol):
    def read_object(self, path: Path) -> Mapping[str, Any]: ...


class JsonMetadataParser:
    def read_object(self, path: Path) -> Mapping[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"cannot read JSON metadata {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise MetadataError(f"{path}: top-level JSON value must be an object")
        return data
