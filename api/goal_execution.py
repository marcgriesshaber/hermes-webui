"""Opt-in, process-local goal turn ownership (never bypasses normal run admission).

The judge still owns completion and budgets. A continuation is claimed only after
its worker has torn down. No polling, retries, timers or model calls live here.
Restart deliberately drops ownership; resuming requires a fresh explicit opt-in.
"""
from __future__ import annotations

import logging
import threading
import weakref
from functools import wraps

log = logging.getLogger(__name__)
_registry_lock = threading.RLock()
_gates = weakref.WeakValueDictionary()
_state_gates = weakref.WeakValueDictionary()
_leases = {}
_server_streams = set()


def _lock_for(registry, sid):
    with _registry_lock:
        lock = registry.get(sid)
        if lock is None:
            lock = threading.RLock()
            registry[sid] = lock
        return lock


def command_gate(sid):
    return _lock_for(_gates, str(sid))


def serialized_goal_state(func):
    """Serialize GoalManager read/modify/write, not session locks or model work."""
    @wraps(func)
    def call(session_id, *args, **kwargs):
        with _lock_for(_state_gates, str(session_id)):
            return func(session_id, *args, **kwargs)
    return call


def capabilities():
    from api.runtime_adapter import runtime_adapter_runner_enabled
    return {"server_owned_goal_continuation": not runtime_adapter_runner_enabled(),
            "goal_execution_modes": ["client", "server"] if not runtime_adapter_runner_enabled() else ["client"],
            "restart_recovery": False}


def begin(s, profile_home, run_options):
    from api.config import PENDING_GOAL_CONTINUATION
    PENDING_GOAL_CONTINUATION.discard(s.session_id)
    lease = {"profile": getattr(s, "profile", None), "home": profile_home,
             "options": dict(run_options), "stream": None, "streams": set(),
             "pending": None, "gate": command_gate(s.session_id)}
    with _registry_lock:
        _leases[s.session_id] = lease
    return lease


def revoke(sid):
    with _registry_lock:
        return _leases.pop(sid, None)


def bind(sid, lease, stream_id):
    with _registry_lock:
        if _leases.get(sid) is lease:
            lease["stream"] = stream_id
            lease["streams"].add(stream_id)
            _server_streams.add(stream_id)


def owner(sid):
    with _registry_lock:
        return "server" if sid in _leases else "client"


def continuation(sid, stream_id, prompt):
    """Return event ownership; server frames intentionally contain no prompt.

    Suppressing the legacy prompt also protects old browser clients, not only
    browsers updated to check execution_owner.
    """
    with _registry_lock:
        lease = _leases.get(sid)
        if lease is None:
            if stream_id in _server_streams:
                return {"execution_owner": "server"}
            return {"execution_owner": "client", "continuation_prompt": prompt, "text": prompt}
        if lease["stream"] in (None, stream_id):
            lease["stream"] = stream_id
            lease["streams"].add(stream_id)
            # Duplicate events replace the same pending claim, never add work.
            _server_streams.add(stream_id)
            lease["pending"] = (stream_id, prompt)
        return {"execution_owner": "server"}


def cancel_owned_stream(stream_id):
    """Fence Stop against launch, including a successor of the stopped stream."""
    with _registry_lock:
        match = next(((sid, v) for sid, v in _leases.items()
                      if stream_id in v["streams"]), None)
    if match is None:
        # Admission registers ownership before starting its worker; Stop can
        # arrive before the HTTP command has bound its returned stream id.
        from api.config import stream_owner_session_id
        sid = stream_owner_session_id(stream_id)
        with _registry_lock:
            lease = _leases.get(sid)
            if lease is not None:
                match = (sid, lease)
    if match is None:
        return
    sid, lease = match
    with lease["gate"]:
        with _registry_lock:
            if _leases.get(sid) is not lease:
                return
            _leases.pop(sid, None)
            successor = lease["stream"]
        if successor and successor != stream_id:
            from api.streaming import cancel_stream
            cancel_stream(successor)


def turn_finished(sid, stream_id):
    """Called after stream + active-run cleanup. Claim once, start normally.

    An intervening human/background turn wins normal run admission: on conflict
    we pause rather than retrying later and surprising the user.
    """
    from api import routes
    from api.goals import goal_command_payload, has_active_goal
    with _registry_lock:
        _server_streams.discard(stream_id)
        lease = _leases.get(sid)
    if lease is None:
        return
    with lease["gate"]:
        with _registry_lock:
            if _leases.get(sid) is not lease or lease["stream"] != stream_id:
                return
            pending = lease["pending"]
            lease["pending"] = None
            if pending is None or pending[0] != stream_id:
                _leases.pop(sid, None)
                return
            lease["stream"] = None
        try:
            s = routes.get_session(sid)
            if (s.session_id != sid or getattr(s, "profile", None) != lease["profile"]
                    or not has_active_goal(sid, profile_home=lease["home"])):
                revoke(sid)
                return
            response = routes._start_run(
                s, msg=pending[1], attachments=[], source="goal_continuation",
                route="/api/goal", goal_related=True, **lease["options"])
            status = int(response.get("_status", 200))
            successor = response.get("stream_id")
            if status >= 400 or not successor:
                raise RuntimeError("goal continuation admission failed")
            bind(sid, lease, successor)
        except Exception:
            revoke(sid)
            goal_command_payload(sid, "pause", profile_home=lease["home"])
            log.warning("Server goal continuation stopped for %s", sid, exc_info=True)
            return
        # A live-view subscriber failure must not revoke an already started run.
        try:
            from api.background_process import get_session_channel
            ch = get_session_channel(sid)
            if ch is not None:
                ch.emit("server_turn_started", {"session_id": sid, "stream_id": successor,
                        "pending_started_at": response.get("pending_started_at"),
                        "source": "goal_continuation"})
        except Exception:
            log.debug("Goal live-view fan-out failed for %s", sid, exc_info=True)
