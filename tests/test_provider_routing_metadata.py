from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from mcframework.errors import MetadataError
from mcframework.package_inspect import ModrinthMatcher, confirmed_provider_match, file_hashes
from mcframework.project import ProjectService

CONTENT = b"exact jar bytes"


def version(project_id: str = "magic") -> dict:
    return {
        "id": "version", "project_id": project_id,
        "files": [{"url": "https://cdn.example.invalid/magic.jar", "hashes": file_hashes(CONTENT)}],
    }


def project(project_id: str = "magic") -> dict:
    return {
        "id": project_id, "project_type": "mod", "categories": ["magic"],
        "additional_categories": ["library", "magic"],
    }


def response(value: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(value).encode())


def test_modrinth_enriches_exact_match_and_reuses_project_cache(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        assert timeout == 1.25
        assert request.get_header("User-agent")
        return response(project() if "/project/" in request.full_url else version())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    matcher = ModrinthMatcher(timeout=1.25)
    match = matcher.match(CONTENT)
    assert match is not None
    assert match.kind == "mod"
    assert match.categories == match.tags == ("magic", "library")
    assert matcher.match(CONTENT) == match
    assert calls.count("https://api.modrinth.com/v2/project/magic") == 1
    assert len(calls) == 3
    selected = confirmed_provider_match(match.document(), CONTENT)
    assert selected is not None
    assert selected.kind == match.kind
    assert selected.categories == match.categories
    assert selected.tags == match.tags


@pytest.mark.parametrize("optional_response", [
    None, [], {}, {"id": "wrong"},
    dict(project(), categories="magic"),
    dict(project(), additional_categories=[1]),
    dict(project(), project_type=[]),
    dict(project(), project_type="unknown"),
    urllib.error.URLError("offline"),
    b"not-json",
])
def test_optional_enrichment_failure_keeps_verified_match(monkeypatch, optional_response):
    def urlopen(request, timeout):
        if "/version_file/" in request.full_url:
            return response(version())
        if isinstance(optional_response, Exception):
            raise optional_response
        if isinstance(optional_response, bytes):
            return io.BytesIO(optional_response)
        return response(optional_response)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    match = ModrinthMatcher().match(CONTENT)
    assert match is not None and match.confidence == "exact"
    assert match.kind is None and match.categories == match.tags == ()


@pytest.mark.parametrize("offline", [False, True])
def test_no_project_request_without_exact_match(monkeypatch, offline):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if offline:
            raise urllib.error.URLError("offline")
        raw = version()
        raw["files"][0]["hashes"]["sha512"] = "0" * 128
        return response(raw)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert ModrinthMatcher().match(CONTENT) is None
    assert len(calls) == 1 and "/version_file/" in calls[0]


def test_project_cache_is_bounded_and_ids_are_url_encoded(monkeypatch):
    current = "magic/other?query=x"
    urls = []

    def urlopen(request, timeout):
        urls.append(request.full_url)
        return response(project(current) if "/project/" in request.full_url else version(current))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    matcher = ModrinthMatcher()
    assert matcher.match(CONTENT) is not None
    assert urls[-1] == "https://api.modrinth.com/v2/project/magic%2Fother%3Fquery%3Dx"
    for index in range(matcher.project_cache_limit):
        current = str(index)
        assert matcher.match(CONTENT) is not None
    assert len(matcher._project_metadata) == matcher.project_cache_limit
    assert "magic/other?query=x" not in matcher._project_metadata


@pytest.mark.parametrize("field,value", [
    ("kind", []), ("kind", ""), ("categories", "magic"),
    ("categories", ["magic", 1]), ("tags", [""]), ("tags", None),
])
def test_confirmed_selection_validates_optional_metadata(field, value):
    candidate = {
        "provider": "modrinth", "project_id": "magic", "version_id": "version",
        "confidence": "exact", "download_url": "https://cdn.example.invalid/magic.jar",
        "hashes": file_hashes(CONTENT), field: value,
    }
    with pytest.raises(MetadataError, match=field):
        confirmed_provider_match(candidate, CONTENT)


@pytest.mark.parametrize("confirmed", [False, True])
def test_modrinth_categories_route_actual_import(tmp_path: Path, monkeypatch, confirmed):
    root = tmp_path / "project/client"
    (root / ".pack").mkdir(parents=True)
    (root / ".pack/layer.json").write_text('{"schema": 2, "id": "client"}')
    rule = {
        "type": "routing_rule", "id": "magic",
        "when": {"operation_types": ["package"], "origin": ["instance_capture"], "package": {
            "provider": ["modrinth"], "categories": ["magic"], "kind": ["mod"],
        }},
        "write": {"file": {"path": "mods/.pack/magic.json", "path_mode": "layer"}},
    }
    (root / ".pack/routing.json").write_text(json.dumps({"operations": [rule]}))
    game = tmp_path / "game"
    (game / "mods").mkdir(parents=True)
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id": "magic", "version": "1.0"}')
    content = jar.getvalue()
    (game / "mods/magic.jar").write_bytes(content)

    def urlopen(request, timeout):
        if "/version_file/" in request.full_url:
            raw = version()
            raw["files"][0]["hashes"] = file_hashes(content)
            return response(raw)
        assert request.full_url == "https://api.modrinth.com/v2/project/magic"
        return response(project())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    service = ProjectService(tmp_path / "project", provider_matcher=ModrinthMatcher())
    service.link("client", "dev", game)
    delta = service.explorer_plan("dev", "mods/magic.jar")["items"][0]
    detail = service.delta_detail("dev", delta["id"])
    assert detail["proposed_package_id"] == "magic"
    selection = "modrinth"
    if confirmed:
        candidates = detail["provider_candidates"]
        selection = next(
            candidate for candidate in candidates
            if candidate["provider"] == "modrinth"
        )
    service.import_package("dev", delta["id"], "client", selection)
    source = root / "mods/.pack/magic.json"
    assert source.is_file()
    declaration = json.loads(source.read_text())["operations"][0]
    assert declaration["source"]["provider"] == "direct_url"
    assert "categories" not in declaration and "tags" not in declaration
    assert service.instance_context("dev").files["mods/magic.jar"] == content
