from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import mcframework.explorer as explorer_module
import mcframework.project as project_module
from mcframework.errors import MetadataError
from mcframework.local_state import LocalState
from mcframework.project import ProjectService
from mcframework.transport import LocalFilesystemInstance
from mcframework.web_server import McpackServer


def game_path(project: Path) -> Path:
    return project.parent / f"{project.name}-game"


def decisions_for(plan: dict[str, Any]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in plan["items"]:
        decision: dict[str, object] = {"mode": item["capture_modes"][-1]}
        if "expected_sha256" in item:
            decision["expected_sha256"] = item["expected_sha256"]
        else:
            decision["expected_absent"] = True
        result[str(item["id"])] = decision
    return result


@contextmanager
def auto_capture_server(
    project: Path, service: ProjectService
) -> Iterator[tuple[McpackServer, urllib.request.OpenerDirector, str]]:
    reservation = LocalState(project).reserve_web("dev")
    server = McpackServer(project, "dev", reservation["nonce"])
    server.service = service
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.expected_host}/"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.open(base + f"?token={reservation['browser_capability']}", timeout=5).read()
    try:
        yield server, opener, base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def auto_capture_request(
    opener: urllib.request.OpenerDirector,
    base: str,
    csrf: str,
    revision: int,
    body: dict[str, object],
) -> urllib.request.Request:
    return urllib.request.Request(
        base + "api/v1/files/capture",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Mcpack-CSRF": csrf,
            "If-Match": f'"{revision}"',
            "Origin": base.rstrip("/"),
        },
    )


