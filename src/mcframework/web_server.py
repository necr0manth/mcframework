"""Loopback-only authenticated HTTP API and packaged SPA server."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import FrameworkError, MetadataError
from .local_state import LocalState, WebServerLockContended
from .project import ProjectService
from .sync import Synchronizer, disabled_package_aliases
from .transport import LocalFilesystemInstance


class McpackServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, project: Path, instance: str, nonce: str):
        self._owned_service: ProjectService | None = None
        self.project = project
        self.instance = instance
        self.nonce = nonce
        self.state = LocalState(project)
        reservation = self.state.read_web(instance)
        if reservation is None or reservation.get("status") != "starting":
            raise MetadataError("web reservation no longer exists")
        if reservation.get("nonce") != nonce:
            raise MetadataError("web reservation was replaced")
        super().__init__(("127.0.0.1", 0), Handler)
        try:
            self.service = ProjectService(project)
            self._owned_service = self.service
            self.management_token = str(reservation["management_token"])
            self.browser_capability: str | None = str(reservation["browser_capability"])
            self.browser_session = secrets.token_urlsafe(32)
            self.auth_lock = threading.Lock()
            self.csrf = secrets.token_urlsafe(24)
            self.revision = 0
            self.expected_host = f"127.0.0.1:{self.server_port}"
            with self.state.lock():
                latest = self.state.read_web(instance)
                if (
                    latest is None
                    or latest.get("status") != "starting"
                    or latest.get("nonce") != nonce
                ):
                    raise MetadataError("web reservation changed before startup")
                self.state.write_web(
                    instance,
                    {
                        "status": "running",
                        "nonce": nonce,
                        "management_token": self.management_token,
                        "csrf": self.csrf,
                        "pid": os.getpid(),
                        "port": self.server_port,
                        "started_at": time.time(),
                        "revision": self.revision,
                    },
                )
        except Exception:
            self.server_close()
            raise

    def server_close(self) -> None:
        service = self._owned_service
        self._owned_service = None
        try:
            super().server_close()
        finally:
            if service is not None:
                service.close()

    def bootstrap(self) -> dict[str, object]:
        result = self.service.bootstrap(self.instance, include_delta=False)
        result["csrf"] = self.csrf
        result["revision"] = self.revision
        return result

    def delta(self, revision: int | None = None) -> dict[str, object]:
        context = self.service.instance_context(self.instance)
        result = self.service.instance_diff(context, enrich_actual=False)
        result["loaded"] = True
        result["complete"] = False
        result["revision"] = self.revision if revision is None else revision
        return result

    def consume_browser_capability(self, candidate: str) -> bool:
        with self.auth_lock:
            current = self.browser_capability
            if current is None or not secrets.compare_digest(candidate, current):
                return False
            self.browser_capability = None
            return True

    def commit_revision(self) -> None:
        self.revision += 1
        with self.state.lock():
            record = self.state.read_web(self.instance)
            if record is None or record.get("nonce") != self.nonce:
                raise MetadataError("web process state changed during mutation")
            record["revision"] = self.revision
            self.state.write_web(self.instance, record)


class RevisionConflict(FrameworkError):
    def __init__(self, expected: int, actual: int):
        super().__init__("stale bootstrap revision")
        self.expected = expected
        self.actual = actual


class Handler(BaseHTTPRequestHandler):
    server: McpackServer

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/api/v1/health":
                if not self._bearer_valid():
                    self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "invalid capability")
                    return
                self._json({"ok": True, "nonce": self.server.nonce, "pid": os.getpid()})
                return
            query_token = parse_qs(parsed.query).get("token", [None])[0]
            if parsed.path == "/" and query_token is not None:
                if not self.server.consume_browser_capability(query_token):
                    self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "invalid capability")
                    return
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    f"mcpack_session={self.server.browser_session}; "
                    "HttpOnly; SameSite=Strict; Path=/",
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not self._cookie_valid():
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "open the URL from mcpack web")
                return
            if parsed.path == "/api/v1/bootstrap":
                with self.server.state.mutation_lock(self.server.instance):
                    self._json(self.server.bootstrap())
                return
            if parsed.path == "/api/v1/delta":
                with self.server.state.mutation_lock(self.server.instance):
                    revision = self.server.revision
                result = self.server.delta(revision)
                with self.server.state.mutation_lock(self.server.instance):
                    if revision != self.server.revision:
                        raise RevisionConflict(revision, self.server.revision)
                self._json(result)
                return
            if parsed.path == "/api/v1/routing":
                query = parse_qs(parsed.query)
                with self.server.state.mutation_lock(self.server.instance):
                    result = self.server.service.routing_options(
                        self.server.instance, query.get("target_layer", [None])[0]
                    )
                    result["revision"] = self.server.revision
                    self._json(result)
                return
            if parsed.path.startswith("/api/v1/delta/"):
                delta_id = unquote(parsed.path.removeprefix("/api/v1/delta/"))
                query = parse_qs(parsed.query)
                expected_revision = self._query_integer(query, "revision", -1)
                with self.server.state.mutation_lock(self.server.instance):
                    if expected_revision != self.server.revision:
                        raise RevisionConflict(expected_revision, self.server.revision)
                item = self.server.service.delta_detail(self.server.instance, delta_id)
                with self.server.state.mutation_lock(self.server.instance):
                    if expected_revision != self.server.revision:
                        raise RevisionConflict(expected_revision, self.server.revision)
                self._json({"revision": expected_revision, "item": item})
                return
            if parsed.path == "/api/v1/files":
                query = parse_qs(parsed.query)
                with self.server.state.mutation_lock(self.server.instance):
                    self._json(
                        self.server.service.explorer_page(
                            self.server.instance,
                            query.get("parent", [""])[0],
                            mode=query.get("mode", ["all"])[0],
                            show_ignored=query.get("show_ignored", ["false"])[0].lower() == "true",
                            cursor=self._query_integer(query, "cursor", 0),
                            limit=self._query_integer(query, "limit", 200),
                            target_layer=query.get("target_layer", [None])[0],
                        )
                    )
                return
            if parsed.path == "/api/v1/files/plan":
                query = parse_qs(parsed.query)
                path = query.get("path", [""])[0]
                with self.server.state.mutation_lock(self.server.instance):
                    revision = self.server.revision
                plan = self.server.service.explorer_plan(
                    self.server.instance,
                    path,
                    query.get("target_layer", [None])[0],
                )
                with self.server.state.mutation_lock(self.server.instance):
                    if revision != self.server.revision:
                        self.server.service.invalidate_explorer(self.server.instance)
                        raise RevisionConflict(revision, self.server.revision)
                plan["revision"] = revision
                self._json(plan)
                return
            self._static(parsed.path)
        except RevisionConflict as exc:
            self._error(
                HTTPStatus.CONFLICT,
                "revision_conflict",
                str(exc),
                {"expected": exc.expected, "actual": exc.actual},
            )
        except FrameworkError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "framework_error", str(exc))
        except Exception:
            self.log_error("unexpected GET failure\n%s", traceback.format_exc())
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "internal server error")

    def do_POST(self) -> None:
        self._mutate("POST")

    def do_PATCH(self) -> None:
        self._mutate("PATCH")

    def do_DELETE(self) -> None:
        self._mutate("DELETE")

    def _mutate(self, method: str) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/v1/shutdown" and self._bearer_valid():
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._require_mutation_auth()
            with self.server.state.mutation_lock(self.server.instance):
                request_revision = self._require_revision()
                body = self._read_json()
                metadata_write = body.get("metadata_write", {})
                if (
                    not isinstance(metadata_write, dict)
                    or set(metadata_write) - {"file", "section"}
                ):
                    raise MetadataError("metadata_write must contain only file and section")
                destination = self._optional_string(metadata_write, "file")
                section = self._optional_string(metadata_write, "section")
                if method == "POST" and parsed.path == "/api/v1/providers/retry":
                    retried = self.server.service.retry_failed_provider_lookups()
                    self._json(
                        {"ok": True, "revision": self.server.revision, "retried": retried}
                    )
                    return
                if method == "POST" and parsed.path == "/api/v1/routing/schema/preview":
                    plan = self.server.service.preview_schema_upgrade(
                        self.server.instance, self._optional_string(body, "target_layer")
                    )
                    plan["revision"] = self.server.revision
                    self._json(plan)
                    return
                if method == "POST" and parsed.path == "/api/v1/routing/preview":
                    operation = body.get("operation")
                    if not isinstance(operation, dict):
                        raise MetadataError("operation must be an object")
                    plan = self.server.service.preview_routing(
                        self.server.instance, operation,
                        self._optional_string(body, "target_layer"),
                        file=destination, section=section,
                        origin=self._optional_string(body, "origin") or "user",
                    )
                    plan["revision"] = self.server.revision
                    self._json(plan)
                    return
                if parsed.path == "/api/v1/files/capture":
                    auto_payload = body.get("auto_payload", False)
                    if not isinstance(auto_payload, bool):
                        raise MetadataError("auto_payload must be a boolean")
                    if auto_payload:
                        result = self.server.service.capture_explorer_path(
                            self.server.instance,
                            self._string(body, "path"),
                            self._optional_string(body, "target_layer"),
                            auto_payload=True,
                        )
                        if not isinstance(result, dict):
                            raise RuntimeError("auto payload capture returned no result")
                        if result["applied"]:
                            self.server.commit_revision()
                            self._json(
                                {
                                    "ok": True,
                                    "applied": True,
                                    "revision": self.server.revision,
                                    "explorer_patch": {
                                        "path": result["path"],
                                        "captured_count": result["captured_count"],
                                    },
                                }
                            )
                        else:
                            self._json(
                                {
                                    "ok": True,
                                    "applied": False,
                                    "revision": self.server.revision,
                                    "decision_required": {
                                        "path": result["path"],
                                        "items": result["items"],
                                    },
                                }
                            )
                        return
                    plan_revision = body.get("plan_revision")
                    if not isinstance(plan_revision, int) or isinstance(plan_revision, bool):
                        raise MetadataError("plan_revision must be an integer")
                    if plan_revision != request_revision or plan_revision != self.server.revision:
                        raise RevisionConflict(plan_revision, self.server.revision)
                with self.server.service.metadata_destination(destination, section):
                    should_shutdown = self._dispatch_mutation(method, parsed.path, body)
                if parsed.path not in {
                    "/api/v1/files/capture",
                    "/api/v1/files/ignore",
                }:
                    self.server.service.invalidate_explorer(self.server.instance)
                self.server.commit_revision()
                if should_shutdown:
                    self._json({"ok": True, "revision": self.server.revision})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self._json(self.server.bootstrap())
        except RevisionConflict as exc:
            self._error(
                HTTPStatus.CONFLICT,
                "revision_conflict",
                str(exc),
                {"expected": exc.expected, "actual": exc.actual},
            )
        except FrameworkError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "framework_error", str(exc))
        except (UnicodeError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", str(exc))
        except Exception:
            self.log_error("unexpected mutation failure\n%s", traceback.format_exc())
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "internal server error")

    def _dispatch_mutation(self, method: str, path: str, body: dict[str, object]) -> bool:
        route = path.removeprefix("/api/v1/")
        if method == "POST" and route == "features":
            self.server.service.add_feature(self.server.instance, self._string(body, "feature"))
        elif method == "DELETE" and route.startswith("features/"):
            self.server.service.remove_feature(
                self.server.instance, unquote(route.removeprefix("features/"))
            )
        elif method == "PATCH" and route.startswith("packages/") and route.endswith("/activation"):
            package_id = unquote(route[len("packages/") : -len("/activation")])
            self.server.service.set_activation(
                self.server.instance,
                package_id,
                self._string(body, "state"),
                self._optional_string(body, "target_layer"),
                move_disabled_mod=True,
            )
        elif method == "PATCH" and route.startswith("groups/") and route.endswith("/activation"):
            group_id = unquote(route[len("groups/") : -len("/activation")])
            self.server.service.set_group_activation(
                self.server.instance,
                group_id,
                self._string(body, "state"),
                self._optional_string(body, "target_layer"),
            )
        elif method == "PATCH" and route.startswith("sections/") and route.endswith("/activation"):
            section_id = unquote(route[len("sections/") : -len("/activation")])
            self.server.service.set_section_activation(
                self.server.instance,
                section_id,
                self._string(body, "state"),
                self._optional_string(body, "target_layer"),
            )
        elif method == "POST" and route.startswith("delta/") and route.endswith("/capture"):
            delta_id = unquote(route[len("delta/") : -len("/capture")])
            self.server.service.capture_delta(
                self.server.instance,
                delta_id,
                self._optional_string(body, "target_layer"),
                expected_sha256=self._optional_string(body, "expected_sha256"),
                require_expected_sha256=True,
            )
        elif method == "POST" and route == "files/capture":
            self.server.service.capture_explorer_path(
                self.server.instance,
                self._string(body, "path"),
                self._optional_string(body, "target_layer"),
                body.get("decisions"),
            )
        elif method == "POST" and route == "files/ignore":
            self.server.service.add_ignore(
                self.server.instance,
                self._string(body, "path"),
                self._string(body, "kind"),
                self._optional_string(body, "target_layer"),
            )
        elif method == "DELETE" and route == "files/ignore":
            self.server.service.remove_ignore(
                self.server.instance,
                body.get("identity"),
                self._optional_string(body, "target_layer"),
            )
        elif method == "POST" and route == "packages/import":
            self.server.service.import_package(
                self.server.instance,
                self._string(body, "delta_id"),
                self._optional_string(body, "target_layer"),
                self._optional_provider_match(body),
                expected_sha256=self._string(body, "expected_sha256"),
            )
        elif method == "DELETE" and route.startswith("packages/"):
            self.server.service.remove_package(
                self.server.instance, unquote(route.removeprefix("packages/")),
                mode=self._string(body, "mode"),
                target_layer=self._optional_string(body, "target_layer"),
                move_disabled_mod=True,
            )
        elif method == "POST" and route == "routing/rules":
            rule = body.get("rule")
            if not isinstance(rule, dict):
                raise MetadataError("rule must be an object")
            self.server.service.save_routing_rule(
                self.server.instance, rule, self._string(body, "scope"),
                self._optional_string(body, "target_layer"),
                file=self._optional_string(body, "destination"),
            )
        elif method == "DELETE" and route.startswith("routing/rules/"):
            self.server.service.remove_routing_rule(
                self.server.instance, unquote(route.removeprefix("routing/rules/")),
                self._string(body, "scope"), self._optional_string(body, "target_layer"),
            )
        elif method == "POST" and route == "routing/schema/upgrade":
            self.server.service.upgrade_layer_schema(
                self.server.instance, self._optional_string(body, "target_layer")
            )
        elif method == "POST" and route == "sync":
            context = self.server.service.instance_context(self.server.instance)
            Synchronizer().sync(
                context.files,
                LocalFilesystemInstance(context.instance.path),
                context.resolution.state.content.prune,
                disabled_package_aliases(context.resolution.state.packages),
            )
        elif method == "POST" and route == "shutdown":
            return True
        else:
            raise MetadataError("API route does not exist")
        return False

    def _require_mutation_auth(self) -> None:
        if not self._cookie_valid():
            raise MetadataError("invalid web session")
        if not secrets.compare_digest(self.headers.get("X-Mcpack-CSRF", ""), self.server.csrf):
            raise MetadataError("invalid CSRF token")
        if self.headers.get("Host") != self.server.expected_host:
            raise MetadataError("invalid Host header")
        origin = self.headers.get("Origin")
        if origin != f"http://{self.server.expected_host}":
            raise MetadataError("invalid Origin header")

    def _require_revision(self) -> int:
        value = self.headers.get("If-Match", "")
        if len(value) < 3 or not value.startswith('"') or not value.endswith('"'):
            raise RevisionConflict(-1, self.server.revision)
        raw = value[1:-1]
        if not raw.isdecimal():
            raise RevisionConflict(-1, self.server.revision)
        expected = int(raw)
        if expected != self.server.revision:
            raise RevisionConflict(expected, self.server.revision)
        return expected

    def _cookie_valid(self) -> bool:
        cookies = {}
        for raw in self.headers.get("Cookie", "").split(";"):
            if "=" in raw:
                key, value = raw.strip().split("=", 1)
                cookies[key] = value
        candidate = cookies.get("mcpack_session", "")
        return bool(candidate) and secrets.compare_digest(candidate, self.server.browser_session)

    def _bearer_valid(self) -> bool:
        candidate = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(candidate) and secrets.compare_digest(candidate, self.server.management_token)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise MetadataError("request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise MetadataError("request body must be a JSON object")
        return value

    @staticmethod
    def _string(body: dict[str, object], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise MetadataError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _optional_string(body: dict[str, object], key: str) -> str | None:
        value = body.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise MetadataError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _optional_provider_match(body: dict[str, object], key: str = "provider_match") -> object:
        value = body.get(key)
        if value is not None and not isinstance(value, dict):
            raise MetadataError(f"{key} must be an object")
        return value

    @staticmethod
    def _query_integer(query: dict[str, list[str]], key: str, default: int) -> int:
        raw = query.get(key, [str(default)])[0]
        try:
            return int(raw)
        except ValueError as exc:
            raise MetadataError(f"{key} must be an integer") from exc

    def _static(self, request_path: str) -> None:
        static_root = Path(__file__).with_name("web_static")
        relative = request_path.lstrip("/") or "index.html"
        target = (static_root / relative).resolve()
        if (
            static_root.is_dir()
            and target.is_relative_to(static_root.resolve())
            and target.is_file()
        ):
            content = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        elif request_path in {"/", "/index.html"}:
            content = _FALLBACK_HTML
            content_type = "text/html; charset=utf-8"
        elif static_root.is_dir() and (static_root / "index.html").is_file():
            content = (static_root / "index.html").read_bytes()
            content_type = "text/html; charset=utf-8"
        else:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "static asset does not exist")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        error: dict[str, object] = {"code": code, "message": message}
        if details is not None:
            error["details"] = details
        self._json({"error": error}, status)


_FALLBACK_HTML = b"""<!doctype html><html><head><meta charset=utf-8><title>mcpack</title></head>
<body><h1>mcpack</h1><p>Frontend assets are not installed.</p></body></html>"""


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv or sys.argv[1:])
    if len(arguments) != 3:
        return 2
    project, instance, nonce = Path(arguments[0]).resolve(), arguments[1], arguments[2]
    state = LocalState(project)
    server: McpackServer | None = None
    try:
        with state.web_server_lock():
            try:
                server = McpackServer(project, instance, nonce)
                server.serve_forever(poll_interval=0.1)
                return 0
            finally:
                if server is not None:
                    server.server_close()
                try:
                    with state.lock():
                        current = state.read_web(instance)
                        if current is not None and current.get("nonce") == nonce:
                            state.web_path(instance).unlink(missing_ok=True)
                except FrameworkError:
                    pass
    except WebServerLockContended as exc:
        try:
            with state.lock():
                current = state.read_web(instance)
                if current is not None and current.get("nonce") == nonce:
                    state.web_path(instance).unlink(missing_ok=True)
        except FrameworkError:
            pass
        print(f"web server error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"web server error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
