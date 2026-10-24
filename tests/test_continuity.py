"""Continuity on synthetic events, with no production state or model access."""
import unittest
from unittest.mock import patch

from tests.role_fixture import Fixture
from life.agent_session import SessionFailure,canonical
from life.resource_policy import ResourcePolicy


class ContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.cfg.continuity_enabled = True
        self.f.role.continuity_protocol = 1
        self.c = self.f.role.continuity
        self.c.activate(self.f.scope)

    def proposal(self,text='想继续了解水彩画的构图',**fields):
        p = dict(kind='inference',text=text,sources=[self.f.event.id],cues=['水彩画'],
                 continuity=dict(type='thought',action='create'))
        p.update(fields)
        return p

    def save(self,p=None,key='save-1'):
        with self.f.life.repo.tx():
            return self.f.role.remember(self.f.scope,p or self.proposal(),self.f.binding,call_key=key)

    def test_C04_note_duplicate_replace_archive_and_no_appraisal(self):
        first = self.save()
        self.assertEqual(first['note_ref'],self.save(key='other-call')['note_ref'])
        note = self.c.memory(self.f.scope,first['note_ref'])
        self.assertEqual(note.source_kind,'inference')
        with self.f.life.repo.tx():
            self.f.life.repo._exec('INSERT INTO idle_states VALUES(?,5,0,NULL)',(self.f.scope.scope_id,))
            self.f.role.rest(self.f.scope,dict(thought=note.summary,keep_thought=True,
                sources=[self.f.event.id],reflection_outcome='new_thought'),self.f.binding)
        self.assertEqual(self.f.life.repo._one('SELECT unchanged_streak FROM idle_states')['unchanged_streak'],5)
        replacement = self.proposal('构图还要考虑留白',continuity=dict(type='thought',action='replace',
            base_ref=note.id,base_version=note.version))
        revised = self.save(replacement)
        self.assertEqual(self.c.memory(self.f.scope,note.id).status,'superseded')
        self.assertNotIn(note.id,[n['ref'] for n in self.c.notes(self.f.scope)])
        self.assertEqual(revised['note_ref'],self.save(replacement,key='retry')['note_ref'])
        archived = self.save(self.proposal('',continuity=dict(type='thought',action='archive',
            base_ref=revised['note_ref'],base_version=1)))
        self.assertEqual(archived['status'],'archived')
        self.assertEqual(self.save(replacement,key='late')['status'],'archived')
        self.assertEqual(self.f.life.repo._one('SELECT COUNT(*) AS n FROM state_journal')['n'],0)

    def test_C03_sources_and_refs_cannot_cross_audience(self):
        note = self.save()
        group = self.f.life.scope('qq:group:2','2')
        with self.assertRaises(SessionFailure):
            self.c.notes(group,ref=note['note_ref'])
        event,_ = self.f.life.ingest_message(group,'unrelated group content')
        with self.assertRaises(SessionFailure):
            self.save(self.proposal(sources=[event.id]))

    def test_C05_version_conflict_and_forget_invalidates_descendants(self):
        note = self.save()
        with self.assertRaises(SessionFailure):
            self.save(self.proposal('changed',continuity=dict(type='thought',action='replace',base_ref=note['note_ref'],base_version=50)))
        child = self.save(self.proposal('解释的延伸',sources=[note['note_ref']]))
        self.f.life.forget(self.f.scope,memory_ids=[note['note_ref']])
        self.assertEqual(self.f.life.repo.get_memory(child['note_ref']).status,'redacted')
        self.assertFalse(self.c.notes(self.f.scope))

    def test_interest_without_binding_does_not_run_or_resume(self):
        prior = self.f.task()
        bound = self.f.role.sessions.require(self.f.binding['session_id'])
        with self.f.life.repo.tx():
            new = self.f.role.intent(self.f.scope,dict(kind='ongoing',goal='了解画风',origin='self',bind=False,
                interest=dict(cues=['水彩'],open_question='如何留白？'),sources=[self.f.event.id]),bound)['intent']
        self.assertNotEqual(new['ref'],prior['ref'])
        self.assertEqual(self.f.role.sessions.require(bound['session_id'])['intent_ref'],prior['ref'])
        self.assertEqual(new['interest']['open_question'],'如何留白？')
        with self.assertRaises(SessionFailure):
            self.f.role.intent(self.f.scope,dict(action='resume',ref=new['ref'],base_revision=1,bind=False),bound)

    def test_C07_activation_boundary_does_not_claim_old_history(self):
        self.assertEqual(self.c.reflection_batch(self.f.scope,self.f.binding)['items'],[])
        cursor = self.c.activate(self.f.scope)
        self.assertTrue(cursor['activation_boundary'])
        self.assertEqual(cursor['through_seq'],self.f.life.repo.get_event(self.f.event.id).ingest_seq)

    def issued_batch(self,n=12):
        for i in range(n):
            self.f.life.ingest_message(self.f.scope,'new actual event '+str(i))
        batch = self.c.read(self.f.scope,dict(view='reflection_batch',limit=12),self.f.binding)
        with self.f.life.repo.tx():
            self.f.life.repo._exec('INSERT INTO life_calls VALUES(?,?,?,?,?)',
                (self.f.binding['session_id'],'read-batch','digest',canonical(dict(ok=True,**batch)),0))
        return batch

    def reflection(self,batch,**fields):
        proposal = self.proposal('从新材料里发现了一个可继续的问题',sources=[batch['items'][0]['id']],
            continuity=dict(type='reflection',action='create',batch_ref=batch['batch_ref'],outcome='summarized',
                            reviewed_ids=[i['id'] for i in batch['items']]))
        proposal['continuity'].update(fields)
        return proposal

    def test_C06_batch_requires_all_ids_and_atomic_cursor(self):
        batch = self.issued_batch()
        cursor = self.c.activate(self.f.scope)
        with self.assertRaises(SessionFailure):
            self.save(self.reflection(batch,reviewed_ids=[batch['items'][-1]['id']]))
        self.assertEqual(self.c.activate(self.f.scope),cursor)
        with patch.object(self.f.life.memory,'remember',side_effect=RuntimeError('simulated storage failure')):
            with self.assertRaises(RuntimeError):
                self.save(self.reflection(batch))
        self.assertEqual(self.c.activate(self.f.scope),cursor)
        result = self.save(self.reflection(batch))
        self.assertEqual(result['through_seq'],batch['through_seq'])
        self.assertEqual(self.c.activate(self.f.scope)['version'],cursor['version']+1)
        with self.assertRaises(SessionFailure):
            self.save(self.reflection(batch))

    def test_C06_no_new_insight_advances_without_memory(self):
        batch = self.issued_batch(2)
        proposal = self.reflection(batch,outcome='no_new_insight')
        proposal.pop('text')
        result = self.save(proposal)
        self.assertEqual(result['outcome'],'no_new_insight')
        self.assertFalse(self.c.notes(self.f.scope))

    def test_C01_C02_three_routes_share_budget_without_reusing_old_greeting(self):
        self.save()
        with self.f.life.repo.tx():
            for i in range(5):
                self.f.role.intent(self.f.scope,dict(kind='ongoing',goal='研究水彩画'+str(i),bind=False,
                    interest=dict(cues=['水彩画']),sources=[self.f.event.id]),self.f.binding)
        self.f.life.ingest_message(self.f.scope,'你好')
        with patch.object(self.f.life.memory,'retrieve_multi',wraps=self.f.life.memory.retrieve_multi) as retrieve:
            preview,_memory = self.c.context(self.f.scope,self.f.binding,{})
        cues = retrieve.call_args.args[1]
        self.assertNotIn('你好',cues)
        self.assertTrue(any('水彩' in c for c in cues))
        self.assertLessEqual(preview['optional_estimated_tokens'],self.f.role.resources.memory_context_target_tokens)
        self.assertLessEqual(len(preview['items']),5)
        self.assertIn('thought',{item['kind'] for item in preview['items']})

    def test_C08_tool_result_is_not_goal_success(self):
        self.f.role.before_tool(self.f.binding,self.f.scope,dict(tool='read_file',call_id='read',
            session_id=self.f.binding['session_id'],arguments=dict(path='synthetic')))
        from life.text import stable_id
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE role_operations SET status='succeeded' WHERE id=?",
                (stable_id('roleop',self.f.binding['session_id'],'read'),))
        activity = self.c.activities(self.f.scope)[0]
        self.assertEqual(activity['phase'],'result_available')
        self.assertFalse(activity['goal_verified'])

    def test_scoped_reference_search_and_mixed_activity_pagination(self):
        older = self.save(self.proposal('想了解雕塑艺术'),key='older')['note_ref']
        for i in range(6):
            self.save(self.proposal('继续阅读水彩画资料'+str(i)),key='recent-'+str(i))
        self.assertEqual([n['ref'] for n in self.c.notes(self.f.scope,query='雕塑',limit=1)],[older])
        fact = self.f.life.memory.remember(self.f.scope,'episode','实际发生的事情',[self.f.event.id],source_kind='user_report')
        with self.assertRaises(SessionFailure):
            self.c.notes(self.f.scope,ref=fact.id)
        with self.f.life.repo.tx():
            for index in (1,3):
                self.f.life.repo._exec('INSERT INTO role_operations(id,session_id,scope_id,tool,request_hash,arguments,status,created) '
                    'VALUES(?,?,?,?,?,?,?,?)',('op-'+str(index),self.f.binding['session_id'],self.f.scope.scope_id,
                    'read_file','fixture',canonical(dict(path='study-'+str(index))),'succeeded',index))
        from life.work_queue import WorkQueue
        WorkQueue(self.f.life.repo,clock=lambda:2).enqueue(self.f.scope,'local_media',dict(sources=[self.f.event.id]),key='media-2')
        first = self.c.read(self.f.scope,dict(view='activities',limit=1),self.f.binding)
        second = self.c.read(self.f.scope,dict(view='activities',limit=1,cursor=first['next_cursor']),self.f.binding)
        self.assertEqual([first['items'][0]['ref'],second['items'][0]['ref']],['op-3','media-2'])
        exact = self.c.read(self.f.scope,dict(view='activities',ref='op-1'),self.f.binding)
        self.assertEqual([a['ref'] for a in exact['items']],['op-1'])
        self.assertEqual([a['ref'] for a in self.c.activities(self.f.scope,query='study-1')],['op-1'])
        with self.assertRaises(SessionFailure):
            self.c.read(self.f.life.scope('qq:group:2','2'),dict(view='activities',ref='op-1'),self.f.binding)
        with self.assertRaises(SessionFailure):
            self.c.read(self.f.scope,dict(view='overview',ref='op-1'),self.f.binding)

    async def test_rest_note_is_atomic_with_end_of_turn(self):
        call = dict(session_id=self.f.binding['session_id'],root_session_id=self.f.binding['session_id'],
            call_id='rest',caller='role',kind='life.rest',proposal=dict(keep_thought=True,thought='想继续读一读',
                thought_cues=['阅读'],sources=[self.f.event.id],next_attention=dict(mode='invalid')))
        failed = await self.f.role.callback(call)
        self.assertFalse(failed['ok'])
        self.assertFalse(self.c.notes(self.f.scope))
        call['proposal']['next_attention']['mode']='adaptive'
        call['call_id']='rest-fixed'
        good = await self.f.role.callback(call)
        self.assertTrue(good['ok'])
        self.assertEqual(good['note_ref'],(await self.f.role.callback(call))['note_ref'])

    def test_B01_disable_and_policy_bounds(self):
        self.f.cfg.continuity_enabled = False
        with self.assertRaises(SessionFailure):
            self.f.role.read(self.f.scope,dict(kind='continuity'))
        with self.assertRaises(ValueError):
            ResourcePolicy.parse(dict(continuity_context_target_tokens=1201))
        with self.assertRaises(ValueError):
            ResourcePolicy.parse(dict(autonomous_no_progress_limit=True))

    async def test_C09_partial_unknown_delivery_does_not_archive_or_resend_thought(self):
        first=self.save()
        count=[0]
        async def transport(_action,_params):
            count[0]+=1
            if count[0]==1:
                return dict(retcode=0,data=dict(message_id=1))
            raise TimeoutError('second segment may have been sent')
        self.f.expressions.transport=transport
        proposal=dict(text='first\nsecond',sources=[self.f.event.id],continuity_refs=[first['note_ref']])
        with self.assertRaises(TimeoutError):
            await self.f.role.say(self.f.scope,proposal,self.f.binding,'send')
        self.assertEqual(self.c.notes(self.f.scope)[0]['delivery'],'partial_unknown')
        self.assertEqual(self.c.memory(self.f.scope,first['note_ref']).status,'active')
        from life.role_agent import RoleAgent
        restarted=RoleAgent(self.f.life,self.f.bridge,self.f.cfg,self.f.root,self.f.role.send)
        restarted.continuity_protocol=1
        self.assertEqual(restarted.continuity.notes(self.f.scope)[0]['delivery'],'partial_unknown')
        with self.assertRaises(SessionFailure):
            await restarted.say(self.f.scope,proposal,self.f.binding,'another-call')
        self.assertEqual(count[0],2)

    async def test_batch_call_retry_and_forget_do_not_recover_cached_preview(self):
        batch=self.issued_batch(3)
        call=dict(session_id=self.f.binding['session_id'],root_session_id=self.f.binding['session_id'],caller='role',
            call_id='reflection',kind='life.remember',proposal=self.reflection(batch))
        first=await self.f.role.callback(call)
        self.assertTrue(first['ok'])
        self.assertEqual(first,await self.f.role.callback(call))
        self.f.life.forget(self.f.scope,memory_ids=[first['memory_id']])
        self.assertFalse((await self.f.role.callback(call))['ok'])
        self.assertEqual(self.f.life.repo._one('SELECT receipt FROM life_calls WHERE call_id=\'read-batch\'')['receipt'],'{}')

    def test_reflection_no_insight_cannot_use_thought_protocol(self):
        batch=self.issued_batch(2)
        p=self.reflection(batch,outcome='no_new_insight')
        p.pop('text')
        p['continuity']['type']='thought'
        with self.assertRaises(SessionFailure):
            self.save(p)

    def test_reflection_result_budget_preserves_first_contiguous_ids(self):
        from life.resource_policy import ResourcePolicy,estimate_tokens
        self.f.role.resources=ResourcePolicy.parse(dict(text_result_max_tokens=1500,text_preview_target_tokens=1000))
        for i in range(25):
            self.f.life.ingest_message(self.f.scope,'长材料'*300+str(i))
        batch=self.c.read(self.f.scope,dict(view='reflection_batch',limit=20),self.f.binding)
        self.assertLessEqual(estimate_tokens(batch),1500)
        self.assertTrue(batch['has_more'])
        with self.f.life.repo.tx():
            self.f.life.repo._exec('INSERT INTO life_calls VALUES(?,?,?,?,?)',
                (self.f.binding['session_id'],'budget-batch','digest',canonical(dict(ok=True,**batch)),0))
        p=self.reflection(batch,outcome='no_new_insight')
        p.pop('text')
        self.assertEqual(self.save(p)['through_seq'],batch['items'][-1]['seq'])


if __name__=='__main__':
    unittest.main()
