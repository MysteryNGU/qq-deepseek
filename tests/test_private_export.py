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

    def test_process_state_excluded_and_old_admin_requests_not_auto_restored(self):
        folder=self.root/'data/settings';folder.mkdir()
        (folder/'server.json').write_text('{"pid":123}')
        (folder/'login.txt').write_text('fixture-password')
        admin=self.root/'data/memory-admin';admin.mkdir()
        (admin/'request.json').write_text('{"action":"retry"}')
        with contextlib.redirect_stdout(io.StringIO()):path=Exporter(self.root,self.root/'backups').write()
        with zipfile.ZipFile(path) as archive:
            names=archive.namelist()
            self.assertNotIn('project/data/settings/server.json', names)
            self.assertIn('project/data/settings/login.txt', names)
            self.assertNotIn('project/data/memory-admin/request.json', names)
            self.assertIn('recovery-only/project/data/memory-admin/request.json', names)
            self.assertFalse(any('/backups/' in name for name in names))

    def test_busy_bot_refuses_export_instead_of_claiming_cross_store_consistency(self):
        folder=self.root/'data/life-control';folder.mkdir()
        (folder/'worker-health.json').write_text(json.dumps(dict(at=time.time(),cognition_ready=True)))
        with self.assertRaisesRegex(RuntimeError,'先停止 Bot'):
            Exporter(self.root,self.root/'backups').write()


if __name__=='__main__':unittest.main()