def test_auto_payload_700_file_post_is_one_linear_compact_batch(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    bulk = game / "bulk"
    bulk.mkdir(parents=True)
    for index in range(700):
        (bulk / f"file-{index:04}.txt").write_bytes(bytes([index % 251]) * 1024)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    cached = service.explorer_snapshot("dev")
    counts = {"contexts": 0, "reads": 0, "writes": 0}
    original_context = service.instance_context
    original_read = LocalFilesystemInstance.read
    original_write = service._batch_atomic_write

    def context(*args: object, **kwargs: object) -> Any:
        counts["contexts"] += 1
        return original_context(*args, **kwargs)

    def read(transport: LocalFilesystemInstance, item_path: str) -> bytes:
        counts["reads"] += 1
        return original_read(transport, item_path)

    def write(item_path: Path, content: bytes) -> None:
        counts["writes"] += 1
        original_write(item_path, content)

    def fail(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("auto payload POST must not use the explicit planning pipeline")

    with auto_capture_server(tmp_path, service) as (server, opener, base):
        with monkeypatch.context() as patch:
            patch.setattr(service, "instance_context", context)
            patch.setattr(LocalFilesystemInstance, "read", read)
            patch.setattr(service, "_batch_atomic_write", write)
            patch.setattr(service, "explorer_plan", fail)
            patch.setattr(service, "instance_diff", fail)
            patch.setattr(service.provider_matcher, "match", fail)
            patch.setattr(server, "bootstrap", fail)
            started = time.perf_counter()
            response = json.loads(
                opener.open(
                    auto_capture_request(
                        opener,
                        base,
                        server.csrf,
                        0,
                        {"path": "bulk", "target_layer": "client", "auto_payload": True},
                    ),
                    timeout=15,
                ).read()
            )
            elapsed = time.perf_counter() - started
        assert response == {
            "ok": True,
            "applied": True,
            "revision": 1,
            "explorer_patch": {"path": "bulk", "captured_count": 700},
        }
        assert server.revision == 1
        assert service._explorer_snapshots["dev"] is not cached
        assert service._explorer_snapshots["dev"].context_verified is False

    record_property("auto_payload_post_700_seconds", f"{elapsed:.6f}")
    assert counts == {"contexts": 1, "reads": 700, "writes": 700}
    assert len(tuple((tmp_path / "client/bulk").iterdir())) == 700


def test_auto_payload_stale_if_match_rejects_before_source_read(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    (game / "bulk/a.txt").write_text("actual")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    cached = service.explorer_snapshot("dev")

    def fail(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("stale auto payload request must not read source files")

    with auto_capture_server(tmp_path, service) as (server, opener, base):
        monkeypatch.setattr(LocalFilesystemInstance, "read", fail)
        request = auto_capture_request(
            opener,
            base,
            server.csrf,
            1,
            {"path": "bulk", "target_layer": "client", "auto_payload": True},
        )
        with pytest.raises(urllib.error.HTTPError) as response:
            opener.open(request, timeout=5)
        assert response.value.code == 409
        assert server.revision == 0
        assert service._explorer_snapshots["dev"] is cached
    assert not (tmp_path / "client/bulk/a.txt").exists()


def test_auto_payload_http_fallback_is_compact_and_keeps_revision(
    layer_factory, tmp_path: Path
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    (game / "bulk/mod.jar").write_text("not inspected by auto payload")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)

    with auto_capture_server(tmp_path, service) as (server, opener, base):
        response = json.loads(
            opener.open(
                auto_capture_request(
                    opener,
                    base,
                    server.csrf,
                    0,
                    {"path": "bulk", "target_layer": "client", "auto_payload": True},
                ),
                timeout=5,
            ).read()
        )
        assert set(response) == {"ok", "applied", "revision", "decision_required"}
        assert response["ok"] is True
        assert response["applied"] is False
        assert response["revision"] == 0
        assert response["decision_required"]["path"] == "bulk"
        assert len(response["decision_required"]["items"]) == 1
        assert server.revision == 0
    assert not (tmp_path / "client/bulk/mod.jar").exists()


def test_auto_payload_source_mutation_during_read_has_zero_writes(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    source = game / "bulk/a.txt"
    source.write_text("first")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    cached = service.explorer_snapshot("dev")
    original_read = LocalFilesystemInstance.read

    def mutate_after_read(transport: LocalFilesystemInstance, item_path: str) -> bytes:
        content = original_read(transport, item_path)
        source.write_text("second-longer")
        return content

    monkeypatch.setattr(LocalFilesystemInstance, "read", mutate_after_read)
    with pytest.raises(MetadataError, match="changed during auto payload"):
        service.capture_explorer_path("dev", "bulk", "client", auto_payload=True)
    assert service._explorer_snapshots["dev"] is cached
    assert not (tmp_path / "client/bulk/a.txt").exists()


def test_auto_payload_nth_write_failure_rolls_back_every_payload(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[], payload={"bulk/a.txt": "old"})
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    (game / "bulk/a.txt").write_text("new")
    (game / "bulk/b.txt").write_text("added")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    cached = service.explorer_snapshot("dev")
    epoch_path = service._auto_payload_epoch_path(tmp_path / "client")
    assert not epoch_path.exists()
    original_write = service._batch_atomic_write
    calls = 0

    def fail_second(item_path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second write failure")
        original_write(item_path, content)

    monkeypatch.setattr(service, "_batch_atomic_write", fail_second)
    with pytest.raises(OSError, match="second write failure"):
        service.capture_explorer_path("dev", "bulk", "client", auto_payload=True)
    assert service._explorer_snapshots["dev"] is cached
    assert not epoch_path.exists()
    assert (tmp_path / "client/bulk/a.txt").read_text() == "old"
    assert not (tmp_path / "client/bulk/b.txt").exists()


def test_auto_payload_post_epoch_failure_rolls_back_epoch_and_payload(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    (game / "bulk/a.txt").write_text("captured")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    cached = service.explorer_snapshot("dev")
    epoch_path = service._auto_payload_epoch_path(tmp_path / "client")

    def fail_build(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise RuntimeError("injected snapshot build failure")

    monkeypatch.setattr(project_module, "build_snapshot", fail_build)
    with pytest.raises(RuntimeError, match="snapshot build failure"):
        service.capture_explorer_path("dev", "bulk", "client", auto_payload=True)
    assert service._explorer_snapshots["dev"] is cached
    assert not epoch_path.exists()
    assert not (tmp_path / "client/bulk/a.txt").exists()


@pytest.mark.parametrize("case", ["package", "nonfinal", "overlay", "mixed"])
def test_auto_payload_unsupported_scope_falls_back_without_mutation(
    case: str, layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case == "nonfinal":
        layer_factory("base", operations=[])
        layer_factory("client", parents=[{"path": "../base"}], operations=[])
    elif case == "mixed":
        layer_factory("client", operations=[], payload={"bulk/missing.txt": "desired"})
    else:
        layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    filename = "mod.jar" if case == "package" else "actual.txt"
    (game / "bulk" / filename).write_text("actual")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    cached = service.explorer_snapshot("dev")
    links_before = service.state.links_path.read_bytes()

    def fail_provider(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("auto payload fallback must not use provider matching")

    monkeypatch.setattr(service.provider_matcher, "match", fail_provider)
    target = "base" if case == "nonfinal" else None if case == "overlay" else "client"
    result = service.capture_explorer_path("dev", "bulk", target, auto_payload=True)
    assert result is not None
    assert result["applied"] is False
    assert result["path"] == "bulk"
    assert result["items"]
    assert service._explorer_snapshots["dev"] is cached
    assert service.state.links_path.read_bytes() == links_before
    assert not service.state.feature_root("dev").exists()
    assert not (tmp_path / (target or "client") / "bulk" / filename).exists()


def test_auto_payload_retains_warm_snapshot_for_ignore_and_loaded_pages(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    bulk = game / "bulk"
    bulk.mkdir(parents=True)
    for index in range(700):
        (bulk / f"file-{index:04}.txt").write_bytes(bytes([index % 251]) * 1024)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    original = service.explorer_snapshot("dev")

    auto_started = time.perf_counter()
    result = service.capture_explorer_path("dev", "bulk", "client", auto_payload=True)
    auto_elapsed = time.perf_counter() - auto_started
    assert result is not None and result["applied"] is True
    retained = service._explorer_snapshots["dev"]
    assert retained is not original
    assert retained.context_verified is False
    assert service.explorer_snapshot("dev") is retained
    assert retained.entries["bulk"]["changed_count"] == 0
    assert retained.entries["bulk/file-0000.txt"]["actual"] is True
    assert retained.entries["bulk/file-0000.txt"]["desired"] is True
    assert retained.entries["bulk/file-0000.txt"]["status"] == "unchanged"

    cold = ProjectService(tmp_path).explorer_snapshot("dev")
    assert cold.context_verified is True
    assert retained.entries == cold.entries
    assert retained.delta_items == cold.delta_items
    assert {parent: frozenset(children) for parent, children in retained.children.items()} == {
        parent: frozenset(children) for parent, children in cold.children.items()
    }

    def fail(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("warm ignore/page flow must not rescan or reread the instance")

    with monkeypatch.context() as patch:
        patch.setattr(explorer_module, "scan_instance", fail)
        patch.setattr(project_module, "scan_instance", fail)
        patch.setattr(LocalFilesystemInstance, "list_files", fail)
        patch.setattr(LocalFilesystemInstance, "read", fail)
        patch.setattr(service, "instance_diff", fail)
        ui_started = time.perf_counter()
        service.add_ignore("dev", "bulk/file-0001.txt", "exact", "client")
        root = service.explorer_page("dev", limit=200, target_layer="client")
        first = service.explorer_page(
            "dev", "bulk", show_ignored=True, cursor=0, limit=200, target_layer="client"
        )
        second = service.explorer_page(
            "dev", "bulk", show_ignored=True, cursor=200, limit=200, target_layer="client"
        )
        ui_elapsed = time.perf_counter() - ui_started
        identity = next(rule["identity"] for rule in root["ignore_rules"] if rule["own"])
        service.remove_ignore("dev", identity, "client")
        restored = service.explorer_page("dev", "bulk", cursor=0, limit=200)

    record_property("auto_payload_cached_700_seconds", f"{auto_elapsed:.6f}")
    record_property("warm_ignore_root_two_pages_seconds", f"{ui_elapsed:.6f}")
    assert first["total"] == 700
    assert second["total"] == 700
    assert restored["total"] == 700
    assert service._explorer_snapshots["dev"].context_verified is False

    scans = 0
    original_scan = project_module.scan_instance

    def count_scan(*args: object, **kwargs: object) -> Any:
        nonlocal scans
        scans += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(project_module, "scan_instance", count_scan)
    plan = service.explorer_plan("dev", "bulk/file-0002.txt", "client")
    assert plan["items"] == []
    assert scans == 1
    assert service._explorer_snapshots["dev"].context_verified is True


@pytest.mark.parametrize("mutation", ["add", "delete", "modify"])
def test_external_nested_change_prevents_auto_snapshot_transform(
    mutation: str, layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[], payload={"other/desired.txt": "desired"})
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    (game / "bulk/a.txt").write_text("captured")
    (game / "other").mkdir(parents=True)
    desired = game / "other/desired.txt"
    desired.write_text("desired")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    service.explorer_snapshot("dev")

    if mutation == "add":
        (game / "other/new.txt").write_text("new")
    elif mutation == "delete":
        desired.unlink()
    else:
        desired.write_text("modified-longer")

    result = service.capture_explorer_path("dev", "bulk", "client", auto_payload=True)
    assert result is not None and result["applied"] is True
    assert "dev" not in service._explorer_snapshots
    scans = 0
    original_scan = project_module.scan_instance

    def scan(*args: object, **kwargs: object) -> Any:
        nonlocal scans
        scans += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(project_module, "scan_instance", scan)
    service.explorer_snapshot("dev")
    assert scans == 1


def test_auto_payload_invalidates_other_instance_snapshots_on_shared_target(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    first_game = tmp_path.parent / f"{tmp_path.name}-first-game"
    second_game = tmp_path.parent / f"{tmp_path.name}-second-game"
    (first_game / "bulk").mkdir(parents=True)
    (first_game / "bulk/a.txt").write_text("captured")
    second_game.mkdir(parents=True)
    writer = ProjectService(tmp_path)
    writer.link("client", "dev", first_game)
    writer.link("client", "other", second_game)
    observer = ProjectService(tmp_path)
    writer.explorer_snapshot("dev")
    observed_before = observer.explorer_snapshot("other")

    result = writer.capture_explorer_path("dev", "bulk", "client", auto_payload=True)
    assert result is not None and result["applied"] is True
    assert "dev" in writer._explorer_snapshots
    assert observer._explorer_snapshots["other"] is observed_before

    scans = 0
    original_scan = project_module.scan_instance

    def scan(*args: object, **kwargs: object) -> Any:
        nonlocal scans
        scans += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(project_module, "scan_instance", scan)
    observed_after = observer.explorer_snapshot("other")
    assert scans == 1
    assert observed_after is not observed_before
    assert observed_after.entries["bulk/a.txt"]["desired"] is True
    assert observed_after.entries["bulk/a.txt"]["actual"] is False
    assert observed_after.entries["bulk/a.txt"]["status"] == "remove"


def test_warm_700_file_capture_is_one_linear_batch(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    bulk = game / "bulk"
    bulk.mkdir(parents=True)
    for index in range(700):
        (bulk / f"file-{index:04}.txt").write_bytes(bytes([index % 251]) * 1024)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    end_to_end_started = time.perf_counter()
    plan = service.explorer_plan("dev", "bulk")
    decisions = decisions_for(plan)

    counts = {"contexts": 0, "reads": 0, "metadata": 0, "copytree": 0}
    original_context = service.instance_context
    original_read = LocalFilesystemInstance.read
    original_append = service._append_operations

    def context(*args: object, **kwargs: object) -> Any:
        counts["contexts"] += 1
        return original_context(*args, **kwargs)

    def read(transport: LocalFilesystemInstance, path: str) -> bytes:
        counts["reads"] += 1
        return original_read(transport, path)

    def append(*args: object, **kwargs: object) -> None:
        counts["metadata"] += 1
        original_append(*args, **kwargs)

    def fail_list(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        raise AssertionError("warm capture must not enumerate the whole instance")

    def fail_diff(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise AssertionError("warm capture must not calculate instance_diff")

    def fail_copytree(*args: object, **kwargs: object) -> None:
        del args, kwargs
        counts["copytree"] += 1
        raise AssertionError("batch journal must not copy whole trees")

    with monkeypatch.context() as patch:
        patch.setattr(service, "instance_context", context)
        patch.setattr(LocalFilesystemInstance, "read", read)
        patch.setattr(LocalFilesystemInstance, "list_files", fail_list)
        patch.setattr(service, "instance_diff", fail_diff)
        patch.setattr(service, "_append_operations", append)
        patch.setattr(shutil, "copytree", fail_copytree)

        started = time.perf_counter()
        service.capture_explorer_path("dev", "bulk", decisions=decisions)
        elapsed = time.perf_counter() - started
    assert "dev" not in service._explorer_snapshots
    refreshed = service.explorer_page("dev")
    end_to_end_elapsed = time.perf_counter() - end_to_end_started
    record_property("capture_700_seconds", f"{elapsed:.6f}")
    record_property("plan_capture_refresh_700_seconds", f"{end_to_end_elapsed:.6f}")

    assert counts == {"contexts": 1, "reads": 700, "metadata": 0, "copytree": 0}
    assert len(tuple((service.state.feature_root("dev") / "bulk").iterdir())) == 700
    assert refreshed["items"][0]["changed_count"] == 0
    resolved = ProjectService(tmp_path).instance_context("dev")
    assert len([path for path in resolved.files if path.startswith("bulk/")]) == 700


def test_package_page_prefetch_batches_real_jars_without_instance_listing(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    mods = game / "mods"
    mods.mkdir(parents=True)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("fabric.mod.json", '{"id":"fixture","version":"1"}')
    jar_content = archive.getvalue()
    for index in range(240):
        (mods / f"fixture-{index:04}.jar").write_bytes(jar_content)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    scans = 0
    original_scan = project_module.scan_instance

    def scan(*args: object, **kwargs: object) -> Any:
        nonlocal scans
        scans += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(project_module, "scan_instance", scan)
    snapshot_started = time.perf_counter()
    service.explorer_snapshot("dev")
    snapshot_elapsed = time.perf_counter() - snapshot_started
    counts = {"contexts": 0, "enriched": 0}
    original_context = service.instance_context

    def context(*args: object, **kwargs: object) -> Any:
        counts["contexts"] += 1
        return original_context(*args, **kwargs)

    def enrich(
        content: bytes, version: str | None, *, allow_remote: bool = True
    ) -> tuple[str, list[dict[str, object]]]:
        del content, version, allow_remote
        counts["enriched"] += 1
        return "ready", []

    def fail_list(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        raise AssertionError("package prefetch must not enumerate the instance")

    with monkeypatch.context() as patch:
        patch.setattr(service, "instance_context", context)
        patch.setattr(service, "ignore_rules", lambda *args, **kwargs: [])
        patch.setattr(service, "_provider_enrichment", enrich)
        patch.setattr(LocalFilesystemInstance, "list_files", fail_list)
        page_started = time.perf_counter()
        page = service.explorer_page("dev", "mods", limit=200)
        page_elapsed = time.perf_counter() - page_started
        deadline = time.monotonic() + 10
        while service._provider_prefetches and time.monotonic() < deadline:
            time.sleep(0.005)

    record_property("package_snapshot_240_seconds", f"{snapshot_elapsed:.6f}")
    record_property("package_page_200_seconds", f"{page_elapsed:.6f}")
    assert len(page["items"]) == 200
    assert not service._provider_prefetches
    assert counts == {"contexts": 1, "enriched": 200}
    assert scans == 1


def test_warm_delta_detail_is_path_specific_and_stale_or_ignored_falls_back(
    layer_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    mods = game / "mods"
    mods.mkdir(parents=True)
    for index in range(300):
        (game / f"unrelated-{index:04}.txt").write_bytes(bytes([index % 251]) * 1024)
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as jar:
        jar.writestr("fabric.mod.json", '{"id":"selected","version":"1"}')
    selected = mods / "selected.jar"
    selected.write_bytes(archive.getvalue())
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    snapshot = service.explorer_snapshot("dev")
    delta_id = str(snapshot.entries["mods/selected.jar"]["delta_id"])
    context = service.instance_context("dev")
    baseline_lists = 0
    original_list = LocalFilesystemInstance.list_files

    def baseline_list(transport: LocalFilesystemInstance) -> tuple[str, ...]:
        nonlocal baseline_lists
        baseline_lists += 1
        return original_list(transport)

    with monkeypatch.context() as patch:
        patch.setattr(LocalFilesystemInstance, "list_files", baseline_list)
        baseline_started = time.perf_counter()
        service._quick_delta_selection(context, delta_id)
        baseline_elapsed = time.perf_counter() - baseline_started

    counts = {"contexts": 0, "reads": 0, "stats": 0, "scans": 0}
    original_context = service.instance_context
    original_read_bytes = Path.read_bytes
    original_stat = Path.stat

    def count_context(*args: object, **kwargs: object) -> Any:
        counts["contexts"] += 1
        return original_context(*args, **kwargs)

    def count_read_bytes(path: Path) -> bytes:
        if path == selected:
            counts["reads"] += 1
        return original_read_bytes(path)

    def count_stat(path: Path, *args: object, **kwargs: object) -> Any:
        if path == selected:
            counts["stats"] += 1
        return original_stat(path, *args, **kwargs)

    def fail_list(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        raise AssertionError("warm delta detail must not enumerate the instance")

    def fail_scan(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        counts["scans"] += 1
        raise AssertionError("warm delta detail must not scan the instance")

    with monkeypatch.context() as patch:
        patch.setattr(service, "instance_context", count_context)
        patch.setattr(Path, "read_bytes", count_read_bytes)
        patch.setattr(Path, "stat", count_stat)
        patch.setattr(LocalFilesystemInstance, "list_files", fail_list)
        patch.setattr(project_module, "scan_instance", fail_scan)
        patch.setattr(service, "_provider_enrichment", lambda *args, **kwargs: ("ready", []))
        warm_started = time.perf_counter()
        detail = service.delta_detail("dev", delta_id)
        warm_elapsed = time.perf_counter() - warm_started

    record_property("delta_detail_list_baseline_301_seconds", f"{baseline_elapsed:.6f}")
    record_property("delta_detail_snapshot_301_seconds", f"{warm_elapsed:.6f}")
    assert baseline_lists == 1
    assert counts == {"contexts": 1, "reads": 1, "stats": 2, "scans": 0}
    assert detail["actual_sha256"] == hashlib.sha256(selected.read_bytes()).hexdigest()

    selected.write_bytes(selected.read_bytes() + b"changed")
    stale_lists = 0

    def stale_list(transport: LocalFilesystemInstance) -> tuple[str, ...]:
        nonlocal stale_lists
        stale_lists += 1
        return original_list(transport)

    with monkeypatch.context() as patch:
        patch.setattr(LocalFilesystemInstance, "list_files", stale_list)
        patch.setattr(service, "_provider_enrichment", lambda *args, **kwargs: ("ready", []))
        refreshed = service.delta_detail("dev", delta_id)
    assert stale_lists == 1
    assert refreshed["actual_sha256"] == hashlib.sha256(selected.read_bytes()).hexdigest()

    service.add_ignore("dev", "mods/selected.jar", "exact", "client")
    with monkeypatch.context() as patch:
        patch.setattr(LocalFilesystemInstance, "list_files", fail_list)
        with pytest.raises(MetadataError, match="does not exist"):
            service.delta_detail("dev", delta_id)


def test_warm_delta_detail_preserves_delta_id_collision_rejection(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    game.mkdir(parents=True)
    (game / "selected.txt").write_text("selected")
    (game / "collision.txt").write_text("collision")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    snapshot = service.explorer_snapshot("dev")
    delta_id = str(snapshot.entries["selected.txt"]["delta_id"])
    original_delta_id = service._delta_id

    def collide(path: str) -> str:
        return delta_id if path == "collision.txt" else original_delta_id(path)

    monkeypatch.setattr(service, "_delta_id", collide)
    with pytest.raises(MetadataError, match="does not exist"):
        service.delta_detail("dev", delta_id)


def test_warm_delta_detail_rejects_delta_id_collision_added_after_snapshot(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    game.mkdir(parents=True)
    (game / "selected.txt").write_text("selected")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    snapshot = service.explorer_snapshot("dev")
    delta_id = str(snapshot.entries["selected.txt"]["delta_id"])
    original_delta_id = service._delta_id

    (game / "collision.txt").write_text("collision")

    def collide(path: str) -> str:
        return delta_id if path == "collision.txt" else original_delta_id(path)

    monkeypatch.setattr(service, "_delta_id", collide)
    with pytest.raises(MetadataError, match="does not exist"):
        service.delta_detail("dev", delta_id)


def test_stale_folder_sha_rejects_before_any_write(layer_factory, tmp_path: Path) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "bulk").mkdir(parents=True)
    selected = game / "bulk/a.txt"
    selected.write_text("first")
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "bulk")
    decisions = decisions_for(plan)
    selected.write_text("second")

    with pytest.raises(MetadataError, match="changed before mutation"):
        service.capture_explorer_path("dev", "bulk", decisions=decisions)
    assert not service.state.feature_root("dev").exists()


def test_folder_remote_provider_is_rejected_before_match_or_write(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory("client", operations=[])
    game = game_path(tmp_path)
    (game / "mods").mkdir(parents=True)
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"extra","version":"1"}')
    (game / "mods/extra.jar").write_bytes(content.getvalue())
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    plan = service.explorer_plan("dev", "mods")
    decisions = decisions_for(plan)
    decision = next(iter(decisions.values()))
    decision["mode"] = "package"
    decision["provider_match"] = {
        "provider": "direct_url",
        "download_url": "https://example.invalid/mod.jar",
        "hashes": {"sha256": hashlib.sha256(content.getvalue()).hexdigest()},
    }

    def fail_match(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("folder capture must not perform provider matching")

    monkeypatch.setattr(service.provider_matcher, "match", fail_match)
    with pytest.raises(MetadataError, match="individually"):
        service.capture_explorer_path("dev", "mods", decisions=decisions)
    assert not service.state.feature_root("dev").exists()


def test_warm_ignore_reprojects_without_instance_io_and_matches_cold_snapshot(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layer_factory(
        "base",
        operations=[
            {
                "type": "ignore",
                "selector": {"kind": "subtree", "path": "inherited"},
            }
        ],
    )
    layer_factory("client", parents=[{"path": "../base"}], operations=[])
    game = game_path(tmp_path)
    for relative in ("inherited/a.txt", "own/a.txt", "own/nested/b.txt"):
        path = game / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    service = ProjectService(tmp_path)
    service.link("client", "dev", game)
    service.explorer_snapshot("dev")

    with monkeypatch.context() as patch:

        def fail(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("warm ignore must not touch the instance filesystem")

        patch.setattr(explorer_module, "scan_instance", fail)
        patch.setattr(project_module, "scan_instance", fail)
        patch.setattr(LocalFilesystemInstance, "list_files", fail)
        patch.setattr(LocalFilesystemInstance, "read", fail)
        patch.setattr(service, "instance_diff", fail)
        service.add_ignore("dev", "own", "subtree", "client")
        warm = service.explorer_snapshot("dev")

    cold = ProjectService(tmp_path).explorer_snapshot("dev")
    warm_projection = {
        path: (entry["ignored"], entry["changed_count"], entry["visible_changed_count"])
        for path, entry in warm.entries.items()
    }
    cold_projection = {
        path: (entry["ignored"], entry["changed_count"], entry["visible_changed_count"])
        for path, entry in cold.entries.items()
    }
    assert warm_projection == cold_projection
    assert warm.entries["inherited/a.txt"]["ignored"] is True
    assert warm.entries["own/nested/b.txt"]["ignored"] is True
