"""Portable, root-confined path helpers."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath

from .errors import UnsafePathError


def normalize_relative_path(value: str, *, allow_glob: bool = False) -> str:
    """Return a canonical POSIX relative path or reject an unsafe value."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UnsafePathError("path must be a non-empty string without NUL bytes")
    candidate = value.replace("\\", "/")
    if candidate.startswith("/") or (len(candidate) >= 2 and candidate[1] == ":"):
        raise UnsafePathError(f"absolute path is not allowed: {value!r}")
    path = PurePosixPath(candidate)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafePathError(f"path contains an unsafe component: {value!r}")
    normalized = path.as_posix()
    if normalized == ".":
        raise UnsafePathError("path must identify a file or directory")
    return normalized


def confined_path(root: Path, relative: str) -> Path:
    """Resolve a canonical relative path beneath root, including symlink checks."""
    normalized = normalize_relative_path(relative)
    root_resolved = root.resolve()
    candidate = root_resolved
    for part in PurePosixPath(normalized).parts:
        candidate /= part
        if candidate.is_symlink():
            raise UnsafePathError(f"managed path contains a symlink: {relative!r}")
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError(f"path escapes root {root_resolved}: {relative!r}") from exc
    return candidate


def matches(path: str, pattern: str) -> bool:
    """Match canonical paths with ** spanning directories."""
    path = normalize_relative_path(path)
    pattern = normalize_relative_path(pattern, allow_glob=True)
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return pattern.endswith("/**") and path == pattern[:-3].rstrip("/")


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a file atomically within its existing parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
