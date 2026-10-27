"""Concrete ordinary fragments and invalidation of histories that saw them."""
import json
import unittest

from life.life_access import RecallError
from life.memory_worker import OrganizeConfig
from tests.role_fixture import Fixture


class OrdinarySharingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture()
        self.addCleanup(self.f.close)
        self.life,self.repo,self.role=self.f.life,self.f.life.repo,self.f.role
        self.sharing=self.role.memory_worker.sharing
        self.internal=self.life.life_access.bind(self.f.scope,mode='internal')
        self.other=self.life.scope('qq:private:2','2')

    def proposal(self,text,selected=None,**payload):
        event,_=self.life.ingest_message(self.f.scope,text)
        if payload:
            event.payload.update(payload)
            self.repo._exec('UPDATE events SET payload=? WHERE id=?',(json.dumps(event.payload),event.id))
            self.repo.project_life('event',event.id)
        resource,body=self.life.life_access.load(self.internal,event.id)
        data=body.encode('utf-8')
        selected=(selected or text).encode('utf-8')
        start=data.index(selected,len(body.split('\n',1)[0].encode('utf-8'))+1)
        proposal=dict(source_ref=resource['resource_id'],classification='ordinary',
            category='recommendation',uncertain=False,spans=[[start,start+len(selected)]])
        return event,resource,proposal

    def publish(self,resource,proposal):
        return self.sharing.prepare(self.internal,proposal,{resource['resource_id']:resource['source_version']})

    async def test_mixed_message_shares_independent_recommendation_and_not_private_ancestor(self):
        event,resource,proposal=self.proposal('推荐电影小森林。我的密码是 secret。','推荐电影小森林。')
        ref=self.publish(resource,proposal)
        ctx=self.life.life_access.bind(self.other)
        _,body=self.life.life_access.load(ctx,ref)
        self.assertIn('推荐电影小森林。',body)
        self.assertNotIn('secret',body)
        with self.assertRaises(RecallError):
            self.life.life_access.load(ctx,event.id)
        with self.assertRaises(RecallError):
            self.life.life_access.load(self.internal,event.id)  # old policy binding

    async def test_cannot_strip_confidentiality_negation_or_unknown_classification(self):
        for text,selected in [('别告诉别人我推荐电影小森林。','推荐电影小森林。'),
                              ('不推荐电影小森林。','推荐电影小森林。'),
                              ('这件事保密。推荐电影小森林。','推荐电影小森林。'),
                              ('我的手机号是13812345678。',None)]:
            with self.subTest(text=text):
                _,resource,proposal=self.proposal(text,selected)
                with self.assertRaises(RecallError):
                    self.publish(resource,proposal)
        _,resource,proposal=self.proposal('推荐电影小森林。')
        proposal['uncertain']=True
        with self.assertRaises(RecallError):
            self.publish(resource,proposal)

    async def test_payload_restriction_changes_source_version_and_blocks_stale_proposal(self):
        event,resource,proposal=self.proposal('推荐电影小森林。')
        event.payload['confidential']=True
        with self.repo.tx():
            self.repo._exec('UPDATE events SET payload=? WHERE id=?',(json.dumps(event.payload),event.id))
            self.repo.project_life('event',event.id)
        with self.assertRaises(RecallError):
            self.publish(resource,proposal)

    async def test_forget_retires_audience_and_erases_receipts_before_cancelling_dsh(self):
        event,resource,proposal=self.proposal('推荐电影小森林。')
        ref=self.publish(resource,proposal)
        binding=self.role.sessions.binding(self.other)
        ctx=self.life.life_access.bind(self.other,session_id=binding['session_id'],generation=binding['generation'])
        self.life.life_access.load(ctx,ref)
        with self.repo.tx():
            self.repo._exec('INSERT INTO life_calls VALUES(?,?,?,?,?)',(binding['session_id'],'old','digest','{"text":"cached"}',1))
        self.repo.redact_events([event.id])
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(binding['session_id'],))['status'],'retired')
        self.assertEqual(self.repo._one('SELECT receipt FROM life_calls WHERE session_id=?',(binding['session_id'],))['receipt'],'{}')
        with self.assertRaises(RecallError):
            self.life.life_access.load(ctx,ref)
        cancelled=[]
        self.f.bridge.session_control=lambda *args: cancelled.append(args) or dict(state='stopped')
        self.role.memory_worker.config=OrganizeConfig(enabled=False)
        await self.role.memory_worker.tick()
        self.assertIn((binding['session_id'],binding['generation']),cancelled)
        self.assertFalse(self.repo._one('SELECT 1 FROM meta WHERE k=?',('life_retire_reader:'+binding['session_id'],)))

    async def test_policy_deny_also_retires_prior_reader(self):
        _,resource,proposal=self.proposal('推荐电影小森林。')
        ref=self.publish(resource,proposal)
        binding=self.role.sessions.binding(self.other)
        ctx=self.life.life_access.bind(self.other,session_id=binding['session_id'],generation=binding['generation'])
        self.life.life_access.load(ctx,ref)
        self.life.life_access.set_rule(resource['resource_id'],'disclose','*','deny')
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(binding['session_id'],))['status'],'retired')

    async def test_automatic_context_tracks_shared_preview_and_correction_retires_it(self):
        event,resource,proposal=self.proposal('推荐电影小森林。')
        self.publish(resource,proposal)
        binding=self.role.sessions.binding(self.other)
        self.life.ingest_message(self.other,'小森林电影推荐')
        context=self.role.context(binding,self.other,dict(caller='role'))
        self.assertIn('推荐电影小森林',context)
        with self.repo.tx():
            event.payload['text']='后来撤回了推荐。'
            self.repo._exec('UPDATE events SET payload=? WHERE id=?',(json.dumps(event.payload),event.id))
            self.repo.project_life('event',event.id)
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(binding['session_id'],))['status'],'retired')

    async def test_retiring_reader_preserves_and_reoffers_unhandled_original_input(self):
        source,resource,proposal=self.proposal('推荐电影小森林。')
        ref=self.publish(resource,proposal)
        binding=self.role.sessions.binding(self.other)
        event,_=self.life.ingest_message(self.other,'我还有一个问题。')
        self.role.sessions.enqueue(binding,event)
        ctx=self.life.life_access.bind(self.other,session_id=binding['session_id'],generation=binding['generation'])
        self.life.life_access.load(ctx,ref)
        self.repo.redact_events([source.id])
        admitted=[]
        self.f.bridge.session_control=lambda *args:dict(state='stopped')
        self.f.bridge.input_status=lambda *args:dict(state='not_found')
        self.f.bridge.session_input=lambda payload: admitted.append(payload) or dict(state='accepted',message_id='new')
        await self.role.memory_worker.retire_readers()
        self.assertEqual(len(admitted),1)
        self.assertEqual(admitted[0]['input_id'],event.id)
        self.assertEqual(admitted[0]['content'],event.text)
        self.assertNotEqual(admitted[0]['session_id'],binding['session_id'])

    async def test_tool_sources_need_explicit_public_provenance(self):
        event,_=self.life.ingest(self.f.scope,'observation','tool_observation','公开电影资料。')
        resource,body=self.life.life_access.load(self.internal,event.id)
        start=len(body.split('\n',1)[0].encode('utf-8'))+1
        proposal=dict(source_ref=resource['resource_id'],classification='ordinary',category='public_finding',
            uncertain=False,spans=[[start,len(body.encode('utf-8'))]])
        with self.assertRaises(RecallError):
            self.publish(resource,proposal)

    async def test_exact_chinese_quotes_derive_spans_and_retain_attribution(self):
        _,resource,proposal=self.proposal('推荐电影小森林。我的密码是 secret。','推荐电影小森林。')
        proposal.pop('spans');proposal['quotes']=['推荐电影小森林。']
        ref=self.publish(resource,proposal)
        _,body=self.life.life_access.load(self.life.life_access.bind(self.other),ref)
        self.assertIn('推荐电影小森林。',body)
        self.assertNotIn('secret',body)

    async def test_quotes_reject_ambiguity_mixed_offsets_and_removed_negation(self):
        for text,quote in [('同一句。同一句。','同一句。'),('不推荐电影小森林。','推荐电影小森林。'),
                           ('这件事保密。推荐电影小森林。','推荐电影小森林。')]:
            _,resource,proposal=self.proposal(text)
            proposal.pop('spans');proposal['quotes']=[quote]
            with self.assertRaises(RecallError):self.publish(resource,proposal)
        _,resource,proposal=self.proposal('普通句子。')
        proposal['quotes']=['普通句子。']
        with self.assertRaises(RecallError):self.publish(resource,proposal)

    async def test_confirmed_self_expression_can_be_recalled_in_other_audience(self):
        async def confirmed(action,params):
            return dict(status='ok',retcode=0,data=dict(message_id=789))
        self.f.expressions.transport=confirmed
        # Exercise the actual delivery writer, not a self-declared sent flag.
        group=self.life.scope('qq:group:9','')
        await self.f.role.send(group,'shared-self-test',dict(text='占卜师这活儿我接了，明天开始营业。'),[])
        row=self.repo._one("SELECT id FROM events WHERE kind='delivered_segment' ORDER BY ingest_seq DESC LIMIT 1")
        self.assertIsNotNone(row)
        resource,_=self.life.life_access.load(self.internal,row['id'])
        proposal=dict(source_ref=resource['resource_id'],classification='ordinary',category='everyday_event',
            uncertain=False,quotes=['占卜师这活儿我接了，明天开始营业。'])
        ref=self.publish(resource,proposal)
        context=self.life.life_access.bind(self.other)
        _,body=self.life.life_access.load(context,ref)
        self.assertIn('confirmed_self_expression',body)
        with self.assertRaises(RecallError):self.life.life_access.load(context,row['id'])

    async def test_unconfirmed_self_output_cannot_claim_sent_experience(self):
        event,_=self.life.ingest(self.f.scope,'delivered_segment','self_output','我已经做过了。',payload=dict(confirmed=True,delivery_id='invented'))
        resource,_=self.life.life_access.load(self.internal,event.id)
        proposal=dict(source_ref=resource['resource_id'],classification='ordinary',category='everyday_event',uncertain=False,quotes=['我已经做过了。'])
        with self.assertRaises(RecallError):self.publish(resource,proposal)


if __name__=='__main__':
    unittest.main()
