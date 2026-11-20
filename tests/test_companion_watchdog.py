# -*- coding: utf-8 -*-
"""Offline tests for ops/companion-watchdog.py.

Covers the 2026-11-20 rewrite that fixed the 11-18 restart storm / destructive rollback:

* the startup grace period can never produce `failed`;
* `failed` needs `max_failures` consecutive observations, each at least one window apart;
* the same source tree is rolled back at most once, and a rollback needs a fresh-enough
  rollback point that actually differs from the current tree;
* restarts are rate limited and the current tree is backed up before it is touched;
* the restart-request channel accepts/expired/invalid/rate-limited/running-action branches;
* settle / grace / window stay in one consistent order.

Nothing here talks to systemd, journalctl or the production life.db: every probe is injected and
`systemctl`/`journalctl`/`runuser` calls are captured by a fake subprocess. Run with

    python -m unittest tests.test_companion_watchdog -v
"""
import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'companion_watchdog', ROOT / 'ops' / 'companion-watchdog.py')
wd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wd)


def make_tree(root):
    (root / 'life').mkdir(parents=True, exist_ok=True)
    (root / 'life' / 'ok.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'life' / 'other.py').write_text('OTHER = 2\n', encoding='utf-8')
    (root / 'ops').mkdir(exist_ok=True)
    (root / 'ops' / 'tool.py').write_text('TOOL = 3\n', encoding='utf-8')
    (root / 'config.toml').write_text('secret = "must never be snapshotted"\n', encoding='utf-8')
    (root / 'toolchain').mkdir(exist_ok=True)
    (root / 'toolchain' / 'node').write_text('binary-ish\n', encoding='utf-8')


class FakeSubprocess:
    """Captures `systemctl`/`journalctl`/`runuser` calls instead of performing them."""

    def __init__(self, calls, returncode=0, stderr='', stdout=''):
        self.calls = calls
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wd-test-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'src'
        self.store = base / 'state'
        self.home = base / 'home'
        self.root.mkdir(parents=True)
        self.store.mkdir(parents=True)
        (self.home / 'data').mkdir(parents=True)
        make_tree(self.root)
        self.calls = []
        self.notices = []
        self.saved = {}
        self.now = time.time()

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wd, name, value)

    def patch(self, name, value):
        if name not in self.saved:
            self.saved[name] = getattr(wd, name)
        setattr(wd, name, value)

    def inject(self, *, active='active', substate='running', health=True, errors=(),
               turns=1, started_epoch=None, returncode=0, stderr=''):
        started = self.now - 300 if started_epoch is None else started_epoch
        self.patch('unit_state', lambda unit: {
            'ActiveState': active, 'SubState': substate,
            'ExecMainStartTimestamp': f'start-{int(started)}',
            'ExecMainStartTimestampEpoch': float(started)})
        self.patch('service_started_epoch', lambda unit: float(started))
        self.patch('http_ok', lambda url, timeout=5, token_file=None: health)
        self.patch('journal_errors', lambda unit, since: list(errors))
        self.patch('cognition_turns', lambda home, since: turns)
        self.patch('write_notice', lambda home, text, *, external_id, payload: self.notices.append(
            dict(text=text, external_id=external_id, payload=payload)) or external_id)
        self.patch('subprocess', FakeSubprocess(self.calls, returncode=returncode, stderr=stderr))

    def state(self, **overrides):
        state = dict(wd.DEFAULTS, **copy.deepcopy(wd.STATE_DEFAULTS))
        state.update(dict(source_root=str(self.root), state_root=str(self.store),
                          home=str(self.home), unit='companion-watchdog-test.service',
                          health_url='http://127.0.0.1:9/alive'))
        state.update(overrides)
        return wd.normalize_timing(state)

    def age_snapshot(self, snapshot_id, seconds):
        path = self.store / 'snapshots' / f'{snapshot_id}.json'
        record = json.loads(path.read_text('utf-8'))
        record['created'] = record['created'] - seconds
        path.write_text(json.dumps(record), 'utf-8')

    def restart_calls(self):
        return [call for call in self.calls if call[:2] == ['systemctl', 'restart']]

    def break_tree(self):
        (self.root / 'life' / 'ok.py').write_text('VALUE = 1\nraise SystemExit("broken")\n',
                                                  encoding='utf-8')
        (self.root / 'life' / 'extra.py').write_text('EXTRA = True\n', encoding='utf-8')


