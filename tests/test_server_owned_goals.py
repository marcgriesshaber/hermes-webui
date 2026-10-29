"""Bounded isolated handler/GoalManager tests. SYNTHETIC MODEL WORKER, no inference.

Standalone (no pytest/server fixture required):
  python tests/test_server_owned_goals.py
Only run this file in a fresh process: it sets a disposable Hermes home BEFORE
importing the real API. All workers have a five-second bound; no production chat.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Handler:
    headers = {}
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
    def send_response(self, status): self.status = status
    def send_header(self, *args): pass
    def end_headers(self): pass


@unittest.skipUnless(__name__ == '__main__', 'Run standalone: fresh-process disposable Hermes home required')
class ServerOwnedGoals(unittest.TestCase):
    def setUp(self):
        from api import routes, config, goals, goal_execution
        from api.models import new_session
        self.routes, self.config, self.goals, self.execution = routes, config, goals, goal_execution
        self.workers = []
        self.errors = []
        self.gate = threading.Event()
        self.gate.set()
        self.complete_at = 3
        self.entered = threading.Event()
        self.patchers = [
            patch.object(routes, '_run_agent_streaming', self.synthetic_worker),
            patch.object(routes, '_resolve_compatible_session_model_state',
                         lambda model, provider, **kw: (model, provider, False)),
            patch.object(routes, '_read_profile_model_config', return_value=('openai', 'synthetic-only', {})),
            patch.object(goals, 'judge_goal', side_effect=lambda goal, response:
                         ('done', 'synthetic judge completion') if 'COMPLETE' in response
                         else ('continue', 'synthetic judge continue')),
            patch('hermes_cli.goals.judge_goal', side_effect=lambda goal, response, **kw:
                  ('done', 'synthetic judge completion', False, None, False) if 'COMPLETE' in response
                  else ('continue', 'synthetic judge continue', False, None, False)),
            patch.object(goals, '_default_max_turns', return_value=4),
            patch('api.runtime_adapter.runtime_adapter_enabled', return_value=False),
            patch('api.runtime_adapter.runtime_adapter_runner_enabled', return_value=False),
            patch.object(routes, 'webui_gateway_chat_enabled', return_value=False),
        ]
        for p in self.patchers: p.start()
        self.s = new_session(workspace=os.environ['HERMES_HOME'], model='synthetic-only',
                             model_provider='openai', profile='default')
        self.sid = self.s.session_id
        from api.profiles import get_hermes_home_for_profile
        self.home = get_hermes_home_for_profile('default')

    def tearDown(self):
        self.gate.set()
        self.execution.revoke(self.sid)
        deadline = time.monotonic() + 5
        for worker in list(self.workers): worker.join(max(0, deadline - time.monotonic()))
        for p in reversed(self.patchers): p.stop()
        self.assertFalse(any(t.is_alive() for t in self.workers), 'bounded worker did not exit')
        self.assertEqual(self.errors, [])

    def command(self, args, **extra):
        h = Handler()
        self.routes._handle_goal_command(h, {'session_id': self.sid, 'args': args,
                                            'profile': 'default', **extra})
        return h.status, json.loads(h.wfile.getvalue())

    def state(self):
        return self.goals.goal_command_payload(self.sid, 'status', profile_home=self.home)['goal']

    def wait_idle(self, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.state()['status'] == expected and self.execution.owner(self.sid) == 'client':
                return
            time.sleep(.01)
        self.fail(f'goal did not settle: {self.state()} / {self.errors}')

    def synthetic_worker(self, sid, msg, model, workspace, stream_id, attachments, **kwargs):
        """Replace inference ONLY; real dispatcher/admission/persistence/judge bridge.

        This bounded worker explicitly invokes the production goal handoff helpers;
        it is not a claim to exercise the full AIAgent streaming implementation.
        """
        from api.models import get_session
        from api.config import _get_session_agent_lock, unregister_active_run, unregister_stream_owner
        self.workers.append(threading.current_thread())
        try:
            assert kwargs.get('goal_related') is True
            assert model == 'synthetic-only'
            self.entered.set()
            assert self.gate.wait(5), 'synthetic worker timed out'
            with _get_session_agent_lock(sid):
                s = get_session(sid)
                if s.active_stream_id != stream_id:
                    return
                used = (self.state() or {}).get('turns_used', 0) + 1
                text = 'SYNTHETIC MODEL ' + ('COMPLETE' if used >= self.complete_at else 'progress')
                s.messages += [{'role': 'user', 'content': msg}, {'role': 'assistant', 'content': text}]
                s.active_stream_id = None
                s.pending_user_message = None
                s.pending_started_at = None
                s.save()
            decision = self.goals.evaluate_goal_after_turn(sid, text, profile_home=self.home)
            if decision.get('should_continue'):
                frame = self.execution.continuation(sid, stream_id, decision['continuation_prompt'])
                assert frame == {'execution_owner': 'server'}
            with self.config.STREAMS_LOCK:
                self.config.STREAMS.pop(stream_id, None)
                self.config.CANCEL_FLAGS.pop(stream_id, None)
                self.config.STREAM_GOAL_RELATED.pop(stream_id, None)
                unregister_active_run(stream_id)
                unregister_stream_owner(stream_id)
            self.execution.turn_finished(sid, stream_id)
        except Exception as exc:
            self.errors.append(repr(exc))

    def test_autonomous_completion_no_browser(self):
        status, payload = self.command('bounded synthetic task', goal_execution='server')
        self.assertEqual(status, 200)
        self.assertEqual(payload['execution_owner'], 'server')
        self.wait_idle('done')
        self.assertEqual(self.state()['turns_used'], 3)
        from api.models import get_session
        self.assertEqual(len(get_session(self.sid).messages), 6)
        self.assertNotIn(self.sid, self.config.PENDING_GOAL_CONTINUATION)

    def test_budget_is_native_and_bounded(self):
        self.complete_at = 100
        self.command('synthetic never complete', goal_execution='server')
        self.wait_idle('paused')
        self.assertEqual(self.state()['turns_used'], 4)
        self.assertIn('budget', self.state()['paused_reason'])

    def test_pause_blocks_queued_and_resume_reopts_in(self):
        self.gate.clear()
        self.command('synthetic pause', goal_execution='server')
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.command('pause')[0], 200)
        self.gate.set()
        self.wait_idle('paused')
        for w in list(self.workers): w.join(2)
        self.assertEqual(self.state()['turns_used'], 0)
        self.command('resume', goal_execution='server')
        self.wait_idle('done')
        self.assertEqual(self.state()['turns_used'], 3)

    def test_clear_while_running(self):
        self.gate.clear()
        self.command('synthetic clear', goal_execution='server')
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.command('clear')[0], 200)
        self.gate.set()
        for w in list(self.workers): w.join(2)
        self.assertIn((self.state() or {}).get('status'), (None, 'cleared'))
        self.assertEqual(self.execution.owner(self.sid), 'client')

    def test_duplicate_continuation_and_finish_claim_once(self):
        self.goals.goal_command_payload(self.sid, 'synthetic duplicate', profile_home=self.home)
        lease = self.execution.begin(self.s, self.home, {})
        self.execution.bind(self.sid, lease, 'old-stream')
        for _ in range(10):
            self.execution.continuation(self.sid, 'old-stream', 'next')
        calls = []
        def launch(s, **kw):
            calls.append(kw)
            return {'stream_id': 'successor', '_status': 200}
        with patch.object(self.routes, '_start_run', side_effect=launch):
            threads = [threading.Thread(target=self.execution.turn_finished,
                       args=(self.sid, 'old-stream')) for _ in range(10)]
            for t in threads: t.start()
            for t in threads: t.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['source'], 'goal_continuation')
        self.execution.revoke(self.sid)

    def test_stop_races_successor_admission(self):
        self.goals.goal_command_payload(self.sid, 'synthetic stop race', profile_home=self.home)
        lease = self.execution.begin(self.s, self.home, {})
        self.execution.bind(self.sid, lease, 'old')
        self.execution.continuation(self.sid, 'old', 'next')
        admitted, release = threading.Event(), threading.Event()
        def launch(s, **kw):
            admitted.set()
            assert release.wait(2)
            return {'stream_id': 'new', '_status': 200}
        from api import streaming
        with patch.object(self.routes, '_start_run', side_effect=launch), patch.object(streaming, 'cancel_stream') as cancel:
            t = threading.Thread(target=self.execution.turn_finished, args=(self.sid, 'old'))
            t.start()
            self.assertTrue(admitted.wait(2))
            stop = threading.Thread(target=self.execution.cancel_owned_stream, args=('old',))
            stop.start()
            release.set()
            t.join(2); stop.join(2)
            self.assertFalse(t.is_alive() or stop.is_alive())
            cancel.assert_called_once_with('new')
        self.assertEqual(self.execution.owner(self.sid), 'client')

    def test_stop_before_finish_suppresses_client_fallback(self):
        self.goals.goal_command_payload(self.sid, 'synthetic stop', profile_home=self.home)
        lease = self.execution.begin(self.s, self.home, {})
        self.execution.bind(self.sid, lease, 'old')
        self.execution.cancel_owned_stream('old')
        self.assertEqual(self.execution.continuation(self.sid, 'old', 'next'), {'execution_owner': 'server'})
        with patch.object(self.routes, '_start_run') as launch:
            self.execution.turn_finished(self.sid, 'old')
            launch.assert_not_called()

    def test_legacy_and_invalid_modes(self):
        self.assertEqual(self.execution.continuation(self.sid, 'legacy', 'next'),
                         {'execution_owner': 'client', 'continuation_prompt': 'next', 'text': 'next'})
        self.assertEqual(self.command('status', goal_execution=True)[0], 400)
        self.assertIsNone(self.state())

    def test_admission_conflict_pauses_without_retry(self):
        self.goals.goal_command_payload(self.sid, 'synthetic conflict', profile_home=self.home)
        lease = self.execution.begin(self.s, self.home, {})
        self.execution.bind(self.sid, lease, 'old')
        self.execution.continuation(self.sid, 'old', 'next')
        with patch.object(self.routes, '_start_run', return_value={'_status': 409}) as launch:
            self.execution.turn_finished(self.sid, 'old')
            self.execution.turn_finished(self.sid, 'old')
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.state()['status'], 'paused')

    def test_profile_change_fails_closed(self):
        self.goals.goal_command_payload(self.sid, 'profile boundary', profile_home=self.home)
        lease = self.execution.begin(self.s, self.home, {})
        self.execution.bind(self.sid, lease, 'old')
        self.execution.continuation(self.sid, 'old', 'next')
        self.s.profile = 'different-profile'
        with patch.object(self.routes, 'get_session', return_value=self.s), patch.object(self.routes, '_start_run') as launch:
            self.execution.turn_finished(self.sid, 'old')
            launch.assert_not_called()
        self.assertEqual(self.execution.owner(self.sid), 'client')

    def test_runner_refused_before_goal_mutation(self):
        with patch('api.runtime_adapter.runtime_adapter_runner_enabled', return_value=True):
            self.assertFalse(self.execution.capabilities()['server_owned_goal_continuation'])
            self.assertEqual(self.command('not accepted', goal_execution='server')[0], 501)
        self.assertIsNone(self.state())

    def test_kickoff_failure_restores_goal_and_revokes(self):
        self.goals.goal_command_payload(self.sid, 'prior goal', profile_home=self.home)
        self.goals.goal_command_payload(self.sid, 'pause', profile_home=self.home)
        with patch.object(self.routes, '_start_run', return_value={'_status': 409, 'error': 'synthetic conflict'}):
            status, payload = self.command('replacement', goal_execution='server')
        self.assertEqual(status, 409)
        self.assertFalse(payload['ok'])
        self.assertEqual(self.state()['goal'], 'prior goal')
        self.assertEqual(self.state()['status'], 'paused')
        self.assertEqual(self.execution.owner(self.sid), 'client')

    def test_stop_before_http_stream_bind(self):
        lease = self.execution.begin(self.s, self.home, {})
        with patch('api.config.stream_owner_session_id', return_value=self.sid):
            self.execution.cancel_owned_stream('admitting')
        self.execution.bind(self.sid, lease, 'admitting')
        self.assertEqual(self.execution.owner(self.sid), 'client')

    def test_selection_and_approval_defaults_preserved(self):
        self.goals.goal_command_payload(self.sid, 'selection', profile_home=self.home)
        options = {'workspace': str(self.home), 'model': 'chosen-model',
                   'model_provider': 'chosen-provider', 'normalized_model': False,
                   'gateway_chat_enabled': False}
        lease = self.execution.begin(self.s, self.home, options)
        self.execution.bind(self.sid, lease, 'old')
        self.execution.continuation(self.sid, 'old', 'next')
        with patch.object(self.routes, '_start_run', return_value={'stream_id': 'new'}) as launch:
            self.execution.turn_finished(self.sid, 'old')
        self.assertEqual({k: launch.call_args.kwargs[k] for k in options}, options)
        self.assertEqual(set(launch.call_args.kwargs), set(options) |
                         {'msg', 'attachments', 'source', 'route', 'goal_related'})


if __name__ == '__main__':
    scratch = Path(os.environ.get('TMPDIR', '/root/.hermes/profiles/expo-developer/cache/scratch'))
    with tempfile.TemporaryDirectory(prefix='server-owned-goals-', dir=scratch) as tmp:
        home = Path(tmp) / '.hermes'
        home.mkdir()
        (home / 'webui' / 'sessions').mkdir(parents=True)
        (home / 'config.yaml').write_text('model:\n  default: synthetic-only\n  provider: openai\ntoolsets: []\n')
        os.environ.update(HOME=tmp, HERMES_HOME=str(home), HERMES_REAL_HOME=tmp,
                          HERMES_WEBUI_STATE_DIR=str(home / 'webui'),
                          HERMES_WEBUI_ISOLATED_PROFILE='1', HERMES_DISABLE_LAZY_INSTALLS='1')
        result = unittest.main(exit=False, verbosity=2).result
    raise SystemExit(0 if result.wasSuccessful() else 1)
