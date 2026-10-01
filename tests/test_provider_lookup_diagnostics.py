from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path

import pytest

from mcframework.package_inspect import (
    CompositeProviderMatcher,
    ProviderMatch,
    default_provider_matcher,
    file_hashes,
    provider_lookup_error,
)
from mcframework.project import ProjectService


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_http_diagnostics_do_not_expose_credentials(status: int) -> None:
    error = urllib.error.HTTPError("https://secret/token", status, "secret-key", {}, None)
    public = provider_lookup_error("curseforge", error)
    assert public["code"] == f"http_{status}"
    assert public["retryable"] is True
    assert "secret" not in json.dumps(public)


def test_partial_provider_failure_is_visible_and_retryable(tmp_path: Path) -> None:
    class Success:
        provider = "modrinth"

        def match(self, content: bytes) -> ProviderMatch:
            return ProviderMatch("modrinth", "project", "version", "1", "https://cdn/file",
                                 file_hashes(content))

    class Failure:
        provider = "curseforge"

        def match(self, content: bytes) -> None:
            raise urllib.error.HTTPError("https://secret/token", 401, "secret", {}, None)

    service = ProjectService(
        tmp_path, provider_matcher=CompositeProviderMatcher([Success(), Failure()])
    )
    try:
        item: dict = {}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            service._set_provider_fields(item, b"content", "1")
            if item["provider_lookup"] != "pending":
                break
            time.sleep(0.01)
        assert item["provider_lookup"] == "failed"
        assert [match["provider"] for match in item["provider_candidates"]] == ["local", "modrinth"]
        assert item["provider_errors"][0]["code"] == "http_401"
        assert "secret" not in json.dumps(item)
        assert service.retry_failed_provider_lookups() == 1
    finally:
        service.close()


def test_missing_curseforge_key_is_a_nonretryable_configuration_issue(monkeypatch) -> None:
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    matcher = default_provider_matcher()
    assert isinstance(matcher, CompositeProviderMatcher)
    monkeypatch.setattr(matcher.matchers[0], "match", lambda content: None)
    result = matcher.lookup_all(b"content")
    assert result.matches == ()
    assert result.errors[0]["code"] == "not_configured"
    assert result.errors[0]["retryable"] is False