# --------------------------------------------------------------------------- snapshots


class SnapshotTests(BaseCase):
    def test_snapshot_ignores_secrets_and_vendored_trees(self):
        record = wd.snapshot(self.root, self.store, 'first')
        files = set(record['manifest'])
        self.assertIn('life/ok.py', files)
        self.assertNotIn('config.toml', files)
        self.assertFalse(any(name.startswith('toolchain/') for name in files))

    def test_restore_reverts_edits_and_removes_added_files(self):
        first = wd.snapshot(self.root, self.store, 'good')
        self.break_tree()
        result = wd.restore(self.root, self.store, first['id'])
        self.assertEqual((self.root / 'life' / 'ok.py').read_text(encoding='utf-8'), 'VALUE = 1\n')
        self.assertFalse((self.root / 'life' / 'extra.py').exists())
        self.assertIn('life/ok.py', result['restored'])
        self.assertIn('life/extra.py', result['removed'])
        self.assertEqual(result['skipped'], [])

    def test_snapshot_id_is_content_addressed(self):
        first = wd.snapshot(self.root, self.store, 'a')
        second = wd.snapshot(self.root, self.store, 'b')
        self.assertEqual(first['id'], second['id'])
        (self.root / 'life' / 'ok.py').write_text('VALUE = 3\n', encoding='utf-8')
        third = wd.snapshot(self.root, self.store, 'c')
        self.assertNotEqual(first['id'], third['id'])

    def test_reconcile_keeps_the_rollback_point_and_the_latest_backup(self):
        good = wd.snapshot(self.root, self.store, 'good')
        self.break_tree()
        backup = wd.snapshot(self.root, self.store, 'pre-rollback-test')
        state = self.state(known_good=good['id'], current=good['id'],
                           last_rollback=dict(backup=backup['id']))
        wd.reconcile(state, self.store)
        names = {path.stem for path in (self.store / 'snapshots').glob('*.json')}
        self.assertIn(good['id'], names)
        self.assertIn(backup['id'], names)


# --------------------------------------------------------------------------- verdicts


