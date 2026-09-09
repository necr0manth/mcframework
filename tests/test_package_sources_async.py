from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import mcframework.project as project_module
from mcframework.errors import MetadataError
from mcframework.package_inspect import (
    CompositeProviderMatcher,
    CurseForgeFingerprintMatcher,
    ModrinthMatcher,
    ProviderMatch,
    curseforge_fingerprint,
    default_provider_matcher,
    file_hashes,
)
from mcframework.project import ProjectService


class NoMatch:
    def match(self, content: bytes) -> ProviderMatch | None:
        del content
        return None


def test_failed_provider_lookup_can_be_retried(tmp_path: Path) -> None:
    class Flaky:
        calls = 0

        def match(self, content: bytes) -> None:
            del content
            self.calls += 1
            if self.calls == 1:
                raise OSError("provider unavailable")

    matcher = Flaky()
    service = ProjectService(tmp_path, provider_matcher=matcher)
    content = archive("mod")

    deadline = time.monotonic() + 2
    status = "pending"
    while status == "pending" and time.monotonic() < deadline:
        status, _ = service._provider_enrichment(content, "1")
        time.sleep(0.01)
    assert status == "failed"
    assert service.retry_failed_provider_lookups() == 1

    deadline = time.monotonic() + 2
    status = "pending"
    while status == "pending" and time.monotonic() < deadline:
        status, _ = service._provider_enrichment(content, "1")
        time.sleep(0.01)
    assert status == "ready"
    assert matcher.calls == 2


def archive(kind: str) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as output:
        if kind == "mod":
            output.writestr("fabric.mod.json", '{"id":"sample","version":"1"}')
        else:
            output.writestr(
                "pack.mcmeta",
                json.dumps({"pack": {"pack_format": 1, "description": kind}}),
            )
    return content.getvalue()


def setup_service(
    layer_factory, tmp_path: Path, relative: str, content: bytes, matcher=None
):
    layer_factory("client", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    target = game / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    service = ProjectService(tmp_path, provider_matcher=matcher or NoMatch())
    service.link("client", "dev", game)
    return service, target


def cf_response(content: bytes, *, download_url: object) -> dict[str, object]:
    return {
        "data": {
            "exactMatches": [
                {
                    "id": 12,
                    "file": {
                        "id": 34,
                        "modId": 12,
                        "displayName": "1.0",
                        "fileFingerprint": curseforge_fingerprint(content),
                        "downloadUrl": download_url,
                        "hashes": [
                            {"algo": 1, "value": hashlib.sha1(content).hexdigest()}
                        ],
                    },
                }
            ]
        }
    }


def test_curseforge_fingerprint_filters_whitespace_and_is_unsigned() -> None:
    assert curseforge_fingerprint(b"a b\tc\r\n") == curseforge_fingerprint(b"abc")
    assert curseforge_fingerprint(b"abc") == 0x60A4FCC1


def test_curseforge_matcher_requires_key_and_exact_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"package bytes"
    calls: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float):
        calls.append(request)
        assert timeout == 1.5
        return io.BytesIO(json.dumps(cf_response(content, download_url="https://cdn/x.jar")).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert CurseForgeFingerprintMatcher("").match(content) is None
    assert calls == []
    match = CurseForgeFingerprintMatcher("secret", timeout=1.5).match(content)
    assert match is not None and match.provider == "curseforge"
    assert match.project_id == "12" and match.version_id == "34"
    assert calls[0].get_header("X-api-key") == "secret"

    bad = cf_response(content, download_url="https://cdn/x.jar")
    bad["data"]["exactMatches"][0]["file"]["hashes"][0]["value"] = "0" * 40  # type: ignore[index]
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(bad).encode()),
    )
    assert CurseForgeFingerprintMatcher("secret").match(content) is None


def test_curseforge_missing_download_uses_authenticated_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"package bytes"
    calls: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float):
        del timeout
        calls.append(request)
        value: object = cf_response(content, download_url=None)
        if "/download-url" in request.full_url:
            value = {"data": "https://cdn.example.invalid/package.jar"}
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    match = CurseForgeFingerprintMatcher("secret").match(content)
    assert match is not None and match.download_url.endswith("package.jar")
    assert len(calls) == 2
    assert all(request.get_header("X-api-key") == "secret" for request in calls)


