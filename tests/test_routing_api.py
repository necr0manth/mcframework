from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from mcframework.local_state import LocalState
from mcframework.project import ProjectService
from mcframework.web_server import McpackServer


def package(identity):
    return {
        "type": "package",
        "id": identity,
        "kind": "mod",
        "path_mode": "layer",
        "activation": "available",
        "destination": f"mods/{identity}.jar",
        "source": {"provider": "local", "location": "missing", "sha256": "0" * 64},
    }


@pytest.fixture
def routing_server(tmp_path):
    project = tmp_path / "project"
    for name, parents, operations in [
        ("base", [], [package("inherited")]),
        ("child", ["../base"], [package("own")]),
    ]:
        directory = project / name / ".pack"
        directory.mkdir(parents=True)
        (directory / "layer.json").write_text(
            json.dumps({"schema": 2, "id": name, "parents": parents})
        )
        (directory / "packages.json").write_text(json.dumps({"operations": operations}))
    game = tmp_path / "game"
    game.mkdir()
    service = ProjectService(project)
    service.link("child", "dev", game)
    reservation = LocalState(project).reserve_web("dev")
    server = McpackServer(project, "dev", reservation["nonce"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, project
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def request(server, route, body=None, *, method="POST", revision=None, csrf=None):
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"mcpack_session={server.browser_session}",
        "X-Mcpack-CSRF": server.csrf if csrf is None else csrf,
        "If-Match": f'"{server.revision if revision is None else revision}"',
        "Origin": f"http://{server.expected_host}",
    }
    req = urllib.request.Request(
        f"http://{server.expected_host}/api/v1/{route}",
        data=json.dumps(body or {}).encode() if method != "GET" else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_preview_is_read_only_and_manual_ignore_is_one_shot(routing_server):
    server, project = routing_server
    write = {"file": "config/.pack/my-ignore.json"}
    status, preview = request(
        server,
        "routing/preview",
        {
            "target_layer": "child",
            "metadata_write": write,
            "operation": {"type": "ignore", "selector": {"kind": "exact", "path": "a.cfg"}},
        },
    )
    assert status == 200, preview
    assert preview["writes"][0]["file"] == write["file"]
    assert preview["revision"] == server.revision == 0
    assert not (project / "child" / write["file"]).exists()
    status, result = request(
        server,
        "files/ignore",
        {
            "path": "a.cfg",
            "kind": "exact",
            "target_layer": "child",
            "metadata_write": write,
        },
    )
    assert status == 200, result
    assert (project / "child" / write["file"]).is_file()
    status, result = request(
        server,
        "files/ignore",
        {
            "path": "b.cfg",
            "kind": "exact",
            "target_layer": "child",
        },
    )
    assert status == 200, result
    assert (project / "child/.pack/ignore.json").is_file()
    assert not (project / "child/.pack/routing.json").exists()


def test_layer_and_instance_rule_save_are_explicit_and_preview_honors_override(routing_server):
    server, project = routing_server
    raw = {
        "type": "routing_rule",
        "id": "ignores",
        "when": {"operation_types": ["ignore"]},
        "write": {"file": "tracked-ignore.json"},
    }
    status, result = request(
        server,
        "routing/rules",
        {
            "target_layer": "child",
            "scope": "layer",
            "rule": raw,
        },
    )
    assert status == 200, result
    tracked = project / "child/.pack/routing.json"
    assert tracked.is_file()
    before = tracked.read_bytes()
    status, result = request(
        server,
        "routing/rules",
        {
            "target_layer": "child",
            "scope": "instance",
            "rule": {**raw, "write": {"file": "local-ignore.json"}},
        },
    )
    assert status == 200, result
    assert tracked.read_bytes() == before
    assert LocalState(project).routing_rules("dev")[0]["id"] == "ignores"
    status, result = request(
        server,
        "routing/preview",
        {
            "target_layer": "child",
            "operation": {"type": "ignore", "glob": "logs/**"},
        },
    )
    assert status == 200, result
    assert result["writes"][0]["file"] == ".pack/local-ignore.json"
    status, result = request(server, "routing?target_layer=child", method="GET")
    assert status == 200, result
    assert len(result["layer_rules"]) == len(result["instance_rules"]) == 1


def test_existing_package_keeps_source_and_remove_only_deletes_owned_declaration(routing_server):
    server, project = routing_server
    source = project / "child/.pack/packages.json"
    status, result = request(
        server,
        "packages/own",
        {
            "target_layer": "child",
            "mode": "disable",
            "metadata_write": {"file": "mods/.pack/other.json"},
        },
        method="DELETE",
    )
    assert status == 200, result
    assert json.loads(source.read_text())["operations"][0]["activation"] == "disabled"
    assert not (project / "child/mods/.pack/other.json").exists()
    status, result = request(
        server,
        "packages/own",
        {
            "target_layer": "child",
            "mode": "remove",
        },
        method="DELETE",
    )
    assert status == 200, result
    assert json.loads(source.read_text())["operations"] == []
    parent = (project / "base/.pack/packages.json").read_bytes()
    status, result = request(
        server,
        "packages/inherited",
        {
            "target_layer": "child",
            "mode": "remove",
        },
        method="DELETE",
    )
    assert status == 200, result
    assert (project / "base/.pack/packages.json").read_bytes() == parent
    assert (
        next(item for item in result["packages"] if item["id"] == "inherited")["activation"]
        == "disabled"
    )


def test_preview_and_rule_mutations_enforce_security_revision_and_validation(routing_server):
    server, project = routing_server
    body = {"target_layer": "child", "operation": {"type": "ignore", "glob": "logs/**"}}
    assert request(server, "routing/preview", body, csrf="bad")[0] == 400
    assert request(server, "routing/preview", body, revision=999)[0] == 409
    assert (
        request(server, "routing/preview", {**body, "metadata_write": {"file": "C:/bad.json"}})[0]
        == 400
    )
    assert request(server, "routing/preview", {**body, "metadata_write": {"typo": "x"}})[0] == 400
    assert (
        request(
            server,
            "routing/rules",
            {
                "scope": "instance",
                "rule": {
                    "type": "routing_rule",
                    "id": "bad",
                    "when": {"package.knd": "mod"},
                },
            },
        )[0]
        == 400
    )
    assert server.revision == 0
    assert not LocalState(project).routing_rules_path("dev").exists()
