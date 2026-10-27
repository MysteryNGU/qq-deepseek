"""One-way activation keeps evidence and removes old retrieval storage."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from life.memory_migration import activate
from tests.role_fixture import Fixture


class MemoryMigrationTests(unittest.TestCase):
    def test_old_code_is_refused_after_one_way_activation(self):
        from supervise import validate_data_compatibility
        f=Fixture()
        try:
            with TemporaryDirectory() as directory:
                root=Path(directory)
                (root/'bot.py').write_text('pass',encoding='utf-8')
                (root/'life').mkdir()
                (root/'life/runtime.py').write_text('SUPPORTED_MEMORY_SCHEMA = 1\nSUPPORTED_RELATIONSHIP_SCHEMA = 2',encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError,'memory_schema_incompatible'):
                    validate_data_compatibility(root,database=f.life.repo.db)
        finally:
            f.close()

    def test_activation_keeps_evidence_indexes_it_and_retires_old_histories(self):
        f=Fixture()
        try:
            repo=f.life.repo
            f.role.sessions.enqueue(f.binding,f.event)
            with repo.tx():
                repo._exec('CREATE TABLE memory_terms(memory_id TEXT,term TEXT)')
                repo._exec("INSERT INTO memory_terms VALUES('old','obsolete')")
                repo._exec('DELETE FROM meta WHERE k=?',('memory_schema:'+f.life.agent_id,))
            original=repo.get_event(f.event.id)
            activate(f.life)
            self.assertEqual(repo.get_event(f.event.id).text,original.text)
            self.assertFalse(repo._one("SELECT 1 FROM sqlite_master WHERE name='memory_terms'"))
            self.assertTrue(f.life.catalog.live(f.life.catalog.resolve(f.event.id)))
            self.assertEqual(repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(f.binding['session_id'],))['status'],'retired')
            self.assertEqual(repo._one('SELECT input_id FROM agent_inputs WHERE session_id=?',(f.binding['session_id'],))['input_id'],f.event.id)
            self.assertEqual(repo._one('SELECT v FROM meta WHERE k=?',('memory_schema:'+f.life.agent_id,))['v'],'2')
            activate(f.life)
            self.assertEqual(repo.get_event(f.event.id).text,original.text)
        finally:
            f.close()
