from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import mcframework.package_inspect as package_inspect_module
import mcframework.project as project_module
from mcframework.errors import MetadataError
from mcframework.local_state import LocalState
from mcframework.package_inspect import (
    CompositeProviderMatcher,
    CurseForgeFingerprintMatcher,
    CurseForgeFingerprintRunner,
    ModrinthMatcher,
    ProviderMatch,
    curseforge_fingerprint,
    default_provider_matcher,
    file_hashes,
)
from mcframework.project import ProjectService
from mcframework.web_server import McpackServer


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
                        "fileName": "package.jar",
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


def test_curseforge_fingerprint_runner_is_exact_isolated_and_keeps_http_in_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"package bytes"
    parent_pid = os.getpid()
    http_pids: list[int] = []

    def urlopen(request: urllib.request.Request, timeout: float):
        del request, timeout
        http_pids.append(os.getpid())
        return io.BytesIO(
            json.dumps(cf_response(content, download_url="https://cdn/x.jar")).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    runner = CurseForgeFingerprintRunner()
    processes: tuple[object, ...] = ()
    try:
        assert runner(content) == curseforge_fingerprint(content)
        match = CurseForgeFingerprintMatcher(
            "secret", fingerprinter=runner
        ).match(content)
        assert match is not None and match.provider == "curseforge"
        executor = runner._executor
        assert executor is not None
        assert executor._processes  # type: ignore[attr-defined]
        processes = tuple(executor._processes.values())  # type: ignore[attr-defined]
        assert parent_pid not in executor._processes  # type: ignore[attr-defined]
        assert http_pids == [parent_pid]
    finally:
        runner.close()
        runner.close()
    assert all(not process.is_alive() for process in processes)  # type: ignore[attr-defined]


def test_curseforge_fingerprint_runner_bounds_outstanding_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[object, bytes, object]] = []
    condition = threading.Condition()
    shutdowns = 0
    attempts = 0
    cleanup = False
    semaphore_type = threading.BoundedSemaphore

    class ObservedSemaphore:
        def __init__(self, value: int):
            self.semaphore = semaphore_type(value)

        def acquire(self) -> None:
            nonlocal attempts
            with condition:
                attempts += 1
                condition.notify_all()
            self.semaphore.acquire()

        def release(self) -> None:
            self.semaphore.release()

    class FakePool:
        def __init__(self, *, max_workers: int, mp_context: object):
            assert max_workers == 4
            assert mp_context.get_start_method() == "spawn"  # type: ignore[attr-defined]

        def submit(self, function, content: bytes):
            from concurrent.futures import Future

            future: Future[int] = Future()
            with condition:
                submitted.append((function, content, future))
                if cleanup:
                    future.set_result(curseforge_fingerprint(content))
                condition.notify_all()
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            nonlocal shutdowns
            assert wait and cancel_futures
            shutdowns += 1

    monkeypatch.setattr(package_inspect_module, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(package_inspect_module.threading, "BoundedSemaphore", ObservedSemaphore)
    runner = CurseForgeFingerprintRunner()
    contents = [bytes([index]) * 100 for index in range(8)]
    with package_inspect_module.ThreadPoolExecutor(max_workers=8) as pool:
        results = [pool.submit(runner, content) for content in contents]
        try:
            with condition:
                assert condition.wait_for(lambda: attempts == 8 and len(submitted) >= 4, timeout=2)
                assert len(submitted) == 4
            for completed in range(len(contents)):
                with condition:
                    assert condition.wait_for(
                        lambda index=completed: len(submitted) > index, timeout=2
                    )
                    function, content, future = submitted[completed]
                assert function is curseforge_fingerprint
                future.set_result(curseforge_fingerprint(content))
            assert [future.result() for future in results] == [
                curseforge_fingerprint(content) for content in contents
            ]
        finally:
            with condition:
                cleanup = True
                for _, content, future in submitted:
                    if not future.done():
                        future.set_result(curseforge_fingerprint(content))
    runner.close()
    runner.close()
    assert shutdowns == 1


def test_composite_starts_modrinth_and_curseforge_in_parallel() -> None:
    started = {"modrinth": threading.Event(), "curseforge": threading.Event()}
    release = threading.Event()

    class BlockingMatcher:
        def __init__(self, provider: str):
            self.provider = provider

        def match(self, content: bytes) -> None:
            del content
            started[self.provider].set()
            assert release.wait(2)

    matcher = CompositeProviderMatcher(
        [BlockingMatcher("modrinth"), BlockingMatcher("curseforge")]
    )
    with package_inspect_module.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(matcher.match_all, b"content")
        try:
            assert started["modrinth"].wait(1)
            assert started["curseforge"].wait(1)
        finally:
            release.set()
        assert future.result() == ()


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
    assert match.version == "1.0"
    assert match.filename == "package.jar"
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


def test_project_service_owns_fingerprint_runner_only_for_default_matcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[object] = []
    closed: list[object] = []

    class FakeRunner:
        def __init__(self) -> None:
            created.append(self)

        def __call__(self, content: bytes) -> int:
            return curseforge_fingerprint(content)

        def close(self) -> None:
            closed.append(self)

    monkeypatch.setattr(project_module, "CurseForgeFingerprintRunner", FakeRunner)
    default = ProjectService(tmp_path / "default")
    custom = ProjectService(tmp_path / "custom", provider_matcher=NoMatch())
    assert len(created) == 1
    default.close()
    default.close()
    custom.close()
    assert closed == created


def test_project_close_cannot_overtake_provider_submit(
    tmp_path: Path,
) -> None:
    from concurrent.futures import Future

    service = ProjectService(tmp_path, provider_matcher=NoMatch())
    service._provider_prefetch_executor.shutdown()
    service._provider_executor.shutdown()
    submit_entered = threading.Event()
    release_submit = threading.Event()
    close_done = threading.Event()
    events: list[str] = []

    class PrefetchExecutor:
        def submit(self, *args: object, **kwargs: object) -> Future[None]:
            del args, kwargs
            raise AssertionError("closed service must not submit prefetch work")

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait and cancel_futures
            events.append("prefetch-shutdown")

    class ProviderExecutor:
        def submit(self, *args: object, **kwargs: object) -> Future[tuple[ProviderMatch, ...]]:
            del args, kwargs
            submit_entered.set()
            assert release_submit.wait(2)
            assert "provider-shutdown" not in events
            future: Future[tuple[ProviderMatch, ...]] = Future()
            future.set_result(())
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait and cancel_futures
            events.append("provider-shutdown")

    service._provider_prefetch_executor = PrefetchExecutor()  # type: ignore[assignment]
    service._provider_executor = ProviderExecutor()  # type: ignore[assignment]
    lookup = threading.Thread(target=service._provider_enrichment, args=(b"content", "1"))
    lookup.start()
    assert submit_entered.wait(1)

    def close() -> None:
        service.close()
        close_done.set()

    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not close_done.wait(0.1)
        assert events == []
    finally:
        release_submit.set()
    lookup.join(timeout=2)
    closer.join(timeout=2)
    assert close_done.is_set()
    assert events == ["prefetch-shutdown", "provider-shutdown"]
    with pytest.raises(RuntimeError, match="ProjectService is closed"):
        service._provider_enrichment(b"other", "1")
    hint = project_module._PackagePrefetch(tmp_path, "x.jar", ("file", 0, 0, 0), False, "mod")
    service._schedule_package_prefetch("dev", (hint,))


def test_routing_http_stays_responsive_while_large_fingerprints_run(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    game.mkdir()
    setup = ProjectService(tmp_path, provider_matcher=NoMatch())
    setup.link("client", "dev", game)
    setup.close()
    monkeypatch.setenv("CURSEFORGE_API_KEY", "dummy-test-key")
    reservation = LocalState(tmp_path).reserve_web("dev")
    server = McpackServer(tmp_path, "dev", reservation["nonce"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = server.service
    composite = service.provider_matcher
    modrinth, curseforge = composite.matchers  # type: ignore[attr-defined]
    monkeypatch.setattr(modrinth, "match", lambda content: None)
    lookup_pids: list[int] = []

    def no_curseforge_match(fingerprint: int) -> dict[str, object]:
        del fingerprint
        lookup_pids.append(os.getpid())
        return {"data": {"exactMatches": []}}

    monkeypatch.setattr(curseforge, "lookup", no_curseforge_match)
    original_fingerprinter = curseforge.fingerprinter
    fingerprint_started = threading.Event()

    def signaling_fingerprinter(content: bytes) -> int:
        fingerprint_started.set()
        return original_fingerprinter(content)

    monkeypatch.setattr(curseforge, "fingerprinter", signaling_fingerprinter)
    content = bytes(range(256)) * 49152 + b"\x01"
    previous_interval = sys.getswitchinterval()
    processes: tuple[object, ...] = ()
    handler_release = threading.Event()
    request = urllib.request.Request(
        f"http://{server.expected_host}/api/v1/routing?target_layer=client",
        headers={"Cookie": f"mcpack_session={server.browser_session}"},
    )
    try:
        sys.setswitchinterval(5.0)
        foreground_entered = threading.Event()
        foreground_done = threading.Event()
        foreground_errors: list[BaseException] = []
        original_routing = service.routing_options

        def gated_routing(*args: object, **kwargs: object) -> dict[str, object]:
            foreground_entered.set()
            assert fingerprint_started.wait(3)
            return original_routing(*args, **kwargs)

        monkeypatch.setattr(service, "routing_options", gated_routing)

        def request_foreground() -> None:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    assert response.status == 200
                    assert json.load(response)["revision"] == 0
            except BaseException as error:
                foreground_errors.append(error)
            finally:
                foreground_done.set()

        foreground = threading.Thread(target=request_foreground)
        foreground.start()
        assert foreground_entered.wait(1)
        started = time.perf_counter()
        assert service._provider_enrichment(content, "1")[0] == "pending"
        assert fingerprint_started.wait(3)
        assert foreground_done.wait(3)
        elapsed = time.perf_counter() - started
        foreground.join(timeout=1)
        assert foreground_errors == []
        assert elapsed < 1.0

        runner = service._fingerprint_runner
        assert runner is not None
        deadline = time.monotonic() + 3
        while runner._executor is None and time.monotonic() < deadline:
            time.sleep(0.001)
        assert runner._executor is not None
        deadline = time.monotonic() + 3
        while not runner._executor._processes and time.monotonic() < deadline:  # type: ignore[attr-defined]
            time.sleep(0.001)
        processes = tuple(runner._executor._processes.values())  # type: ignore[attr-defined]
        assert processes

        handler_entered = threading.Event()
        handler_done = threading.Event()
        close_done = threading.Event()

        def blocked_routing(*args: object, **kwargs: object) -> dict[str, object]:
            handler_entered.set()
            assert handler_release.wait(3)
            return original_routing(*args, **kwargs)

        monkeypatch.setattr(service, "routing_options", blocked_routing)

        def request_routing() -> None:
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    assert response.status == 200
            finally:
                handler_done.set()

        handler = threading.Thread(target=request_routing)
        handler.start()
        assert handler_entered.wait(1)
        server.shutdown()
        thread.join(timeout=3)

        def close_server() -> None:
            server.server_close()
            close_done.set()

        closer = threading.Thread(target=close_server)
        closer.start()
        assert not close_done.wait(0.1)
        assert any(process.is_alive() for process in processes)  # type: ignore[attr-defined]
        handler_release.set()
        handler.join(timeout=5)
        closer.join(timeout=8)
        assert handler_done.is_set()
        assert close_done.is_set()
    finally:
        handler_release.set()
        sys.setswitchinterval(previous_interval)
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert lookup_pids == [os.getpid()]
    assert all(lookup.status == "ready" for lookup in service._provider_matches.values())
    assert all(not process.is_alive() for process in processes)  # type: ignore[attr-defined]


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
    assert operation["source"]["provider"] == "curseforge"


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
