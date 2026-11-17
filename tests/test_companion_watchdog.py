"""Offline tests for ops/companion-watchdog.py: snapshot, restore, verdicts, notice.

Runs on the isolation sandbox with a temporary source tree and state dir. Nothing here talks to
systemd, a live service or the production life.db; the unit/journal/health/decision probes are
injected so the decision logic itself is what gets tested.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('companion_watchdog', ROOT / 'ops' / 'companion-watchdog.py')
wd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wd)


def make_tree(root):
    (root / 'life').mkdir(parents=True, exist_ok=True)
    (root / 'life' / 'ok.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'life' / 'other.py').write_text('OTHER = 2\n', encoding='utf-8')
    (root / 'config.toml').write_text('secret = "must never be snapshotted"\n', encoding='utf-8')
    (root / 'toolchain').mkdir(exist_ok=True)
    (root / 'toolchain' / 'node').write_text('binary-ish\n', encoding='utf-8')


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='p26-wd-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'src'
        self.store = Path(self.tmp.name) / 'state'
        self.root.mkdir(parents=True)
        make_tree(self.root)

    def test_snapshot_ignores_secrets_and_vendored_trees(self):
        record = wd.snapshot(self.root, self.store, 'first')
        files = set(record['manifest'])
        self.assertIn('life/ok.py', files)
        self.assertNotIn('config.toml', files)
        self.assertFalse(any(name.startswith('toolchain/') for name in files))

    def test_restore_reverts_edits_and_removes_added_files(self):
        first = wd.snapshot(self.root, self.store, 'good')
        (self.root / 'life' / 'ok.py').write_text('VALUE = 1\nraise SystemExit("broken")\n', encoding='utf-8')
        (self.root / 'life' / 'new_file.py').write_text('ADDED = True\n', encoding='utf-8')
        result = wd.restore(self.root, self.store, first['id'])
        self.assertEqual((self.root / 'life' / 'ok.py').read_text(encoding='utf-8'), 'VALUE = 1\n')
        self.assertFalse((self.root / 'life' / 'new_file.py').exists())
        self.assertIn('life/ok.py', result['restored'])
        self.assertIn('life/new_file.py', result['removed'])
        self.assertEqual(result['skipped'], [])

    def test_restore_is_idempotent_and_leaves_untouched_files_alone(self):
        first = wd.snapshot(self.root, self.store, 'good')
        untouched = self.root / 'life' / 'other.py'
        before = untouched.stat().st_mtime_ns
        time.sleep(0.01)
        result = wd.restore(self.root, self.store, first['id'])
        self.assertEqual(result['restored'], [])
        self.assertEqual(untouched.stat().st_mtime_ns, before)

    def test_snapshot_id_is_content_addressed(self):
        first = wd.snapshot(self.root, self.store, 'a')
        second = wd.snapshot(self.root, self.store, 'b')
        self.assertEqual(first['id'], second['id'], 'same content must produce the same id')
        (self.root / 'life' / 'ok.py').write_text('VALUE = 3\n', encoding='utf-8')
        third = wd.snapshot(self.root, self.store, 'c')
        self.assertNotEqual(first['id'], third['id'])


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='p26-verdict-')
        self.addCleanup(self.tmp.cleanup)
        self.state = dict(wd.DEFAULTS, state_root=str(Path(self.tmp.name) / 'state'),
                          failures=0, current=None, known_good=None)
        self.saved = {'unit_state': wd.unit_state, 'http_ok': wd.http_ok,
                      'journal_errors': wd.journal_errors, 'cognition_turns': wd.cognition_turns, 
                      'service_started_epoch': wd.service_started_epoch}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wd, name, value)

    def inject(self, *, active='active', health=True, errors=(), turns=1):
        wd.unit_state = lambda unit: {'ActiveState': active, 'SubState': 'running',
                                      'ExecMainStartTimestamp': 'Tue 2026-11-17 22:00:00 CST'}
        wd.http_ok = lambda url, timeout=5, token_file=None: health
        wd.journal_errors = lambda unit, since: list(errors)
        wd.cognition_turns = lambda home, since: turns
        # The probes above carry the observation window, so pin it too: otherwise the verdict
        # would depend on the host having systemctl/journalctl and a real life.db.
        wd.service_started_epoch = lambda unit: time.time() - 30

    def test_healthy_requires_active_health_no_errors_and_a_turn(self):
        self.inject()
        self.state['window_anchored'] = time.time() - 30
        self.assertEqual(wd.evaluate(self.state)['verdict'], 'healthy')

    def test_missing_cognition_turn_is_not_a_failure(self):
        """A quiet role after a restart is normal; the gate is process faults, not chattiness."""
        self.inject(turns=0)
        self.state['window_anchored'] = time.time() - 600
        verdict = wd.evaluate(self.state)
        self.assertEqual(verdict['verdict'], 'healthy')
        self.assertIn('turns=0', verdict['reason'])

    def test_unknown_cognition_probe_does_not_block_health(self):
        self.inject(turns=None)
        self.state['window_anchored'] = time.time() - 600
        self.assertEqual(wd.evaluate(self.state)['verdict'], 'healthy')

    def test_health_failure_becomes_failed_after_the_window(self):
        self.inject(health=False)
        self.state['window_anchored'] = time.time() - 5
        self.assertEqual(wd.evaluate(self.state)['verdict'], 'pending')
        self.state['window_anchored'] = time.time() - 600
        self.assertEqual(wd.evaluate(self.state)['verdict'], 'failed')

    def test_inactive_unit_fails_immediately(self):
        self.inject(active='failed', health=False)
        self.assertEqual(wd.evaluate(self.state)['verdict'], 'failed')

    def test_journal_traceback_fails_even_when_health_is_200(self):
        self.inject(errors=['Traceback (most recent call last):'])
        self.state['window_anchored'] = time.time() - 30
        verdict = wd.evaluate(self.state)
        self.assertEqual(verdict['verdict'], 'failed')
        self.assertIn('journal', verdict['reason'])

    def test_window_anchors_on_the_restart_not_on_now(self):
        """A permanent /health failure must eventually fail instead of pending forever."""
        self.inject(health=False)
        wd.service_started_epoch = lambda unit: 0.0            # unknown start -> anchor at first sight
        state = dict(self.state, unit_started='', window_anchored=0.0)
        first = wd.evaluate(state)                          # first sight anchors the window
        self.assertGreater(state['window_anchored'], 0)
        self.assertEqual(first['verdict'], 'pending', 'a fresh restart gets the full window')
        state['window_anchored'] = time.time() - 600
        self.assertEqual(wd.evaluate(state)['verdict'], 'failed')


class FailurePathTests(unittest.TestCase):
    """The full rehearsal: broken source -> failed verdict -> rollback -> restart -> notice."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='p26-path-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / 'src'
        self.store = base / 'state'
        self.home = base / 'home'
        (self.home / 'data').mkdir(parents=True)
        self.root.mkdir(parents=True)
        make_tree(self.root)
        self.db = self.home / 'data' / 'life.db'
        db = sqlite3.connect(self.db)
        db.executescript('''
        CREATE TABLE events (id TEXT PRIMARY KEY, agent_id TEXT, conversation_id TEXT,
          participant_id TEXT DEFAULT '', audience_policy_id TEXT DEFAULT '', kind TEXT,
          source_kind TEXT, external_id TEXT, occurred_at REAL, received_at REAL,
          ingest_seq INTEGER, actor_id TEXT DEFAULT '', payload TEXT DEFAULT '{}',
          source_event_ids TEXT DEFAULT '[]', schema_version INTEGER DEFAULT 1, redacted_at REAL);
        CREATE TABLE decisions (id TEXT PRIMARY KEY, created_at REAL);
        ''')
        db.commit()
        db.close()
        self.restarted = []
        self.saved = {'unit_state': wd.unit_state, 'http_ok': wd.http_ok,
                      'journal_errors': wd.journal_errors, 'cognition_turns': wd.cognition_turns,
                      'subprocess': wd.subprocess}

        class FakeSubprocess:
            @staticmethod
            def run(args, **kwargs):
                self.restarted.append(list(args))
                if args and args[0] == 'runuser':
                    # write the notice through the same SQL the real path uses, as the test user
                    script_args = args[args.index('--') + 1:]
                    import subprocess as real
                    return real.run(script_args, capture_output=True, text=True)
                class Result:
                    returncode = 0
                    stdout = ''
                    stderr = ''
                return Result()
        wd.subprocess = FakeSubprocess

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wd, name, value)

    def state(self):
        return dict(wd.DEFAULTS, source_root=str(self.root), state_root=str(self.store),
                    home=str(self.home), failures=0, current=None, known_good=None,
                    window_seconds=60, settle_seconds=0)

    def test_good_release_becomes_the_rollback_point(self):
        state = self.state()
        record = wd.snapshot(self.root, self.store, 'good')
        state['current'] = record['id']
        wd.unit_state = lambda unit: {'ActiveState': 'active', 'SubState': 'running',
                                      'ExecMainStartTimestamp': 'start-1'}
        wd.http_ok = lambda url, timeout=5, token_file=None: True
        wd.journal_errors = lambda unit, since: []
        wd.cognition_turns = lambda home, since: 2
        state['window_anchored'] = time.time() - 30
        outcome = wd.cycle(state, self.store)
        self.assertEqual(outcome['verdict'], 'healthy')
        self.assertEqual(state['known_good'], record['id'])

    def test_broken_release_rolls_back_restarts_and_notifies(self):
        state = self.state()
        good = wd.snapshot(self.root, self.store, 'good')
        state['known_good'] = good['id']
        # she edits her own source and breaks it
        (self.root / 'life' / 'ok.py').write_text('raise SyntaxError("broken")\n', encoding='utf-8')
        (self.root / 'life' / 'extra.py').write_text('EXTRA = True\n', encoding='utf-8')
        state['current'] = wd.snapshot(self.root, self.store, 'bad')['id']

        wd.unit_state = lambda unit: {'ActiveState': 'failed', 'SubState': 'failed',
                                      'ExecMainStartTimestamp': 'start-bad'}
        wd.http_ok = lambda url, timeout=5, token_file=None: False
        wd.journal_errors = lambda unit, since: ['Traceback (most recent call last):']
        wd.cognition_turns = lambda home, since: 0
        outcome = wd.cycle(state, self.store)

        self.assertEqual(outcome['verdict'], 'rolled-back')
        self.assertEqual((self.root / 'life' / 'ok.py').read_text(encoding='utf-8'), 'VALUE = 1\n')
        self.assertFalse((self.root / 'life' / 'extra.py').exists())
        restarts = [call for call in self.restarted if call[:2] == ['systemctl', 'restart']]
        self.assertEqual(len(restarts), 1, 'rollback must restart the unit')
        self.assertTrue(any(call[:2] == ['systemctl', 'reset-failed'] for call in self.restarted))

        db = sqlite3.connect(self.db)
        rows = db.execute("SELECT id, conversation_id, source_kind, external_id, payload "
                          "FROM events WHERE external_id LIKE 'watchdog:%'").fetchall()
        db.close()
        self.assertEqual(len(rows), 1, 'exactly one notice for this rollback')
        event_id, conversation, source_kind, external_id, payload = rows[0]
        self.assertTrue(external_id.startswith('watchdog:rollback:'))
        self.assertEqual(conversation, 'qq:private:763282103')
        self.assertEqual(source_kind, 'self_observation')
        body = json.loads(payload)
        self.assertIn('\u56de\u6eda', body['text'])   # 閸ョ偞绮?        self.assertEqual(body['rolled_back_to'], good['id'])

    def test_repeated_failures_stall_instead_of_looping(self):
        state = self.state()
        state['known_good'] = wd.snapshot(self.root, self.store, 'good')['id']
        state['max_failures'] = 2
        state['failures'] = 2
        wd.unit_state = lambda unit: {'ActiveState': 'failed', 'SubState': 'failed',
                                      'ExecMainStartTimestamp': 'start-bad'}
        wd.http_ok = lambda url, timeout=5, token_file=None: False
        wd.journal_errors = lambda unit, since: []
        wd.cognition_turns = lambda home, since: 0
        outcome = wd.cycle(state, self.store)
        self.assertEqual(outcome['verdict'], 'stalled')
        self.assertFalse([call for call in self.restarted
                        if call[:2] in (['systemctl', 'restart'], ['systemctl', 'reset-failed'])])
        db = sqlite3.connect(self.db)
        kinds = [row[0] for row in db.execute(
            "SELECT external_id FROM events WHERE external_id LIKE 'watchdog:%'")]
        db.close()
        self.assertTrue(any(kind.startswith('watchdog:stalled:') for kind in kinds))

    def test_no_rollback_point_means_no_guessing(self):
        state = self.state()
        wd.unit_state = lambda unit: {'ActiveState': 'failed', 'SubState': 'failed',
                                      'ExecMainStartTimestamp': 'start-bad'}
        wd.http_ok = lambda url, timeout=5, token_file=None: False
        wd.journal_errors = lambda unit, since: []
        wd.cognition_turns = lambda home, since: 0
        outcome = wd.cycle(state, self.store)
        self.assertEqual(outcome['verdict'], 'no-rollback-point')
        self.assertFalse([call for call in self.restarted
                        if call[:2] in (['systemctl', 'restart'], ['systemctl', 'reset-failed'])])

    def test_notice_is_idempotent_for_the_same_restart(self):
        wd.write_notice(self.home, 'same text', external_id='watchdog:rollback:start-x',
                        payload=dict(watchdog='rollback'))
        wd.write_notice(self.home, 'same text', external_id='watchdog:rollback:start-x',
                        payload=dict(watchdog='rollback'))
        db = sqlite3.connect(self.db)
        count = db.execute("SELECT COUNT(*) FROM events WHERE external_id='watchdog:rollback:start-x'").fetchone()[0]
        db.close()
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()





