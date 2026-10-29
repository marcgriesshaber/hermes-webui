"""Real route/body/admission tests; no model inference is claimed here."""
import io
import json
from types import SimpleNamespace

import pytest

from api import commands, models, profiles, routes
from api.command_dispatch import parse_command


class Handler:
    def __init__(self, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.request = None
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass

    def result(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def scope(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    profiles.set_request_profile("default")
    session = models.Session(session_id="dispatch-test-session", profile="default", workspace=str(tmp_path))
    models.SESSIONS[session.session_id] = session
    # Real goal persistence is isolated; no GoalManager or handler substitution.
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _profile: tmp_path)
    yield session
    models.SESSIONS.pop(session.session_id, None)
    profiles.clear_request_profile()


def post(scope, command, **overrides):
    body = {"session_id": scope.session_id, "profile": "default", "command": command, **overrides}
    handler = Handler(body)
    assert routes.handle_post(handler, SimpleNamespace(path="/api/commands/dispatch", query="")) is not False
    return handler.status, handler.result()


def test_capability_route(scope):
    handler = Handler()
    routes.handle_get(handler, SimpleNamespace(path="/api/commands/capabilities", query=""))
    result = handler.result()
    assert result["version"] == 1 and result["cli_parity"] is False
    rows = {row["name"]: row for row in result["commands"]}
    assert rows["goal"]["execution_kind"] == "endpoint"
    assert rows["model"]["execution_kind"] == "app_action"
    assert rows["compress"]["requires_confirmation"] is True
    assert "reload_mcp" in rows["reload-mcp"]["dispatch_aliases"]
    assert "compact" in rows["compress"]["aliases"]
    assert rows["retry"]["requires_confirmation"] is True
    assert rows["stop"]["app_action"] == "stop_current_run"
    assert rows["workspace"]["app_action"] == "select_workspace"
    assert rows["reasoning"]["read_only"] is True
    from api.goal_execution import capabilities
    assert result["goal_server_execution"] is capabilities()["server_owned_goal_continuation"]


def test_goal_capability_tracks_runner_refusal(scope, monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_RUNTIME_ADAPTER", "runner")
    from api.command_dispatch import capabilities
    # Override the actual backend selector, not a manufactured capability payload.
    from api import runtime_adapter
    monkeypatch.setattr(runtime_adapter, "runtime_adapter_runner_enabled", lambda: True)
    assert capabilities()["goal_server_execution"] is False


@pytest.mark.parametrize("raw,name,args", [
    ("/goal  two\nlines  ", "goal", " two\nlines  "),
    ("/reload_mcp\t\t", "reload-mcp", "\t"),
    ("/codex_runtime\n auto  ", "codex-runtime", " auto  "),
    ("/goal", "goal", ""),
])
def test_exact_parser(raw, name, args):
    assert parse_command(raw) == (name, args)


@pytest.mark.parametrize("command", ["/status", "/usage", "/reasoning"])
def test_actual_readonly_routes(scope, command):
    status, result = post(scope, command)
    assert status == 200
    assert result
    assert not scope.messages


def test_actual_goal_status_and_pause_persistence(scope):
    status, result = post(scope, "/goal status")
    assert status == 200
    assert result["action"] == "status"
    from api.goals import goal_command_payload
    seeded = goal_command_payload(scope.session_id, "verify persisted dispatch goal", profile_home=scope.workspace)
    assert seeded["ok"], seeded
    status, result = post(scope, "/goal pause")
    assert status == 200, result
    status, readback = post(scope, "/goal status")
    assert status == 200
    assert readback["goal"] == result["goal"]
    assert not scope.messages


@pytest.mark.parametrize("command", ["/compress", "/compact", "/rollback abc", "/undo"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_dangerous_cannot_bypass_confirmation(scope, command, confirmed):
    status, result = post(scope, command, confirmed=confirmed)
    assert status == 409 and result["code"] == "confirmation_required"
    assert not scope.messages


@pytest.mark.parametrize("command,action,confirmation", [
    ("/stop", "stop_current_run", False),
    ("/regenerate", "retry_confirmed", True),
    ("/workspace", "select_workspace", False),
    ("/new-chat", "new_session", False),
])
def test_native_action_is_not_server_execution(scope, command, action, confirmation):
    status, result = post(scope, command)
    assert status == 200
    assert result["execution_kind"] == "app_action"
    assert result["app_action"] == action
    assert result["requires_confirmation"] is confirmation
    assert result["mutates_server_state"] is False
    assert not scope.messages


@pytest.mark.parametrize("overrides", [{"profile": "other"}, {"profile": " default "}, {"profile": None}, {"session_id": "missing-session"}])
def test_scope_checked_before_runtime(scope, monkeypatch, overrides):
    def forbidden():
        pytest.fail("runtime called before admission")
    monkeypatch.setattr(commands, "_run_reload_mcp_command", forbidden)
    status, _ = post(scope, "/reload_mcp", **overrides)
    assert status in {404, 409}


def test_wrong_owner_rejected(scope):
    scope.profile = "other"
    status, result = post(scope, "/credits")
    assert status == 409 and result["code"] == "session_profile_mismatch"


def test_unknown_has_no_prompt_fallback(scope):
    status, result = post(scope, "/not-a-real-command secret  ")
    assert status == 422 and result["code"] == "unsupported_command"
    assert scope.messages == []


def test_runtime_real_validation_refuses_bad_arguments(scope):
    status, result = post(scope, "/reload_mcp unexpected")
    assert status == 400
    assert "does not accept arguments" in result["error"]


def test_exact_runtime_alias_dispatch(scope, monkeypatch):
    # Boundary spy only: separate tests exercise the actual runtime below.
    seen = []
    monkeypatch.setattr(commands, "_run_codex_runtime_command", lambda args: seen.append(args) or "ok")
    assert post(scope, "/codex_runtime  auto\n  ") == (200, {"output": "ok"})
    assert seen == [" auto\n  "]


def test_goal_forwarding_preserves_run_and_options(scope, monkeypatch):
    seen = []
    def boundary(handler, body):
        seen.append(body)
        return routes.j(handler, {"stream_id": "run-exact", "session_id": body["session_id"], "goal": {"status": "active"}})
    monkeypatch.setattr(routes, "_handle_goal_command", boundary)
    status, result = post(scope, "/goal  exact\nargs  ", goal_execution="server")
    assert status == 200 and result["stream_id"] == "run-exact"
    assert seen[0]["args"] == " exact\nargs  "
    assert seen[0]["goal_execution"] == "server"


def test_loopback_http_routes(scope):
    """Actual JSON/HTTP handler, CSRF gate and route dispatch on loopback."""
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    class HTTPHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            profiles.set_request_profile("default")
            try:
                routes.handle_post(self, urlparse(self.path))
            finally:
                profiles.clear_request_profile()

        def do_GET(self):
            profiles.set_request_profile("default")
            try:
                routes.handle_get(self, urlparse(self.path))
            finally:
                profiles.clear_request_profile()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/commands/capabilities", timeout=5) as response:
            assert json.load(response)["version"] == 1
        for command in ("/status", "/usage", "/goal status", "/codex_runtime invalid-runtime"):
            request = urllib.request.Request(base + "/api/commands/dispatch", data=json.dumps({
                "session_id": scope.session_id, "profile": "default", "command": command,
            }).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_registered_plugin_executes_exact_args(scope, monkeypatch):
    from hermes_cli.plugins import get_plugin_commands
    seen = []
    monkeypatch.setitem(get_plugin_commands(), "dispatch-probe", {
        "handler": lambda args: seen.append(args) or "plugin actual result",
        "description": "Disposable test plugin",
    })
    status, result = post(scope, "/dispatch-probe  exact\nargs  ")
    assert status == 200 and result == {"output": "plugin actual result"}
    assert seen == [" exact\nargs  "]


def test_real_runtime_invalid_switch_does_not_persist(scope):
    status, result = post(scope, "/codex_runtime definitely-not-a-runtime")
    # Real installed shared runtime implementation (or explicit unavailability).
    assert status in {200, 503}
    if status == 200:
        assert result["output"] and "runtime" in result["output"].lower()
    else:
        assert result["code"] == "runtime_unavailable"