def test_curseforge_rejects_match_without_usable_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"package bytes"

    def urlopen(request: urllib.request.Request, timeout: float):
        del timeout
        value: object = cf_response(content, download_url=None)
        if "/download-url" in request.full_url:
            value = {"data": ""}
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert CurseForgeFingerprintMatcher("secret").match(content) is None


def test_default_matcher_enables_curseforge_only_with_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    without_key = default_provider_matcher()
    assert [matcher.provider for matcher in without_key.matchers] == ["modrinth"]  # type: ignore[attr-defined]
    monkeypatch.setenv("CURSEFORGE_API_KEY", "configured")
    with_key = default_provider_matcher()
    assert [matcher.provider for matcher in with_key.matchers] == [  # type: ignore[attr-defined]
        "modrinth",
        "curseforge",
    ]


def test_default_matcher_all_provider_failures_reach_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    content = archive("mod")
    failing = True

    def urlopen(request: urllib.request.Request, timeout: float):
        del timeout
        if failing:
            if "api.modrinth.com" in request.full_url:
                return io.BytesIO(b"not-json")
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
        if "api.modrinth.com" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return io.BytesIO(b'{"data":{"exactMatches":[]}}')

    monkeypatch.setenv("CURSEFORGE_API_KEY", "configured")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    service = ProjectService(tmp_path, provider_matcher=default_provider_matcher())

    deadline = time.monotonic() + 2
    status = "pending"
    while status == "pending" and time.monotonic() < deadline:
        status, _ = service._provider_enrichment(content, "1")
        time.sleep(0.01)
    assert status == "failed"
    assert service.retry_failed_provider_lookups() == 1

    failing = False
    deadline = time.monotonic() + 2
    while status != "ready" and time.monotonic() < deadline:
        status, candidates = service._provider_enrichment(content, "1")
        time.sleep(0.01)
    assert status == "ready"
    assert [candidate["provider"] for candidate in candidates] == ["local"]


def test_default_composite_keeps_partial_match_or_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"package bytes"
    hashes = file_hashes(content)
    mode = "curseforge-no-match"

    def urlopen(request: urllib.request.Request, timeout: float):
        del timeout
        if mode == "curseforge-no-match":
            if "api.modrinth.com" in request.full_url:
                raise urllib.error.URLError("modrinth offline")
            return io.BytesIO(b'{"data":{"exactMatches":[]}}')
        if "api.curseforge.com" in request.full_url:
            raise urllib.error.URLError("curseforge offline")
        value = (
            {
                "id": "project",
                "project_type": "mod",
                "categories": [],
                "additional_categories": [],
            }
            if "/project/" in request.full_url
            else {
                "id": "version",
                "project_id": "project",
                "files": [{"url": "https://cdn/file", "hashes": hashes}],
            }
        )
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setenv("CURSEFORGE_API_KEY", "configured")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert default_provider_matcher().match_all(content) == ()  # type: ignore[attr-defined]

    mode = "modrinth-match"
    matches = default_provider_matcher().match_all(content)  # type: ignore[attr-defined]
    assert [match.provider for match in matches] == ["modrinth"]


def test_composite_order_and_modrinth_shader_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"package bytes"
    hashes = file_hashes(content)

    class Matcher:
        def __init__(self, provider: str):
            self.provider = provider

        def match(self, selected: bytes) -> ProviderMatch:
            return ProviderMatch(
                self.provider,
                self.provider,
                "version",
                None,
                f"https://cdn/{self.provider}",
                file_hashes(selected),
            )

    matches = CompositeProviderMatcher(
        [Matcher("modrinth"), Matcher("curseforge")]
    ).match_all(content)
    assert [item.provider for item in matches] == ["modrinth", "curseforge"]

    def urlopen(request: urllib.request.Request, timeout: float):
        del timeout
        value = (
            {
                "id": "project",
                "project_type": "shader",
                "categories": [],
                "additional_categories": [],
            }
            if "/project/" in request.full_url
            else {
                "id": "version",
                "project_id": "project",
                "files": [{"url": "https://cdn/file", "hashes": hashes}],
            }
        )
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    match = ModrinthMatcher().match(content)
    assert match is not None and match.kind == "shaderpack"


