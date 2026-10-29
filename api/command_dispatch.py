"""Capability-driven HTTP slash bridge, not a CLI or prompt fallback.

Dedicated responses are deliberately returned verbatim (notably goal runs).
Only the command token and ONE whitespace separator are removed from arguments.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)
_RUNTIME = {
    "reload-mcp": "_run_reload_mcp_command",
    "reload-skills": "_run_reload_skills_command",
    "codex-runtime": "_run_codex_runtime_command",
    "credits": "_run_credits_command",
}
_ALIASES = {"reload_mcp": "reload-mcp", "reload_skills": "reload-skills", "codex_runtime": "codex-runtime", "compact": "compress", "regenerate": "retry", "new-chat": "new"}
_READ = {"status": "/api/session/status", "usage": "/api/session/usage", "reasoning": "/api/reasoning"}
_APP = {"help": "show_help", "new": "new_session", "model": "select_model", "personality": "select_personality", "title": "rename_session", "stop": "stop_current_run", "retry": "retry_confirmed", "workspace": "select_workspace"}
_APP_CONFIRM = {"retry"}
_DANGEROUS = {"compress": "/api/session/compress/start", "rollback": "/api/rollback/restore", "undo": "/api/session/undo"}


def parse_command(raw):
    if not isinstance(raw, str):
        raise ValueError("command must be a raw slash-command string")
    match = re.match(r"\A/([a-zA-Z0-9_-]+)(?:\s|$)", raw)
    if not match:
        raise ValueError("command must start with a slash command token")
    name = match.group(1).lower()
    return _ALIASES.get(name, name), raw[match.end():]


def capabilities():
    from api.commands import list_commands
    from api.profiles import profile_env_for_active_request
    with profile_env_for_active_request("command capabilities", logger_override=logger):
        rows = {row["name"]: dict(row) for row in list_commands()}
    for name in set(_RUNTIME) | set(_READ) | set(_APP) | set(_DANGEROUS) | {"goal"}:
        rows.setdefault(name, {"name": name, "aliases": []})
    for alias in _ALIASES:
        rows.pop(alias, None)
    for name, row in rows.items():
        row.update(execution_kind="unsupported", reason="No safe HTTP execution adapter; CLI inventory is not transport support.")
        if name == "goal" or name in _RUNTIME or name in _READ or row.get("category") == "Plugin":
            row.update(execution_kind="endpoint", endpoint="/api/commands/dispatch", method="POST", scope="session_profile")
            row.pop("reason", None)
        if name == "goal":
            row.update(response_semantics="dedicated_handler_passthrough", optional_fields=["model", "model_provider", "explicit_model_pick", "workspace", "goal_execution"])
        if name in _APP or name in _DANGEROUS:
            row.update(execution_kind="app_action", app_action=_APP.get(name, name))
            row.pop("reason", None)
        if name in _READ:
            row.update(read_only=True, args_supported=False)
        if name in _APP_CONFIRM:
            row.update(requires_confirmation=True, mutates_server_state=False)
        if name in _DANGEROUS:
            row.update(requires_confirmation=True, dispatch_supported=False, dedicated_endpoint=_DANGEROUS[name], reason="Use the dedicated confirmation flow; dispatch never performs this mutation.")
        row["registry_aliases"] = list(row.get("aliases", []))
        # aliases is executable transport support, not unverified CLI metadata.
        row["aliases"] = sorted(alias for alias, target in _ALIASES.items() if target == name)
        row["dispatch_aliases"] = list(row["aliases"])
        row.setdefault("dispatch_supported", row["execution_kind"] in {"endpoint", "app_action"})
        if row["execution_kind"] == "app_action":
            row["mutates_server_state"] = False
    from api.goal_execution import capabilities as goal_capabilities
    return {"version": 1, "goal_server_execution": goal_capabilities().get("server_owned_goal_continuation") is True, "commands": [rows[name] for name in sorted(rows)], "argument_semantics": "Remove slash token and one whitespace separator; preserve all remaining characters.", "unknown_command_policy": "reject_no_prompt_fallback", "profile_semantics": "Required exact active request profile and session owner (default represents legacy null owner).", "cli_parity": False}


def dispatch(handler, body):
    from api import routes, commands
    from urllib.parse import urlencode, urlparse

    def refuse(message, status=400, code="invalid_command"):
        return routes.j(handler, {"error": message, "code": code}, status=status)

    if not isinstance(body, dict):
        return refuse("JSON object required")
    sid, profile = body.get("session_id"), body.get("profile")
    if not isinstance(sid, str) or not sid or not routes.is_safe_session_id(sid):
        return refuse("Exact valid session_id is required")
    if not isinstance(profile, str) or not profile or profile != routes._get_active_profile_name():
        return refuse("profile must exactly match the active request profile", 409, "session_profile_mismatch")
    try:
        owner = routes.get_session_profile_readonly(sid) or "default"
    except KeyError:
        return refuse("Session not found", 404, "session_not_found")
    if owner != profile:
        return refuse("Session belongs to a different profile", 409, "session_profile_mismatch")
    if not routes._session_id_visible_to_request_profile(handler, sid):
        return True
    try:
        name, args = parse_command(body.get("command"))
    except ValueError as exc:
        return refuse(str(exc))
    if name in _DANGEROUS:
        return refuse("Use the dedicated explicit confirmation flow; dispatch cannot confirm or execute this command", 409, "confirmation_required")
    if name in _APP:
        return routes.j(handler, {"execution_kind": "app_action", "app_action": _APP[name], "requires_confirmation": name in _APP_CONFIRM, "mutates_server_state": False, "command": name, "args": args, "session_id": sid, "profile": profile})
    if name == "goal":
        # Preserve optional model/workspace fields and the trusted handler's run.
        forwarded = {key: body[key] for key in ("session_id", "profile", "model", "model_provider", "explicit_model_pick", "workspace", "goal_execution") if key in body}
        forwarded["args"] = args
        return routes._handle_goal_command(handler, forwarded)
    if name in _READ:
        if args.strip():
            return refuse("This read-only command does not accept arguments")
        return routes.handle_get(handler, urlparse(_READ[name] + "?" + urlencode({"session_id": sid})))
    try:
        from api.profiles import profile_env_for_active_request
        with profile_env_for_active_request("command dispatch", logger_override=logger):
            if name in _RUNTIME:
                if name != "codex-runtime" and args.strip():
                    return refuse("This runtime command does not accept arguments")
                function = getattr(commands, _RUNTIME[name])
                output = function(args) if name == "codex-runtime" else function()
                return routes.j(handler, {"output": output})
            # Resolve actual installed plugin handlers; never a CLI/shell/model.
            inventory = {row["name"]: row for row in commands.list_commands()}
            if inventory.get(name, {}).get("category") != "Plugin":
                return refuse("Command has no safe server adapter", 422, "unsupported_command")
            from hermes_cli.plugins import get_plugin_command_handler, resolve_plugin_command_result
            plugin = get_plugin_command_handler(name)
            if plugin is None:
                return refuse("Plugin command unavailable", 404, "unsupported_command")
            try:
                output = str(resolve_plugin_command_result(plugin(args)) or "(no output)")
            except Exception as exc:
                logger.warning("Plugin dispatch failed", exc_info=True)
                output = f"Plugin command error: {type(exc).__name__}"
            return routes.j(handler, {"output": output})
    except (ImportError, RuntimeError):
        logger.warning("Command runtime unavailable", exc_info=True)
        return refuse("Command runtime unavailable", 503, "runtime_unavailable")
    except ValueError as exc:
        return refuse(str(exc))
