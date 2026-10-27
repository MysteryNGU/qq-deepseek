"""Independent persona routing, migration and scoped self changes."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from life.agent_session import SessionFailure,canonical
from life.persona_files import PersonaFiles
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime
from life.self_change import SelfChange,file_hash


class PersonaFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = SimpleNamespace(owner=1,persona='persona.md',public_persona='personas/public.md')
        self.files = PersonaFiles(self.root,self.cfg)
        self.files.public.parent.mkdir(parents=True)
        self.files.private.write_text('# Private\nOWNER_ONLY_STYLE\nPRIVATE_WRITING_RULES\n',encoding='utf-8')
        self.files.public.write_text('# Public\nPUBLIC_ONLY_STYLE\n',encoding='utf-8')
        self.life = LifeRuntime(self.root/'life.db')
        self.role = RoleAgent(self.life,SimpleNamespace(model='test'),self.cfg,self.root,AsyncMock())
        self.scope = self.life.scope('qq:private:1','1')
        self.life.ingest(self.scope,'message','user_report','hello',actor_id='1')

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    def legacy(self):
        source = '<!-- persona:public -->\nPUBLIC_ONLY_STYLE\n<!-- /persona:public -->\n\n'
        source += '<!-- persona:private -->\nOWNER_ONLY_STYLE\n<!-- /persona:private -->\n'
        self.files.private.write_text(source,encoding='utf-8')
        self.files.public.write_text('<!-- Derived persona sha256='+hashlib.sha256(source.encode()).hexdigest()+
            ' -->\nPUBLIC_ONLY_STYLE\n',encoding='utf-8')
        return source

    def test_legacy_split_preserves_both_sections_and_is_idempotent(self):
        self.legacy()
        self.assertTrue(self.files.migrate())
        self.assertEqual(self.files.private.read_text('utf-8'),'OWNER_ONLY_STYLE\n')
        self.assertEqual(self.files.public.read_text('utf-8'),'PUBLIC_ONLY_STYLE\n')
        before=(file_hash(self.files.private),file_hash(self.files.public))
        self.assertFalse(self.files.migrate())
        self.assertEqual(before,(file_hash(self.files.private),file_hash(self.files.public)))

    def test_interrupted_split_recovers_from_the_unchanged_legacy_source(self):
        source=self.legacy()
        from life.persona import write_view
        def write(path,body):
            if path==self.files.private:raise OSError('interrupted before private commit')
            write_view(path,body)
        with patch('life.persona_files.write_view',side_effect=write):
            with self.assertRaises(OSError):self.files.migrate()
        self.assertEqual(self.files.private.read_text('utf-8'),source)
        self.assertNotIn('OWNER_ONLY_STYLE',self.files.public.read_text('utf-8'))
        self.assertTrue(self.files.migrate())

    def test_migration_does_not_overwrite_independently_edited_public_source(self):
        source=self.legacy()
        self.files.public.write_text('An independently edited public persona.',encoding='utf-8')
        with self.assertRaises(ValueError):self.files.migrate()
        self.assertEqual(self.files.private.read_text('utf-8'),source)
        self.assertEqual(self.files.public.read_text('utf-8'),'An independently edited public persona.')

    def test_private_public_and_group_routes_select_exactly_one_source(self):
        for scope,private in [(self.scope,True),(self.life.scope('qq:private:2','2'),False),
                (self.life.scope('qq:group:3','1'),False)]:
            self.life.ingest(scope,'message','user_report','hello',actor_id=scope.participant_id)
            root=self.role.sessions.binding(scope)
            static=self.role.stable_context(scope,{})
            packet=json.loads(self.role.context(root,scope,{}).split('当前状态与材料：\n')[1])
            read=self.role.read(scope,dict(kind='self'),root)
            for text in (static,packet.get('persona',''),read['persona']):
                # Dynamic context may intentionally omit the full persona.
                if text:
                    self.assertIn('OWNER_ONLY_STYLE' if private else 'PUBLIC_ONLY_STYLE',text)
                    self.assertNotIn('PUBLIC_ONLY_STYLE' if private else 'OWNER_ONLY_STYLE',text)
                    if not private:self.assertNotIn('PRIVATE_WRITING_RULES',text)
            self.assertEqual(read['persona_audience'],'private' if private else 'public')

    def test_missing_public_file_never_falls_back_to_private_source(self):
        self.files.public.unlink()
        scope=self.life.scope('qq:group:9','')
        self.assertNotIn('OWNER_ONLY_STYLE',self.role.stable_context(scope,{}))
        self.assertEqual(self.files.read(False),'')
        self.assertFalse(self.files.public.exists())

    def test_explicit_public_read_returns_the_public_file_hash(self):
        read=self.role.read(self.scope,dict(kind='self',persona_audience='public'))
        self.assertEqual(read['persona_hash'],file_hash(self.files.public))
        self.assertEqual(read['persona_path'],'personas/public.md')
        self.assertIn('PUBLIC_ONLY_STYLE',read['persona'])
        self.assertNotIn('OWNER_ONLY_STYLE',read['persona'])

    def test_other_private_chats_and_owner_in_a_group_cannot_read_private_persona(self):
        for scope in (self.life.scope('qq:private:2','2'),self.life.scope('qq:group:3','1')):
            with self.assertRaises(SessionFailure):
                self.role.read(scope,dict(kind='self',persona_audience='private'))

    async def test_public_self_change_preserves_private_bytes_and_survives_restart(self):
        before=self.files.private.read_bytes()
        service=SelfChange(self.life,self.cfg,self.root)
        result=await service.apply(self.scope,dict(target='persona_public',content='PUBLIC_UPDATED',
            base_hash=file_hash(self.files.public)),'public-change')
        self.assertEqual(result['public_persona_hash'],file_hash(self.files.public))
        self.assertEqual(self.files.private.read_bytes(),before)
        PersonaFiles(self.root,self.cfg).migrate()
        SelfChange(self.life,self.cfg,self.root).recover()
        self.assertEqual(self.files.public.read_text('utf-8'),'PUBLIC_UPDATED')
        self.assertNotIn('PUBLIC_UPDATED',self.role.stable_context(self.scope,{}))

    async def test_private_self_change_preserves_public_bytes(self):
        before=self.files.public.read_bytes()
        service=SelfChange(self.life,self.cfg,self.root)
        await service.apply(self.scope,dict(target='persona',content='PRIVATE_UPDATED',
            base_hash=file_hash(self.files.private)),'private-change')
        self.assertEqual(self.files.public.read_bytes(),before)
        self.assertNotIn('PRIVATE_UPDATED',self.role.stable_context(self.life.scope('qq:group:3',''),{}))

    async def test_stale_public_hash_cross_target_paths_and_mixed_sections_are_rejected(self):
        service=SelfChange(self.life,self.cfg,self.root)
        before=(self.files.private.read_bytes(),self.files.public.read_bytes())
        for key,proposal in [('stale',dict(target='persona_public',content='stale',base_hash='old')),
            ('wrong-file',dict(target='persona_public',files=[dict(path='persona.md',content='wrong')])) ,
            ('mixed',dict(target='persona_public',content='<!-- persona:private -->\nSECRET\n<!-- /persona:private -->'))]:
            with self.assertRaises(SessionFailure):await service.apply(self.scope,proposal,key)
        self.assertEqual(before,(self.files.private.read_bytes(),self.files.public.read_bytes()))

    async def test_group_actor_cannot_use_global_persona_self_change(self):
        service=SelfChange(self.life,self.cfg,self.root)
        with self.assertRaises(SessionFailure):
            await service.apply(self.life.scope('qq:group:3','1'),dict(target='persona_public',content='unauthorized'),'group-change')

    def test_old_prepared_combined_persona_is_rejected_without_blocking_recovery(self):
        service=SelfChange(self.life,self.cfg,self.root)
        content='<!-- persona:public -->PUBLIC<!-- /persona:public -->\n<!-- persona:private -->PRIVATE<!-- /persona:private -->'
        manifest=[dict(path='persona.md',content=content,base_hash=file_hash(self.files.private),
            target_hash=hashlib.sha256(content.encode()).hexdigest())]
        before=(self.files.private.read_bytes(),self.files.public.read_bytes())
        with self.life.repo.tx():
            self.life.repo._exec("INSERT INTO self_changes VALUES('old-combined','hash',?,'prepared',?,NULL,1,1)",
                (self.scope.scope_id,canonical(manifest)))
        service.recover()
        self.assertEqual(self.life.repo._one("SELECT phase FROM self_changes WHERE id='old-combined'")['phase'],'rejected')
        self.assertEqual(before,(self.files.private.read_bytes(),self.files.public.read_bytes()))

    def test_custom_public_path_is_respected_and_colliding_paths_are_invalid(self):
        cfg=SimpleNamespace(persona='persona.md',public_persona='another-public.md')
        files=PersonaFiles(self.root,cfg)
        self.assertEqual(files.public,self.root/'another-public.md')
        with self.assertRaises(ValueError):PersonaFiles(self.root,SimpleNamespace(persona='persona.md',public_persona='./persona.md'))


if __name__=='__main__':
    unittest.main()
