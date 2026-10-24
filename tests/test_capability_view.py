"""Capability and method extensions use synthetic scopes and existing commit APIs."""
import asyncio
import hashlib
import json
from unittest.mock import patch
import unittest

from tests.role_fixture import Fixture
from life.agent_session import SessionFailure,canonical
from life.self_change import SelfChange
from life.resource_policy import estimate_tokens


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role=self.f.role;self.role.capability_protocol=1
        self.role.media_adapter_protocol=1
        self.view=self.role.capabilities
        self.store=self.view.methods
        self.native=dict(ok=True,version='native-v1',tools=[dict(name='synthetic_read',description='Read a synthetic sample.',parameters={})],methods=[])
        self.calls=[]
        def catalog(sid,generation):
            self.calls.append((sid,generation));return self.native
        self.f.bridge.current_agent_catalog=catalog
        self.changes=SelfChange(self.f.life,self.f.cfg,self.f.root)

    async def save(self,name='study',text='Actual reusable method.',**values):
        proposal=dict(target='skill',name=name,content=text,create=True,base_hash=None,
            metadata=dict(description='Synthetic study method'),**values)
        return await self.changes.apply(self.f.scope,proposal,'change-'+name+str(len(self.calls)),require_skill_baseline=True)

    async def test_A02_new_method_hash_update_conflict_and_old_receipt_replay(self):
        receipt=await self.save()
        item=self.store.read(self.f.scope,name='study.md')
        self.assertEqual(item['hash'],hashlib.sha256(item['text'].encode()).hexdigest())
        changed=dict(target='skill',name='study',content='A revised method',base_hash=item['hash'])
        updated=await self.changes.apply(self.f.scope,changed,'update',require_skill_baseline=True)
        self.assertNotEqual(item['hash'],updated['methods'][0]['hash'])
        with self.assertRaises(SessionFailure):
            await self.changes.apply(self.f.scope,changed,'stale-write',require_skill_baseline=True)
        self.assertEqual(updated,await self.changes.apply(self.f.scope,changed,'update',require_skill_baseline=True))
        with self.assertRaises(SessionFailure):
            await self.changes.apply(self.f.scope,dict(target='skill',name='study',content='No baseline'),'no-baseline',require_skill_baseline=True)
        self.assertFalse(receipt['behavior_validated'])
        with self.assertRaises(SessionFailure):
            await self.changes.apply(self.f.scope,dict(target='persona',files=[dict(path='skills/study.md',content='Bypass baseline')]),
                'alternate-method-write',require_skill_baseline=True)

    async def test_A02_file_applied_index_failure_recovers_from_existing_manifest(self):
        with patch.object(self.store,'reconcile',side_effect=OSError('synthetic index failure')):
            with self.assertRaises(OSError):
                await self.save('recover')
        self.assertEqual((self.f.root/'skills/recover.md').read_text('utf-8'),'Actual reusable method.')
        self.changes.recover()
        self.assertEqual(self.store.read(self.f.scope,name='recover.md')['summary'],'Synthetic study method')
        self.assertEqual(self.f.life.repo._one("SELECT phase FROM self_changes")['phase'],'applied')

    async def test_A03_private_public_and_explicit_delegate_method_visibility(self):
        await self.save('private')
        public=dict(target='skill',name='public',content='Generic method without private materials.',create=True,base_hash=None,
            metadata=dict(description='Generic public method',visibility='public',public_content=True))
        await self.changes.apply(self.f.scope,public,'public',require_skill_baseline=True)
        group=self.f.life.scope('qq:group:2','1')
        visible=self.store.catalog(group)
        self.assertEqual([i['summary'] for i in visible],['Generic public method'])
        self.assertNotIn('private',canonical(visible))
        call=dict(caller='delegate',session_id='synthetic-child')
        self.assertEqual(self.store.catalog(self.f.scope,call),[])
        method=self.store.read(self.f.scope,name='private.md')
        with self.f.life.repo.tx():
            self.f.life.repo._exec('INSERT INTO meta VALUES(?,?)',('delegate_methods:synthetic-child',canonical([method['ref']])))
        self.assertEqual([i['ref'] for i in self.store.catalog(self.f.scope,call)],[method['ref']])
        with self.assertRaises(SessionFailure):
            self.store.read(group,name='private.md')
        with self.assertRaises(SessionFailure):
            self.store.validate_metadata(dict(visibility='public',public_content=True,sources=[self.f.event.id]))

    async def test_A03_external_file_changes_and_deletion_cannot_load_cached_body(self):
        await self.save()
        first=self.store.read(self.f.scope,name='study.md')
        path=self.f.root/'skills/study.md';path.write_text('Changed on disk',encoding='utf-8')
        with self.assertRaises(SessionFailure):
            self.store.read(self.f.scope,ref=first['ref'],expected_hash=first['hash'])
        self.assertEqual(self.store.read(self.f.scope,name='study.md')['text'],'Changed on disk')
        path.unlink()
        with self.assertRaises(SessionFailure):
            self.store.read(self.f.scope,ref=first['ref'])

    async def test_A03_forget_revokes_methods_without_recreating_them_from_files(self):
        await self.save()
        item=self.store.read(self.f.scope,name='study.md')
        self.f.life.forget(self.f.scope)
        self.assertTrue((self.f.root/'skills/study.md').is_file())
        self.assertEqual(self.store.catalog(self.f.scope),[])
        with self.assertRaises(SessionFailure):
            self.store.read(self.f.scope,ref=item['ref'])
        self.assertEqual(self.store.get_meta('study.md')['state'],'revoked')

    async def test_A03_delegate_grants_freeze_at_dispatch_and_change_with_next_assignment(self):
        await self.save('first');await self.save('second')
        first=self.store.read(self.f.scope,name='first.md')['ref']
        second=self.store.read(self.f.scope,name='second.md')['ref']
        task=self.f.task()
        with self.f.life.repo.tx():
            self.role.intent(self.f.scope,dict(ref=task['ref'],base_revision=task['revision'],
                checkpoint=dict(method_refs=[first]),sources=[self.f.event.id]),self.f.binding)
        binding=self.role.sessions.require(self.f.binding['session_id'])
        self.role.before_tool(binding,self.f.scope,dict(tool='subagent',call_id='assignment-1',
            session_id=binding['session_id'],arguments=dict(prompt='Synthetic delegated study')))
        self.role.tasks.ensure(self.f.scope,task['ref'],task['goal'],checkpoint=dict(method_refs=[second]))
        with self.f.life.repo.tx():
            self.role._register_delegate('synthetic-child',binding['session_id'],binding,'assignment-1')
        child=dict(caller='delegate',session_id='synthetic-child')
        self.assertEqual([i['ref'] for i in self.store.catalog(self.f.scope,child)],[first])
        self.role.before_tool(binding,self.f.scope,dict(tool='subagent',call_id='assignment-2',
            session_id=binding['session_id'],arguments=dict(prompt='Next delegated study')))
        with self.f.life.repo.tx():
            self.role._register_delegate('synthetic-child',binding['session_id'],binding,'assignment-2')
        self.assertEqual([i['ref'] for i in self.store.catalog(self.f.scope,child)],[second])

    async def test_A01_directory_is_current_agent_only_and_does_not_execute(self):
        result=await self.view.read(self.f.scope,dict(category='tools'),self.f.binding)
        self.assertEqual(self.calls,[(self.f.binding['session_id'],1)])
        self.assertEqual(result['items'][0]['invocation']['tool'],'synthetic_read')
        detail=await self.view.read(self.f.scope,dict(ref='tool:dsh:synthetic_read'),self.f.binding)
        self.assertEqual(detail['item']['schema']['parameters'],{})
        self.assertEqual(detail['item']['availability']['readiness'],'unknown')
        self.assertFalse(self.f.life.repo._one('SELECT 1 FROM role_operations'))
        with self.assertRaises(SessionFailure):
            await self.view.read(self.f.scope,dict(ref='tool:dsh:missing'),self.f.binding)

    async def test_A03_versioned_cursor_and_registry_failure_do_not_use_stale_snapshot(self):
        self.native['tools']*=2
        self.native['tools'][1]=dict(name='another_read',description='Another',parameters={})
        first=await self.view.read(self.f.scope,dict(limit=1),self.f.binding)
        second=await self.view.read(self.f.scope,dict(limit=1,cursor=first['next_cursor']),self.f.binding)
        self.assertNotEqual(first['items'][0]['ref'],second['items'][0]['ref'])
        self.native['version']='native-v2'
        with self.assertRaises(SessionFailure):
            await self.view.read(self.f.scope,dict(limit=1,cursor=first['next_cursor']),self.f.binding)
        with patch.object(self.f.bridge,'current_agent_catalog',side_effect=RuntimeError('SDK offline')):
            with self.assertRaises(SessionFailure):
                await self.view.read(self.f.scope,{},self.f.binding)
        self.assertFalse(self.view.snapshots)

    async def test_A03_generation_and_forget_epoch_rechecked_after_async_read(self):
        def changed(*_args):
            self.f.bridge._generation+=1;return self.native
        self.f.bridge.current_agent_catalog=changed
        with self.assertRaises(SessionFailure):
            await self.view.read(self.f.scope,{},self.f.binding)

    async def test_A09_method_summary_is_short_and_never_injects_body(self):
        await self.save(text='private unique method body '*200)
        summary=self.view.summary(self.f.scope,self.f.binding)
        self.assertLessEqual(estimate_tokens(summary),200)
        self.assertNotIn('private unique',canonical(summary))
        self.assertEqual(summary,self.view.summary(self.f.scope,self.f.binding))

    async def test_A09_large_schema_pages_obey_complete_result_budget_and_version(self):
        from dataclasses import replace
        self.role.resources=replace(self.role.resources,text_preview_target_tokens=200,text_result_max_tokens=600,
            text_result_max_utf8_bytes=2000)
        self.native['tools'][0]['parameters']={str(i):dict(description='实际 schema 内容🙂'*100) for i in range(20)}
        detail=await self.view.read(self.f.scope,dict(ref='tool:dsh:synthetic_read'),self.f.binding)
        self.assertTrue(detail['truncated'])
        first=await self.view.read(self.f.scope,detail['detail_read'],self.f.binding)
        self.assertLessEqual(estimate_tokens(first),600)
        self.assertLessEqual(len(canonical(first).encode()),2000)
        self.assertGreater(first['next_offset'],0)
        with self.assertRaises(SessionFailure):
            await self.view.read(self.f.scope,dict(ref='tool:dsh:synthetic_read',field='schema',offset=first['next_offset'],version='stale'),self.f.binding)

    async def test_A03_protocol_mismatch_and_private_names_stay_hidden(self):
        await self.save('secret-title')
        self.role.capability_protocol=None
        with self.assertRaises(SessionFailure):
            await self.view.read(self.f.scope,{},self.f.binding)
        self.role.capability_protocol=1
        group=self.f.life.scope('qq:group:2','2')
        result=await self.view.read(group,{},self.role.sessions.binding(group))
        self.assertNotIn('secret-title',canonical(result))

    def test_policy_declarations_cannot_be_overridden_by_model_arguments(self):
        self.assertEqual(self.view.policy('mcp__bili__bili_search')['effect'],'read')
        self.assertTrue(self.view.policy('mcp__bili__bili_reply')['principal_adoption'])
        self.assertTrue(self.view.policy('mcp__unknown__write')['principal_adoption'])
        self.view.policies.append(dict(tool='synthetic_write',effect='write',principal_adoption=True,
            package_id='test-package',version='v1'))
        self.assertEqual(self.view.policy('synthetic_write')['effect'],'unknown')
        self.assertEqual(self.view.policy('synthetic_write',dict(package_id='test-package',version='v1'))['effect'],'write')
        declaration=dict(package_id='independent-package',version='1',policy=dict(effect='read',principal_adoption=False,audience='owner'))
        self.assertEqual(self.view.policy('brand_new_read',declaration)['effect'],'read')

    def test_A07_registered_read_policy_keeps_confirmed_failures_out_of_unknown_write_state(self):
        self.f.cfg.continuity_enabled=True;self.role.continuity_protocol=1
        self.f.task();binding=self.role.sessions.require(self.f.binding['session_id'])
        from life.text import stable_id
        identity=dict(package_id='independent-package',version='1',policy=dict(effect='read',principal_adoption=False,audience='owner'))
        for index in range(3):
            call='read-'+str(index)
            self.role.before_tool(binding,self.f.scope,dict(tool='brand_new_read',tool_identity=identity,
                call_id=call,session_id=binding['session_id'],arguments={}))
            self.role._observe_result(binding['session_id'],dict(type='tool/result',seq=index+1,data=dict(message=dict(
                toolCallId=call,isError=True,content=[dict(type='text',text='{"ok":false,"code":"not_ready"}')]))))
            op=self.f.life.repo._one('SELECT status FROM role_operations WHERE id=?',(stable_id('roleop',binding['session_id'],call),))
            self.assertEqual(op['status'],'failed')
        self.assertEqual(self.role.initiative.guards(self.f.scope)[0]['phase'],'suspended')