def test_modrinth_version_datapack_loader_overrides_project_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"datapack bytes"
    hashes = file_hashes(content)

    def urlopen(request: urllib.request.Request, timeout: float):
        del timeout
        value = (
            {
                "id": "ZXtLZIEN",
                "project_type": "mod",
                "categories": [],
                "additional_categories": [],
            }
            if "/project/" in request.full_url
            else {
                "id": "qsiGaGGS",
                "project_id": "ZXtLZIEN",
                "loaders": ["datapack"],
                "files": [{"url": "https://cdn/file.zip", "hashes": hashes}],
            }
        )
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    match = ModrinthMatcher().match(content)
    assert match is not None
    assert match.project_id == "ZXtLZIEN"
    assert match.version_id == "qsiGaGGS"
    assert match.kind == "datapack"


def test_composite_keeps_success_when_other_provider_fails() -> None:
    content = b"package bytes"

    class Success:
        def match(self, selected: bytes) -> ProviderMatch:
            return ProviderMatch(
                "modrinth",
                "project",
                "version",
                None,
                "https://cdn/file",
                file_hashes(selected),
            )

    class Failure:
        def match(self, selected: bytes) -> None:
            del selected
            raise UnicodeError("malformed response")

    matches = CompositeProviderMatcher([Success(), Failure()]).match_all(content)
    assert [match.provider for match in matches] == ["modrinth"]


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("mod", "mods/sample.jar"),
        ("shaderpack", "shaderpacks/sample.zip"),
        ("resourcepack", "resourcepacks/sample.zip"),
        ("datapack", "datapacks/sample.zip"),
    ],
)
def test_all_package_kinds_plan_detail_and_local_import(
    layer_factory, tmp_path: Path, kind: str, relative: str
) -> None:
    content = archive(kind)
    service, _ = setup_service(layer_factory, tmp_path, relative, content)
    plan_item = service.explorer_plan("dev", relative)["items"][0]
    assert plan_item["kind"] == "package"
    assert plan_item["proposed_package_kind"] == kind
    assert plan_item["provider_lookup"] in {"pending", "ready"}
    detail = service.delta_detail("dev", str(plan_item["id"]))
    assert detail["kind"] == "package"
    service.import_package(
        "dev", str(detail["id"]), provider_match="local", expected_sha256=detail["actual_sha256"]
    )
    target = service.state.feature_root("dev")
    operation = json.loads((target / ".pack/local-edits.json").read_text())["operations"][-1]
    assert operation["kind"] == kind
    suffix = ".jar" if kind == "mod" else ".zip"
    assert Path(operation["source"]["location"]).suffix == suffix


def test_declared_pack_at_arbitrary_destination_uses_declaration_kind(
    layer_factory, tmp_path: Path
) -> None:
    original = archive("resourcepack")
    (tmp_path / "original.zip").write_bytes(original)
    layer_factory(
        "client",
        operations=[
            {
                "type": "package",
                "id": "custom-pack",
                "kind": "resourcepack",
                "activation": "enabled",
                "destination": "custom/package.bin",
                "source": {
                    "provider": "local",
                    "location": "original.zip",
                    "sha256": hashlib.sha256(original).hexdigest(),
                },
            }
        ],
    )
    changed = archive("shaderpack")
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "custom").mkdir(parents=True)
    (game / "custom/package.bin").write_bytes(changed)
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", game)
    detail = service.delta_detail("dev", service.explorer_plan("dev", "custom")["items"][0]["id"])
    assert detail["proposed_package_kind"] == "resourcepack"
    service.import_package(
        "dev", detail["id"], provider_match="local", expected_sha256=detail["actual_sha256"]
    )
    assert service.instance_context("dev").files["custom/package.bin"] == changed


