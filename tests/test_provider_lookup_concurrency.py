from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcframework.package_inspect import CompositeProviderMatcher, ModrinthMatcher, file_hashes
from mcframework.project import ProjectService


def test_remote_requests_overlap_with_global_bound_dedup_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = [f"package-{index}".encode() for index in range(19)]
    digests = {file_hashes(content)["sha512"] for content in contents}
    condition = threading.Condition()
    release = threading.Event()
    requested: list[str] = []
    active = peak = completed = 0
    fail = True

    def urlopen(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        nonlocal active, peak
        if "/project/" in request.full_url:
            return io.BytesIO(json.dumps({
                "id": request.full_url.rsplit("/", 1)[1],
                "project_type": "mod", "categories": [], "additional_categories": [],
            }).encode())
        digest = request.full_url.split("/version_file/", 1)[1].split("?", 1)[0]
        assert digest in digests
        with condition:
            requested.append(digest)
            active += 1
            peak = max(peak, active)
            condition.notify_all()
        try:
            assert release.wait(5)
            return io.BytesIO(json.dumps({
                "id": digest, "project_id": digest, "version_number": "1",
                "files": [{"url": "https://cdn/file", "hashes": {"sha512": digest}}],
            }).encode())
        finally:
            with condition:
                active -= 1

    class PartialFailure:
        provider = "curseforge"

        def match(self, content: bytes) -> None:
            if fail and content == contents[0]:
                raise urllib.error.HTTPError("https://secret/token", 429, "secret", {}, None)

    modrinth = ModrinthMatcher()
    modrinth.project_cache_limit = 1
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    service = ProjectService(
        tmp_path, provider_matcher=CompositeProviderMatcher([modrinth, PartialFailure()])
    )
    finish = service._finish_provider_lookup

    def finished(digest, future):
        nonlocal completed
        finish(digest, future)
        with condition:
            completed += 1
            condition.notify_all()

    monkeypatch.setattr(service, "_finish_provider_lookup", finished)
    try:
        # Separate callers/plans share one package limit and one digest cache.
        for batch in (contents[:10], contents[10:]):
            for content in batch:
                item: dict = {}
                service._set_provider_fields(item, content, "1")
                assert item["provider_lookup"] == "pending"
                assert [candidate["provider"] for candidate in item["provider_candidates"]] == [
                    "local"
                ]
        with condition:
            assert condition.wait_for(lambda: active == 16, timeout=5)
            assert len(requested) == 16
        duplicate: dict = {}
        service._set_provider_fields(duplicate, contents[0], "1")
        assert duplicate["provider_lookup"] == "pending"
        with service._provider_lock:
            assert len(service._provider_matches) == 19
            queued = [lookup.future for lookup in service._provider_matches.values()][16:]
            assert all(future is not None and not future.running() for future in queued)
        release.set()
        with condition:
            assert condition.wait_for(lambda: completed == 19, timeout=5)
        assert peak == 16
        assert len(requested) == len(set(requested)) == 19
        assert len(modrinth._project_metadata) == 1
        for content in contents:
            item = {}
            service._set_provider_fields(item, content, "1")
            assert item["provider_lookup"] == ("failed" if content == contents[0] else "ready")
            assert [candidate["provider"] for candidate in item["provider_candidates"]] == [
                "local", "modrinth"
            ]
            if content == contents[0]:
                assert item["provider_errors"][0]["code"] == "http_429"
                assert "secret" not in json.dumps(item)
        assert len(requested) == 19
        assert service.retry_failed_provider_lookups() == 1
        fail = False
        retry: dict = {}
        service._set_provider_fields(retry, contents[0], "1")
        with condition:
            assert condition.wait_for(lambda: completed == 20, timeout=5)
        service._set_provider_fields(retry, contents[0], "1")
        assert retry["provider_lookup"] == "ready"
        assert retry["provider_errors"] == []
        assert len(requested) == 20
        assert service.retry_failed_provider_lookups() == 0
    finally:
        release.set()
        service.close()
