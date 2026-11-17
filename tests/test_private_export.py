import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import zipfile

from scripts.export_private_bundle import Exporter


class PrivateExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'project';self.root.mkdir()
        (self.root/'data').mkdir()
        (self.root/'config.toml').write_text('owner=1\napi_key="fixture-secret"\n', encoding='utf-8')
        (self.root/'persona.md').write_text('Synthetic adult persona.', encoding='utf-8')
        self.db = sqlite3.connect(self.root/'data/life.db');self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE memories(text TEXT)')
        self.db.execute('INSERT INTO memories VALUES(?)', ('Committed memory in WAL',));self.db.commit()

    def test_bundle_preserves_wal_memory_and_secret_without_source_mutation(self):
        before = (self.root/'config.toml').read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            path = Exporter(self.root, self.root/'backups').write()
        self.assertNotIn('fixture-secret', output.getvalue())
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read('project/config.toml'), before)
            self.assertNotIn('project/data/life.db-wal', archive.namelist())
            restored = self.root/'restored.db';restored.write_bytes(archive.read('project/data/life.db'))
            with contextlib.closing(sqlite3.connect(restored)) as db:
                self.assertEqual(db.execute('SELECT text FROM memories').fetchone()[0], 'Committed memory in WAL')
            manifest=json.loads(archive.read('migration/manifest.json'))
            self.assertTrue(any(e['mode']=='sqlite_consistent_backup' for e in manifest['entries']))
        self.assertEqual((self.root/'config.toml').read_bytes(), before)

    def test_only_configuration_memory_and_skills_are_exported(self):
        folder=self.root/'data/settings';folder.mkdir()
        (folder/'server.json').write_text('{"pid":123}')
        (folder/'login.txt').write_text('fixture-password')
        (self.root/'bot.py').write_text('print(123)')
        skills=self.root/'skills';skills.mkdir();(skills/'method.md').write_text('Personal method')
        admin=self.root/'data/memory-admin';admin.mkdir()
        (admin/'request.json').write_text('{"action":"retry"}')
        with contextlib.redirect_stdout(io.StringIO()):path=Exporter(self.root,self.root/'backups').write()
        with zipfile.ZipFile(path) as archive:
            names=archive.namelist()
            self.assertNotIn('project/bot.py', names)
            self.assertIn('project/skills/method.md', names)
            self.assertNotIn('project/data/settings/server.json', names)
            self.assertIn('project/data/settings/login.txt', names)
            self.assertNotIn('project/data/memory-admin/request.json', names)
            self.assertNotIn('recovery-only/project/data/memory-admin/request.json', names)
            self.assertFalse(any('/backups/' in name for name in names))

    def test_busy_bot_refuses_export_instead_of_claiming_cross_store_consistency(self):
        folder=self.root/'data/life-control';folder.mkdir()
        (folder/'worker-health.json').write_text(json.dumps(dict(at=time.time(),cognition_ready=True)))
        with self.assertRaisesRegex(RuntimeError,'先停止 Bot'):
            Exporter(self.root,self.root/'backups').write()

    def test_multi_character_export_preserves_independent_state_and_voice_assets(self):
        folder=self.root/'data/characters';folder.mkdir()
        (folder/'registry.json').write_text(json.dumps({'xiaoyu':{'wanted':False},'firefly':{'wanted':False}}))
        for identity in ('xiaoyu','firefly'):
            root=folder/identity;(root/'data').mkdir(parents=True)
            (root/'config.toml').write_text(f'owner=1\nlife_agent="{identity}"\napi_key="private-{identity}"\n')
            (root/'persona.md').write_text('Persona '+identity)
            (root/'data/voice.wav').write_bytes(b'fixed voice')
            home=root/'data/dsh-home/sessions';home.mkdir(parents=True)
            (home/'history.json').write_text(json.dumps({'memory':identity}))
            with contextlib.closing(sqlite3.connect(root/'data/life.db')) as db:
                db.execute('CREATE TABLE memories(text TEXT)')
                db.execute('INSERT INTO memories VALUES(?)',(identity,));db.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            path=Exporter(self.root,self.root/'backups',verify=True).write()
        with zipfile.ZipFile(path) as archive:
            for identity in ('xiaoyu','firefly'):
                prefix=f'project/data/characters/{identity}'
                self.assertIn(prefix+'/config.toml',archive.namelist())
                self.assertIn(prefix+'/data/life.db',archive.namelist())
                self.assertIn(prefix+'/data/voice.wav',archive.namelist())
                self.assertIn(prefix+'/data/dsh-home/sessions/history.json',archive.namelist())
            manifest=json.loads(archive.read('migration/manifest.json'))
            self.assertEqual(manifest['scope'],'multi_character_private_state_and_assets')
            self.assertEqual(sum(e['mode']=='sqlite_consistent_backup' for e in manifest['entries']),2)

    def test_multi_character_export_refuses_a_wanted_running_person(self):
        folder=self.root/'data/characters';folder.mkdir()
        (folder/'registry.json').write_text(json.dumps({'xiaoyu':{'wanted':True}}))
        with self.assertRaisesRegex(RuntimeError,'停止所有人物'):
            Exporter(self.root,self.root/'backups').write()


if __name__=='__main__':unittest.main()