def test_visible_package_page_queues_one_background_enrichment_batch(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    (game / "mods").mkdir(parents=True)
    large = b"x" * (2 * 1024 * 1024)
    for index in range(8):
        (game / f"mods/{index}.jar").write_bytes(large)
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", game)
    service.explorer_snapshot("dev")
    started = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocked(*args, **kwargs) -> None:
        nonlocal started
        del args, kwargs
        with lock:
            started += 1
        assert release.wait(5)

    monkeypatch.setattr(service, "_prefetch_package_path", blocked)
    begin = time.perf_counter()
    page = service.explorer_page("dev", "mods")
    elapsed = time.perf_counter() - begin
    try:
        deadline = time.monotonic() + 1
        while started < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert len(page["items"]) == 8
        assert elapsed < 0.5
        assert started == 1
    finally:
        release.set()


def test_slow_provider_is_nonblocking_single_flight_for_page_detail_plan_and_local_apply(
    layer_factory, tmp_path: Path
) -> None:
    started = threading.Event()
    release = threading.Event()

    class Slow:
        calls = 0

        def match(self, content: bytes) -> None:
            del content
            self.calls += 1
            started.set()
            assert release.wait(5)

    matcher = Slow()
    content = archive("mod")
    service, _ = setup_service(layer_factory, tmp_path, "mods/sample.jar", content, matcher)
    try:
        begin = time.perf_counter()
        page = service.explorer_page("dev", "mods")
        assert time.perf_counter() - begin < 0.5
        assert started.wait(1)
        delta_id = str(page["items"][0]["delta_id"])
        detail = service.delta_detail("dev", delta_id)
        plan = service.explorer_plan("dev", "mods")
        assert detail["provider_lookup"] == plan["items"][0]["provider_lookup"] == "pending"
        assert matcher.calls == 1
        decision = {
            delta_id: {
                "mode": "package",
                "provider_match": "local",
                "expected_sha256": plan["items"][0]["expected_sha256"],
            }
        }
        begin = time.perf_counter()
        service.capture_explorer_path("dev", "mods", decisions=decision)
        assert time.perf_counter() - begin < 0.5
    finally:
        release.set()


def test_folder_remote_candidate_uses_hash_guard_and_rolls_back(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = archive("resourcepack")
    service, target_file = setup_service(
        layer_factory, tmp_path, "resourcepacks/sample.zip", content
    )
    plan = service.explorer_plan("dev", "resourcepacks")
    item = plan["items"][0]
    candidate = {
        "provider": "curseforge",
        "project_id": "12",
        "version_id": "34",
        "version": "1",
        "download_url": "https://cdn.example.invalid/sample.zip",
        "hashes": file_hashes(content),
        "confidence": "exact",
    }
    decision = {
        str(item["id"]): {
            "mode": "package",
            "provider_match": candidate,
            "expected_sha256": item["expected_sha256"],
        }
    }
    target_file.write_bytes(archive("shaderpack"))
    with pytest.raises(MetadataError, match="changed before mutation"):
        service.capture_explorer_path("dev", "resourcepacks", decisions=decision)
    assert not service.state.feature_root("dev").exists()

    target_file.write_bytes(content)
    service.explorer_snapshot("dev", refresh=True)
    plan = service.explorer_plan("dev", "resourcepacks")
    decision[str(item["id"])]["expected_sha256"] = plan["items"][0]["expected_sha256"]
    original_append = service._append_operations
    monkeypatch.setattr(
        service,
        "_append_operations",
        lambda *args, **kwargs: (_ for _ in ()).throw(MetadataError("forced rollback")),
    )
    with pytest.raises(MetadataError, match="forced rollback"):
        service.capture_explorer_path("dev", "resourcepacks", decisions=decision)
    assert not service.state.feature_root("dev").exists()
    monkeypatch.setattr(service, "_append_operations", original_append)
    service.capture_explorer_path("dev", "resourcepacks", decisions=decision)
    operation = json.loads(
        (service.state.feature_root("dev") / ".pack/local-edits.json").read_text()
    )["operations"][-1]
    assert operation["source"]["provider"] == "direct_url"


def test_cached_empty_page_skips_global_scan_and_changed_directory_falls_back(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    for index in range(100):
        (game / f"empty-{index}").mkdir(parents=True)
    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service.link("client", "dev", game)
    service.explorer_snapshot("dev")
    calls = 0
    original = project_module.scan_generation_matches

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(project_module, "scan_generation_matches", counted)
    assert service.explorer_page("dev", "empty-50")["items"] == []
    assert calls == 0
    (game / "empty-50/new.txt").write_text("new")
    page = service.explorer_page("dev", "empty-50")
    assert calls >= 1
    assert [item["path"] for item in page["items"]] == ["empty-50/new.txt"]
