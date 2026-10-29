# Typed command bridge for native clients

`GET /api/commands/capabilities` exposes a version-1 profile-scoped capability inventory. Registry names are not executable authority. Each command declares `execution_kind` (`endpoint`, `app_action`, `unsupported`), executable aliases separate from `registry_aliases`, and explicit confirmation metadata. `cli_parity:false` is intentional: terminal-/gateway-only behavior is not fabricated.

`POST /api/commands/dispatch` requires exact `session_id`, `profile`, and raw slash `command`. The active authenticated request profile and persisted session owner must agree. Only the token plus one whitespace separator is removed; the handler owns further argument interpretation. Unknown commands never become shell commands or model prompts.

Implemented server dispatch: goal through its existing handler, read-only status/usage/reasoning, allowlisted runtime commands, and actually registered plugin handlers. Local navigation and native selection/confirmation remain `app_action`. Compression, undo and filesystem rollback refuse generic dispatch even if the caller sets `confirmed:true`; use the existing dedicated review/confirmation flow. Plugin/runtime commands remain profile-wide where their underlying handler is profile-wide; session validation does not make their effects chat-local.

`goal_server_execution` reflects actual local/gateway support. See [server-owned goals](server-owned-goals.md) for opt-in, stop/budget/approval behavior and restart limitations. Legacy clients and omitted opt-in retain existing behavior.

Verification: isolated real route/HTTP/persistence tests, with synthetic model/judge only for bounded goal turns. No production deployment, native device or real model claim. Tests must use disposable state outside the production Hermes home and an explicit compatible Python interpreter; this environment uses Python 3.12 for WebUI dependencies.
