import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from life.release import NativeWorker,Probe,ReleaseController


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root/'source'
        (self.source/'life').mkdir(parents=True)
        (self.source/'worker.py').write_text('def calc(value):\n    return value - 1\n',encoding='utf-8')
        (self.source/'life/policy.py').write_text('FLAG = 1\n',encoding='utf-8')
        self.control = self.root/'control'
        self.controller = ReleaseController(self.source,self.control,NativeWorker())
        self.patch = dict(changes=[
            dict(file='worker.py',old='return value - 1',new='from helper import adjust\n    return adjust(value)'),
            dict(file='helper.py',action='create',content='def adjust(value):\n    return value + 1\n'),
            dict(file='life/policy.py',old='FLAG = 1',new='FLAG = 2')])
        self.probes = [Probe('worker','calc',{'value':3},4)]

    def tearDown(self):
        self.tmp.cleanup()

    def prepared(self,patch=None,origin=None):
        identity = self.controller.prepare('bundle',patch or self.patch)
        result = self.controller.evaluate(identity,self.probes,origin=origin,max_cost_ratio=10)
        self.assertEqual(result['verdict'],'publishable',result)
        self.controller.request_publish(identity,origin=origin)
        return identity

    def test_ordered_edits_in_one_file_publish_and_roll_back_together(self):
        patch = dict(changes=[
            dict(file='worker.py',old='return value - 1',new='return value + 1'),
            dict(file='worker.py',old='def calc(value):',new='def calc(value=3):')])
        identity = self.prepared(patch)
        manifest,_,_ = self.controller._candidate(identity)
        self.assertEqual(manifest['modified_paths'],['worker.py'])
        self.assertIn('value - 1',(self.source/'worker.py').read_text('utf-8'))
        self.controller.install_source(identity)
        self.assertIn('calc(value=3)',(self.source/'worker.py').read_text('utf-8'))
        self.assertEqual(self.controller.worker.invoke(self.source,'worker','calc',{})[0],4)
        self.controller.rollback_source(identity)
        self.assertIn('value - 1',(self.source/'worker.py').read_text('utf-8'))

    def test_mixed_actions_for_one_file_are_rejected_without_changing_source(self):
        with self.assertRaisesRegex(ValueError,'conflicting_candidate_file_actions'):
            self.controller.prepare('conflict',dict(changes=[
                dict(file='worker.py',old='return value - 1',new='return value + 1'),
                dict(file='worker.py',action='delete',base_hash=self.controller.digest(self.source/'worker.py'))]))
        self.assertTrue((self.source/'worker.py').is_file())

    def test_native_release_runs_the_current_protected_test_suite_entrypoint(self):
        (self.source/'tests').mkdir()
        (self.source/'tests/run_offline_suite.py').write_text("print('offline suite ran')\n",encoding='utf-8')
        result = NativeWorker().run_suite(self.source)
        self.assertTrue(result['ok'],result)
        self.assertIn('offline suite ran',result['log'])

    def test_new_module_and_core_change_activate_as_one_bundle_and_rollback(self):
        identity = self.prepared()
        self.assertFalse((self.source/'helper.py').exists())
        self.controller.install_source(identity)
        self.assertTrue((self.source/'helper.py').is_file())
        self.assertIn('FLAG = 2',(self.source/'life/policy.py').read_text('utf-8'))
        value,_ = self.controller.worker.invoke(self.source,'worker','calc',{'value':8})
        self.assertEqual(value,9)
        self.controller.rollback_source(identity)
        self.assertFalse((self.source/'helper.py').exists())
        self.assertIn('FLAG = 1',(self.source/'life/policy.py').read_text('utf-8'))

    def test_restart_recovers_a_partially_replaced_bundle(self):
        identity = self.prepared()
        self.controller.deployment_phase(identity,'prepared')
        (self.source/'worker.py').write_bytes((self.controller.artifacts/identity/'candidate/worker.py').read_bytes())
        recovered = ReleaseController(self.source,self.control,NativeWorker())
        recovered.install_source(identity)
        self.assertEqual(recovered.deployment(identity)['phase'],'installed')
        self.assertTrue((self.source/'helper.py').exists())

    def test_migration_is_checked_committed_once_and_keeps_data_on_code_rollback(self):
        database = self.root/'life.db'
        with closing(sqlite3.connect(database)) as db,db:
            db.execute('CREATE TABLE notes(id INTEGER PRIMARY KEY,body TEXT)')
            db.execute("INSERT INTO notes VALUES(1,'keep this real row')")
        origin = dict(kind='operator',database=str(database))
        patch = dict(self.patch,migrations=[dict(id='note-extra-v1',sql=[
            "ALTER TABLE notes ADD COLUMN extra TEXT DEFAULT 'default-value'",
            'CREATE INDEX note_body_index ON notes(body)'])])
        identity = self.prepared(patch,origin)
        with closing(sqlite3.connect(database)) as db,db:
            self.assertEqual(len(db.execute('PRAGMA table_info(notes)').fetchall()),2)
        self.controller.install_source(identity)
        self.controller.install_source(identity)
        with closing(sqlite3.connect(database)) as db,db:
            self.assertEqual(db.execute('SELECT * FROM notes').fetchone(),(1,'keep this real row','default-value'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM release_migrations').fetchone()[0],1)
        self.controller.rollback_source(identity)
        with closing(sqlite3.connect(database)) as db,db:
            self.assertEqual(db.execute('SELECT body FROM notes').fetchone()[0],'keep this real row')

    def test_test_oracle_bootstrap_and_destructive_migrations_remain_protected(self):
        for name in ('tests/new_test.py','audit/new_probe.py','supervise.py'):
            with self.subTest(name=name):
                with self.assertRaises(PermissionError):
                    self.controller.prepare('forbidden',dict(changes=[dict(file=name,action='create',content='a=1\n')]))
        with self.assertRaisesRegex(ValueError,'only_additive'):
            self.controller.prepare('destructive',dict(self.patch,migrations=[dict(id='drop',sql=['DROP TABLE notes'])]))

    def test_rollback_checks_all_files_before_overwriting_user_edit(self):
        identity = self.prepared()
        self.controller.install_source(identity)
        (self.source/'helper.py').write_text('USER_EDIT = True\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'rollback_would_overwrite_user_change'):
            self.controller.rollback_source(identity)
        self.assertIn('from helper',(self.source/'worker.py').read_text('utf-8'))
        self.assertIn('USER_EDIT',(self.source/'helper.py').read_text('utf-8'))
