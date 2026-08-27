from __future__ import annotations

from pathlib import Path

import pytest

from mcframework.errors import MetadataError, UnsafePathError
from mcframework.loader import WorkspaceLoader, validate_parameter_value
from mcframework.paths import confined_path, matches, normalize_relative_path


@pytest.mark.parametrize("value", ["/etc/passwd", "C:/Windows/file", "../escape", "a/../b", ""])
def test_rejects_unsafe_paths(value: str) -> None:
    with pytest.raises(UnsafePathError):
        normalize_relative_path(value)


def test_normalizes_backslashes_and_allows_glob_characters_in_concrete_paths() -> None:
    assert normalize_relative_path(r"mods\example.jar") == "mods/example.jar"
    assert normalize_relative_path("mods/*.jar", allow_glob=True) == "mods/*.jar"
    assert normalize_relative_path("mods/*.jar") == "mods/*.jar"
    assert normalize_relative_path("config/file?.txt") == "config/file?.txt"
    assert normalize_relative_path(".idea/shelf/[Changes]/mod.jar") == (
        ".idea/shelf/[Changes]/mod.jar"
    )


def test_glob_semantics() -> None:
    assert matches("mods/a.jar", "mods/*.jar")
    assert not matches("mods/a.txt", "mods/*.jar")
    assert matches("world/region/r.0.0.mca", "world/**")
    assert matches("world", "world/**")


def test_confined_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(UnsafePathError):
        confined_path(root, "link/file")


def test_loader_excludes_metadata_and_loads_payload(layer_factory, tmp_path: Path) -> None:
    root = layer_factory(
        "base",
        operations=[],
        payload={
            "config/a.txt": b"payload",
            ".idea/shelf/Uncommitted_changes_[Changes]/mod.jar": b"jar",
        },
    )
    layer = WorkspaceLoader(tmp_path).load(root)
    assert layer.identity == "base"
    assert layer.payload == {
        ".idea/shelf/Uncommitted_changes_[Changes]/mod.jar": b"jar",
        "config/a.txt": b"payload",
    }
    assert not any(path.startswith(".pack") for path in layer.payload)


def test_loader_requires_marker(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(MetadataError, match="marker"):
        WorkspaceLoader(tmp_path).load("empty")


def test_loader_rejects_bad_schema_and_unknown_document_keys(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    metadata = root / ".pack"
    metadata.mkdir(parents=True)
    (metadata / "layer.json").write_text('{"schema": 2, "id": "bad"}')
    with pytest.raises(MetadataError, match="schema"):
        WorkspaceLoader(tmp_path).load(root)
    (metadata / "layer.json").write_text('{"schema": 1, "id": "bad"}')
    (metadata / "x.json").write_text('{"unexpected": true}')
    with pytest.raises(MetadataError, match="unsupported keys"):
        WorkspaceLoader(tmp_path).load(root)


def test_invalid_parameter_is_rejected() -> None:
    with pytest.raises(MetadataError, match="non-serializable"):
        validate_parameter_value({"bad": {1, 2}})


def test_payload_symlink_is_rejected(layer_factory, tmp_path: Path) -> None:
    root = layer_factory("base", operations=[])
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    try:
        (root / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(UnsafePathError, match="symlinks"):
        WorkspaceLoader(tmp_path).load(root)
