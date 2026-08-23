from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def layer_factory(tmp_path: Path):
    def create(
        name: str,
        *,
        parents: list[Any] | None = None,
        operations: list[dict[str, Any]] | None = None,
        metadata_files: dict[str, list[dict[str, Any]]] | None = None,
        payload: dict[str, bytes | str] | None = None,
    ) -> Path:
        root = tmp_path / name
        metadata = root / ".pack"
        metadata.mkdir(parents=True)
        (metadata / "layer.json").write_text(
            json.dumps({"schema": 1, "id": name, "parents": parents or []}),
            encoding="utf-8",
        )
        if operations is not None:
            (metadata / "operations.json").write_text(
                json.dumps({"operations": operations}), encoding="utf-8"
            )
        for filename, items in (metadata_files or {}).items():
            (metadata / filename).write_text(json.dumps({"operations": items}), encoding="utf-8")
        for relative, content in (payload or {}).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode() if isinstance(content, str) else content)
        return root

    return create
