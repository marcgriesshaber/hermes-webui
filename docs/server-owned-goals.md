# Optional server-owned goal turns

## Contract

`GET /api/goal/capabilities` returns:

```json
{"server_owned_goal_continuation":true,"goal_execution_modes":["client","server"],"restart_recovery":false}
```

The runner backend advertises false and refuses server opt-in with HTTP 501,
before mutating any goal. Local and gateway-chat workers support the handoff.

`POST /api/goal` retains the existing body and adds optional
`"goal_execution":"server"`. Omission or `"client"` retains legacy behavior.
Other values, including booleans, are rejected with 400. For example:

```json
{"session_id":"EXACT_SESSION_ID","profile":"default","args":"Complete the task","goal_execution":"server","model":"EXPLICIT_MODEL","model_provider":"EXPLICIT_PROVIDER","explicit_model_pick":true}
```

Successful set returns the existing kickoff stream response plus
`execution_owner: "server"`. Adopt that stream; do not POST chat/start.
Server-mode `args:"resume"` resets the native budget and starts a continuation
immediately (409 if a stream is already running). Legacy resume still only
changes goal state. Status reports current `execution_owner`. Pause and clear
revoke ownership regardless of whether the request repeats the opt-in flag.
They do not interrupt an already executing model turn, matching existing goal
controls. Chat Stop revokes automatic continuation and cancels the stream using
the existing cancellation path, including a racing successor.

The native GoalManager remains authoritative for completion and budgets. Both
workers emit a `goal_continue` frame with `execution_owner:"server"` for an
owned turn; **the top-level continuation_prompt and text are omitted** so old
browser queue consumers cannot accidentally submit a duplicate. Updated browser
code also explicitly ignores server-owned frames. The informational `goal`
frame still contains the judge decision. Subsequent turns use ordinary
`_start_run` admission with `goal_related:true`, the initial resolved
workspace/model/provider/backend, and normal approval/clarification machinery.
Live session subscribers receive the existing `server_turn_started` event.
No browser connection is necessary to schedule or persist the next turn.

## Boundaries

Ownership is process-local and explicit. Restart does not silently resume a
goal; the persisted goal survives and requires an explicit server-mode resume.
Profile/session identity changes fail closed rather than migrating authority.
A competing user/background turn can win normal admission; the goal pauses on
that conflict rather than retrying unexpectedly. No retry loop or timer exists.
Model work can wait for normal user approvals; this feature never autoapproves.

## Bounded verification

Run from the checkout with the installed Hermes modules available:

```sh
PYTHONDONTWRITEBYTECODE=1 HERMES_WEBUI_AGENT_DIR=/usr/local/lib/hermes-agent python tests/test_server_owned_goals.py
```

The standalone suite creates a disposable home under TMPDIR before importing
the real API, then removes it. It invokes actual goal HTTP handlers directly,
real `_start_run` and stream admission, real Session persistence, and the native
GoalManager. **The model worker and goal judge are explicitly synthetic.**
It verifies multi-turn completion without a browser, budget exhaustion,
pause/clear/resume, duplicate claims, concurrent teardown, Stop racing successor
admission, Stop before kickoff binding, legacy event compatibility, invalid
mode rejection, runner refusal, profile fail-closed behavior, kickoff rollback,
selection forwarding, and absence of approval-policy overrides.
This is not live inference, full AIAgent streaming, HTTP-auth, device, or gateway
integration evidence. No services are restarted or deployed.