class VerdictTests(BaseCase):
    def test_bad_alive_inside_the_startup_grace_is_never_failed(self):
        self.inject(health=False, started_epoch=self.now - 30)   # grace default = 120s
        state = self.state()
        for offset in (0, 30, 60, 89):      # age 30s..119s: still inside the 120s grace
            verdict = wd.evaluate(state, now=self.now + offset)
            self.assertEqual(verdict['verdict'], 'pending', verdict)
            self.assertIn('grace', verdict['reason'])
        self.assertEqual(state['failure_streak'], [])

    def test_failed_needs_max_failures_counted_one_window_apart(self):
        self.inject(health=False, started_epoch=self.now - 300)
        state = self.state(window_seconds=75, max_failures=3, settle_seconds=10)
        first = wd.evaluate(state, now=self.now)
        self.assertEqual(first['verdict'], 'pending')
        self.assertEqual(state['failure_streak'], [self.now])
        # a second probe inside the same window must not count
        again = wd.evaluate(state, now=self.now + 10)
        self.assertEqual(again['verdict'], 'pending')
        self.assertEqual(len(state['failure_streak']), 1, 'a repeat inside the window is not counted')
        second = wd.evaluate(state, now=self.now + 80)
        self.assertEqual(second['verdict'], 'pending')
        self.assertEqual(len(state['failure_streak']), 2)
        third = wd.evaluate(state, now=self.now + 160)
        self.assertEqual(third['verdict'], 'failed')
        self.assertIn('/alive not 200', third['reason'])

    def test_a_healthy_observation_clears_the_streak(self):
        self.inject(health=False, started_epoch=self.now - 300)
        state = self.state(window_seconds=75, max_failures=3)
        wd.evaluate(state, now=self.now)
        wd.evaluate(state, now=self.now + 80)
        self.assertEqual(state['failures'], 2)
        self.inject(health=True, started_epoch=self.now - 300)
        self.assertEqual(wd.evaluate(state, now=self.now + 90)['verdict'], 'healthy')
        self.assertEqual(state['failures'], 0)

    def test_healthy_requires_a_cognition_turn(self):
        self.inject(turns=0, started_epoch=self.now - 300)
        verdict = wd.evaluate(self.state(), now=self.now)
        self.assertEqual(verdict['verdict'], 'pending')
        self.assertIn('no cognition turn', verdict['reason'])

    def test_unavailable_cognition_probe_is_pending(self):
        self.inject(turns=None, started_epoch=self.now - 300)
        verdict = wd.evaluate(self.state(), now=self.now)
        self.assertEqual(verdict['verdict'], 'pending')
        self.assertIn('unavailable', verdict['reason'])

    def test_healthy_when_active_alive_and_thinking(self):
        self.inject(turns=4, started_epoch=self.now - 300)
        verdict = wd.evaluate(self.state(), now=self.now)
        self.assertEqual(verdict['verdict'], 'healthy')

    def test_activating_unit_inside_grace_is_pending(self):
        self.inject(active='activating', substate='start', health=False,
                    started_epoch=self.now - 20)
        self.assertEqual(wd.evaluate(self.state(), now=self.now)['verdict'], 'pending')

    def test_crashed_unit_counts_even_inside_the_grace_period(self):
        """A dead process is not a slow start: systemd already knows the start failed."""
        self.inject(active='failed', substate='failed', health=False,
                    started_epoch=self.now - 5)
        state = self.state(window_seconds=75, max_failures=2)
        first = wd.evaluate(state, now=self.now)
        self.assertEqual(first['verdict'], 'pending')
        self.assertEqual(state['failures'], 1)
        second = wd.evaluate(state, now=self.now + 80)
        self.assertEqual(second['verdict'], 'failed')

    def test_journal_traceback_is_degraded_not_a_rollback_trigger(self):
        self.inject(errors=['Traceback (most recent call last):'], started_epoch=self.now - 300)
        state = self.state()
        verdict = wd.evaluate(state, now=self.now)
        self.assertEqual(verdict['verdict'], 'degraded')
        self.assertIn('journal', verdict['reason'])
        self.assertEqual(state['failure_streak'], [])

    def test_journal_probe_only_looks_at_the_recent_window(self):
        """A traceback from an hour ago must not block health forever."""
        seen = {}

        def fake_journal(unit, since):
            seen['since'] = since
            return []

        self.inject(turns=1, started_epoch=self.now - 300)
        self.patch('journal_errors', fake_journal)
        state = self.state(window_seconds=75)
        wd.evaluate(state, now=self.now)
        wd.evaluate(state, now=self.now + 500)
        self.assertAlmostEqual(seen['since'], self.now + 425, delta=1)

    def test_window_anchors_on_systemds_start_timestamp(self):
        self.inject(health=False, started_epoch=self.now - 500)
        state = self.state(window_seconds=75, max_failures=3)
        wd.evaluate(state, now=self.now)
        self.assertEqual(state['unit_started_epoch'], self.now - 500)
        self.assertEqual(state['observed_since'], self.now)

    def test_start_epoch_falls_back_to_the_probe_when_unit_state_lacks_it(self):
        self.inject(health=True, turns=1)
        self.patch('unit_state', lambda unit: {
            'ActiveState': 'active', 'SubState': 'running',
            'ExecMainStartTimestamp': 'start-without-epoch'})
        self.patch('service_started_epoch', lambda unit: self.now - 400)
        state = self.state()
        wd.evaluate(state, now=self.now)
        self.assertEqual(state['unit_started_epoch'], self.now - 400)


