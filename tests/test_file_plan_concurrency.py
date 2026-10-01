from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mcframework.local_state import LocalState
from mcframework.project import ProjectService
from mcframework.web_server import McpackServer


@pytest.mark.parametrize("change_revision", [False, True])
def test_file_plan_allows_retry_and_rejects_concurrent_changes(
    layer_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_revision: bool
) -> None:
    layer_factory("client", operations=[])
    game = tmp_path.parent / f"{tmp_path.name}-game"
    game.mkdir()
    (game / "extra.txt").write_text("extra")
    setup = ProjectService(tmp_path)
    setup.link("client", "dev", game)
    setup.close()
    reservation = LocalState(tmp_path).reserve_web("dev")
    server = McpackServer(tmp_path, "dev", reservation["nonce"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entered = threading.Event()
    release = threading.Event()
    original_plan = server.service.explorer_plan
    invalidated: list[str] = []
    original_invalidate = server.service.invalidate_explorer

    def blocked_plan(*args, **kwargs):
        plan = original_plan(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return plan

    def invalidate(instance: str) -> None:
        invalidated.append(instance)
        original_invalidate(instance)

    monkeypatch.setattr(server.service, "explorer_plan", blocked_plan)
    monkeypatch.setattr(server.service, "invalidate_explorer", invalidate)
    base = f"http://{server.expected_host}"
    headers = {"Cookie": f"mcpack_session={server.browser_session}"}

    def fetch_plan():
        request = urllib.request.Request(
            base + "/api/v1/files/plan?path=extra.txt", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fetch_plan)
        assert entered.wait(3)
        retry = urllib.request.Request(
            base + "/api/v1/providers/retry",
            data=b"{}",
            method="POST",
            headers={
                **headers,
                "Content-Type": "application/json",
                "Origin": base,
                "X-Mcpack-CSRF": server.csrf,
                "If-Match": '"0"',
            },
        )
        # The plan remains blocked until after retry returns: no timing race can
        # hide a plan handler monopolizing the instance mutation lock.
        with urllib.request.urlopen(retry, timeout=2) as response:
            assert response.status == 200
            assert json.load(response)["revision"] == 0
        if change_revision:
            with server.state.mutation_lock("dev"):
                server.commit_revision()
        release.set()
        status, body = future.result(timeout=3)
        if change_revision:
            assert status == 409
            assert body["error"]["code"] == "revision_conflict"
            assert invalidated == ["dev"]
            assert "dev" not in server.service._explorer_snapshots
        else:
            assert status == 200
            assert body["revision"] == 0
            assert body["items"][0]["path"] == "extra.txt"
    finally:
        release.set()
        pool.shutdown(wait=True)
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