# --------------------------------------------------------------------------- timing


class TimingTests(unittest.TestCase):
    def test_defaults_are_ordered(self):
        state = dict(wd.DEFAULTS)
        wd.normalize_timing(state)
        self.assertLessEqual(state['settle_seconds'], state['startup_grace_seconds'])
        self.assertLessEqual(state['settle_seconds'], state['window_seconds'])

    def test_grace_is_never_shorter_than_settle(self):
        state = dict(wd.DEFAULTS, settle_seconds=30, startup_grace_seconds=5,
                     window_seconds=20)
        wd.normalize_timing(state)
        self.assertEqual(state['startup_grace_seconds'], 30)
        self.assertGreaterEqual(state['window_seconds'], state['settle_seconds'])

    def test_zero_values_survive_for_isolated_rehearsal(self):
        state = dict(wd.DEFAULTS, settle_seconds=0, startup_grace_seconds=0, window_seconds=0)
        wd.normalize_timing(state)
        self.assertEqual((state['settle_seconds'], state['startup_grace_seconds'],
                          state['window_seconds']), (0.0, 0.0, 0.0))


# --------------------------------------------------------------------------- rollback path


class RollbackTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.inject(active='failed', substate='failed', health=False, turns=0,
                    started_epoch=self.now - 300)
        self.good = wd.snapshot(self.root, self.store, 'good')

    def broken_state(self, **overrides):
        self.break_tree()
        state = self.state(known_good=self.good['id'], known_good_start='old-start',
                           unit_started_epoch=self.now - 300, unit_started='start-old',
                           window_seconds=75, max_failures=3, restarts=[])
        state.update(overrides)
        return state

    def test_confirmed_failure_rolls_back_once_backs_up_and_notifies(self):
        state = self.broken_state()
        # the verified snapshot is older than this start, as production requires
        self.age_snapshot(self.good['id'], 900)
        outcomes = [wd.cycle(state, self.store, now=self.now + offset)
                    for offset in (0, 80, 160)]
        self.assertEqual([out['verdict'] for out in outcomes], ['pending', 'pending', 'rolled-back'])
        self.assertEqual((self.root / 'life' / 'ok.py').read_text(encoding='utf-8'), 'VALUE = 1\n')
        self.assertFalse((self.root / 'life' / 'extra.py').exists())
        restarts = self.restart_calls()
        self.assertEqual(len(restarts), 1, 'the rollback restarts the unit exactly once')
        self.assertTrue(any(call[:2] == ['systemctl', 'reset-failed'] for call in self.calls))

        # the broken tree was backed up first, and the backup can undo the rollback
        backup_id = state['last_rollback']['backup']
        self.assertNotEqual(backup_id, self.good['id'])
        self.assertTrue((self.store / 'snapshots' / f'{backup_id}.json').is_file())
        wd.restore(self.root, self.store, backup_id)
        self.assertIn('SystemExit', (self.root / 'life' / 'ok.py').read_text(encoding='utf-8'))
        self.assertTrue((self.root / 'life' / 'extra.py').exists())
        self.assertEqual(state['restarts'], [self.now + 160])
        self.assertEqual(len(self.notices), 1)
        self.assertTrue(self.notices[0]['external_id'].startswith('watchdog:rollback:'))

    def test_the_same_source_tree_is_rolled_back_only_once(self):
        state = self.broken_state()
        self.age_snapshot(self.good['id'], 900)
        for offset in (0, 80, 160):
            wd.cycle(state, self.store, now=self.now + offset)
        self.assertEqual(len(self.restart_calls()), 1)

        # she applies the exact same broken source again after the rollback
        self.break_tree()
        state['unit_started'] = ''
        outcomes = [wd.cycle(state, self.store, now=self.now + 200 + offset)
                    for offset in (0, 80, 160)]
        self.assertEqual(outcomes[-1]['verdict'], 'stalled')
        self.assertIn('already rolled back', outcomes[-1]['reason'])
        self.assertEqual(len(self.restart_calls()), 1, 'no second restart for the same source')
        self.assertTrue(state['automation_stopped'])
        self.assertTrue(any(note['external_id'].startswith('watchdog:stalled:')
                            for note in self.notices))

    def test_restart_budget_stops_a_storm(self):
        state = self.broken_state(restarts=[self.now - 10, self.now - 20],
                                  max_restarts_per_window=2)
        outcome = wd.rollback(state, self.store, now=self.now,
                              verdict=dict(verdict='failed', reason='/alive not 200'))
        self.assertEqual(outcome['verdict'], 'stalled')
        self.assertIn('restart budget', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])
        # the tree was not touched: a stalled watchdog must not be destructive
        self.assertIn('SystemExit', (self.root / 'life' / 'ok.py').read_text(encoding='utf-8'))

    def test_rollback_point_must_predate_this_start(self):
        state = self.broken_state(unit_started_epoch=self.now - 300)
        outcome = wd.rollback(state, self.store, now=self.now,
                              verdict=dict(verdict='failed', reason='x'))
        # self.good was created ~now, so it is not older than the start
        self.assertEqual(outcome['verdict'], 'stalled')
        self.assertIn('not older than', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_identical_tree_is_not_rolled_back(self):
        good = wd.snapshot(self.root, self.store, 'same')
        state = self.state(known_good=good['id'], unit_started_epoch=self.now - 300,
                           unit_started='start-old', restarts=[])
        outcome = wd.rollback(state, self.store, now=self.now,
                              verdict=dict(verdict='failed', reason='x'))
        self.assertEqual(outcome['verdict'], 'stalled')
        self.assertIn('identical', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_no_running_actions_are_interrupted(self):
        self.make_life_db(running=1)
        state = self.broken_state()
        self.age_snapshot(self.good['id'], 900)
        outcome = wd.rollback(state, self.store, now=self.now,
                              verdict=dict(verdict='failed', reason='x'))
        self.assertEqual(outcome['verdict'], 'deferred')
        self.assertIn('running', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])
        self.assertIn('SystemExit', (self.root / 'life' / 'ok.py').read_text(encoding='utf-8'))

    def test_no_rollback_point_stalls_instead_of_guessing(self):
        self.break_tree()
        state = self.state(known_good=None, unit_started_epoch=self.now - 300,
                           unit_started='start-old', restarts=[])
        outcome = wd.rollback(state, self.store, now=self.now,
                              verdict=dict(verdict='failed', reason='x'))
        self.assertEqual(outcome['verdict'], 'stalled')
        self.assertIn('no verified snapshot', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_dry_run_reports_the_plan_without_touching_anything(self):
        state = self.broken_state()
        self.age_snapshot(self.good['id'], 900)
        plan = wd.describe_rollback(state, self.store, now=self.now)
        self.assertEqual(plan['action'], 'rollback')
        self.assertTrue(plan['fresh'])
        self.assertFalse(plan['already_rolled_back'])
        self.assertEqual(plan['would_restore'], 1)
        self.assertEqual(plan['would_remove'], 1)
        self.assertEqual(self.restart_calls(), [])
        self.assertIn('SystemExit', (self.root / 'life' / 'ok.py').read_text(encoding='utf-8'))

    def make_life_db(self, running=0):
        db = sqlite3.connect(self.home / 'data' / 'life.db')
        db.executescript('CREATE TABLE companion_actions (id TEXT PRIMARY KEY, status TEXT);')
        for index in range(running):
            db.execute("INSERT INTO companion_actions (id, status) VALUES (?, 'running')",
                       (f'action-{index}',))
        db.commit()
        db.close()


# --------------------------------------------------------------------------- restart requests


class RestartRequestTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.inject(health=True, turns=3, started_epoch=self.now - 300)
        self.state_ = self.state()

    def write_request(self, **overrides):
        payload = dict(reason='drill: restart-request channel', requested_at=self.now - 5,
                       requested_by='xiaoyu')
        payload.update(overrides)
        path = self.store / 'restart.request'
        path.write_text(json.dumps(payload), 'utf-8')
        return path

    def test_valid_request_restarts_once_and_is_finalized_done(self):
        path = self.write_request()
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'accepted')
        restarts = self.restart_calls()
        self.assertEqual(restarts, [['systemctl', 'restart', self.state_['unit']]])
        self.assertFalse(path.exists(), 'the live request file must not survive')
        done = self.store / 'restart.request.done'
        self.assertTrue(done.is_file())
        final = json.loads(done.read_text('utf-8'))
        self.assertEqual(final['status'], 'accepted')
        self.assertIn('handled_at', final)
        self.assertEqual(final['original']['reason'], 'drill: restart-request channel')
        self.assertEqual(self.state_['last_restart_accepted'], self.now)
        self.assertEqual(self.state_['restarts'], [self.now])
        self.assertEqual(self.state_['unit_started'], '', 'next cycle must re-anchor on the new start')
        self.assertEqual(len(self.notices), 1)
        self.assertIn('重启', self.notices[0]['text'])
        history = self.store / 'restart.request.history.jsonl'
        self.assertTrue(history.is_file())

    def test_expired_request_is_rejected_without_restarting(self):
        path = self.write_request(requested_at=self.now - 3600)
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('expired', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])
        self.assertFalse(path.exists())
        final = json.loads((self.store / 'restart.request.rejected').read_text('utf-8'))
        self.assertEqual(final['status'], 'rejected')
        self.assertIn('expired', final['detail'])

    def test_invalid_json_is_rejected(self):
        (self.store / 'restart.request').write_text('not json at all', 'utf-8')
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('invalid JSON', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])
        self.assertTrue((self.store / 'restart.request.rejected').is_file())

    def test_unknown_requester_is_rejected(self):
        self.write_request(requested_by='intruder')
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('requested_by', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_future_requested_at_is_rejected(self):
        self.write_request(requested_at=self.now + 600)
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('future', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_missing_reason_is_rejected(self):
        self.write_request(reason='')
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('reason', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_rate_limit_rejects_a_second_request(self):
        self.state_['last_restart_accepted'] = self.now - 60
        self.write_request()
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('rate limited', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_exhausted_restart_budget_rejects_the_request(self):
        self.state_['restarts'] = [self.now - 10, self.now - 20]
        self.state_['max_restarts_per_window'] = 2
        self.write_request()
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('budget exhausted', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])

    def test_running_actions_block_the_restart(self):
        db = sqlite3.connect(self.home / 'data' / 'life.db')
        db.executescript('CREATE TABLE companion_actions (id TEXT PRIMARY KEY, status TEXT);')
        db.execute("INSERT INTO companion_actions (id, status) VALUES ('a1', 'running')")
        db.commit()
        db.close()
        self.write_request()
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'rejected')
        self.assertIn('running', outcome['reason'])
        self.assertEqual(self.restart_calls(), [])
        self.assertTrue((self.store / 'restart.request.rejected').is_file())

    def test_failed_systemctl_is_reported_as_rejected(self):
        self.patch('subprocess', FakeSubprocess(self.calls, returncode=5,
                                                stderr='Unit not found.'))
        self.write_request()
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('Unit not found', outcome['detail'])
        self.assertEqual(self.state_['restarts'], [])
        self.assertTrue((self.store / 'restart.request.rejected').is_file())

    def test_dry_run_leaves_the_request_for_the_real_run(self):
        path = self.write_request()
        outcome = wd.handle_restart_request(self.state_, self.store, now=self.now, dry_run=True)
        self.assertEqual(outcome['status'], 'would-restart')
        self.assertTrue(path.exists())
        self.assertEqual(self.restart_calls(), [])

    def test_cycle_short_circuits_on_an_accepted_request(self):
        self.write_request()
        outcome = wd.cycle(self.state_, self.store, now=self.now)
        self.assertEqual(outcome['verdict'], 'restart-requested')
        self.assertEqual(len(self.restart_calls()), 1)
        self.assertEqual(self.state_['last_verdict'], 'restart-requested')

    def test_rejected_request_does_not_stop_the_health_cycle(self):
        self.write_request(requested_at=self.now - 3600)
        outcome = wd.cycle(self.state_, self.store, now=self.now)
        self.assertIn(outcome['verdict'], ('healthy', 'pending', 'degraded'))
        self.assertEqual(self.restart_calls(), [])


# --------------------------------------------------------------------------- probes + notice


class ProbeParsingTests(BaseCase):
    def test_unit_state_parses_systemds_unix_timestamp(self):
        self.patch('subprocess', FakeSubprocess(
            self.calls, stdout=('ActiveState=active\nSubState=running\n'
                                'ExecMainStartTimestamp=@1795141745\nNRestarts=0\nMainPID=42\n'
                                'LoadState=loaded\n')))
        info = wd.unit_state('companion.service')
        self.assertEqual(info['ActiveState'], 'active')
        self.assertEqual(info['ExecMainStartTimestampEpoch'], 1795141745.0)

    def test_service_started_epoch_reads_the_unix_stamp(self):
        self.patch('subprocess', FakeSubprocess(self.calls, stdout='ExecMainStartTimestamp=@1795141745\n'))
        self.assertEqual(wd.service_started_epoch('companion.service'), 1795141745.0)

    def test_missing_timestamp_is_reported_as_unknown_not_guessed(self):
        self.patch('subprocess', FakeSubprocess(self.calls, stdout='ExecMainStartTimestamp=\n'))
        self.assertEqual(wd.service_started_epoch('companion.service'), 0.0)


class NoticeTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.db = self.home / 'data' / 'life.db'
        db = sqlite3.connect(self.db)
        db.executescript('''
        CREATE TABLE events (id TEXT PRIMARY KEY, agent_id TEXT, conversation_id TEXT,
          participant_id TEXT DEFAULT '', audience_policy_id TEXT DEFAULT '', kind TEXT,
          source_kind TEXT, external_id TEXT, occurred_at REAL, received_at REAL,
          ingest_seq INTEGER, actor_id TEXT DEFAULT '', payload TEXT DEFAULT '{}',
          source_event_ids TEXT DEFAULT '[]', schema_version INTEGER DEFAULT 1, redacted_at REAL);
        ''')
        db.commit()
        db.close()
        self.patch('subprocess', _InlineRunuser())

    def test_notice_lands_in_the_principal_conversation(self):
        event_id = wd.write_notice(self.home, '回滚了', external_id='watchdog:rollback:start-x',
                                   payload=dict(watchdog='rollback'))
        self.assertTrue(event_id)
        db = sqlite3.connect(self.db)
        row = db.execute('SELECT conversation_id, source_kind, payload FROM events '
                         'WHERE external_id=?', ('watchdog:rollback:start-x',)).fetchone()
        db.close()
        self.assertEqual(row[0], 'qq:private:763282103')
        self.assertEqual(row[1], 'self_observation')
        self.assertIn('回滚了', json.loads(row[2])['text'])

    def test_notice_is_idempotent_for_the_same_external_id(self):
        for _ in range(2):
            wd.write_notice(self.home, 'same text',
                            external_id='watchdog:rollback:start-x',
                            payload=dict(watchdog='rollback'))
        db = sqlite3.connect(self.db)
        count = db.execute("SELECT COUNT(*) FROM events WHERE external_id='watchdog:rollback:start-x'").fetchone()[0]
        db.close()
        self.assertEqual(count, 1)

    def test_missing_database_is_skipped_instead_of_raising(self):
        missing = Path(self.tmp.name) / 'nowhere'
        self.assertIsNone(wd.write_notice(missing, 'x', external_id='watchdog:x',
                                          payload={}))


class _InlineRunuser:
    """Run the notice SQL locally instead of through `runuser -u companion`."""

    @staticmethod
    def run(args, **kwargs):
        import subprocess as real
        script_args = args[args.index('--') + 1:]
        return real.run(script_args, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
