import unittest

from life.agent_session import SessionFailure
from life.interaction_mode import InteractionMode
from life.tool_contracts import validate
from tests.role_fixture import Fixture


class InteractionModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.role, self.scope = self.f.role, self.f.scope
        self.mode = self.role.interaction_mode

    def tearDown(self):
        self.f.close()

    def tool(self, name='read', sid=None):
        return dict(tool=name, call_id='work', session_id=sid or self.f.binding['session_id'], arguments={})

    async def test_chat_prompt_and_default_tools_change_with_state(self):
        self.role.compact_tool_protocol = self.role.capability_protocol = 1
        call = dict(kind='static_context',root_session_id=self.f.binding['session_id'],
                    session_id=self.f.binding['session_id'])
        chat = await self.role.callback(call)
        self.assertEqual(chat['default_tools'], [])
        self.assertIn('当前是日常状态', chat['stable_text'])
        self.assertNotIn('接到工作请求按当前 acknowledge_before_work', chat['stable_text'])
        self.assertIn('missing_you', chat['stable_text'])
        stamp = self.role.context_stamp(self.f.binding,self.scope)
        result = self.role.intent(self.scope,dict(action='set_mode',mode='tools'),self.f.binding)
        self.assertIn('当前是工具状态',result['role_protocol'])
        work = await self.role.callback(call)
        self.assertEqual(work['default_tools'],['pwsh','job_output','job_kill'])
        self.assertNotEqual(chat['presentation_scope'],work['presentation_scope'])
        self.assertNotEqual(stamp,self.role.context_stamp(self.f.binding,self.scope))
        self.assertLess(len(work['stable_text']),len(chat['stable_text']))

    def test_external_action_enters_tools_and_turn_completion_returns_to_chat(self):
        self.assertTrue(self.role.before_tool(self.f.binding,self.scope,self.tool())['ok'])
        self.assertEqual(self.mode.state(self.scope)['mode'],'tools')
        with self.f.life.repo.tx():
            self.role.close_turn(self.f.binding['session_id'],'life.say',{},dict(turn_complete=False))
        self.assertEqual(self.mode.state(self.scope)['mode'],'tools')
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE role_operations SET status='succeeded'")
            self.role.close_turn(self.f.binding['session_id'],'life.say',{},dict(turn_complete=True))
        self.assertEqual(self.mode.state(self.scope)['mode'],'chat')

    def test_manual_chat_fence_survives_new_service_and_blocks_all_work_paths(self):
        self.mode.user_select(self.scope,'chat')
        mode = InteractionMode(self.role)
        self.assertEqual(mode.state(self.scope)['policy'],'chat')
        for fn in [lambda: mode.select(self.scope,'tools'),
                   lambda: self.role.before_tool(self.f.binding,self.scope,self.tool()),
                   lambda: self.role.commit_eligibility(self.f.binding,{}),
                   lambda: self.role.intent(self.scope,dict(goal='restart work'),self.f.binding)]:
            with self.assertRaises(SessionFailure) as got: fn()
            self.assertEqual(got.exception.code,'mode_locked')
        self.assertEqual(self.role.read(self.scope,dict(kind='interaction_mode'))['mode'],'chat')
        self.assertIsInstance(self.role.read(self.scope,dict(kind='contact_policy')),dict)
        self.assertTrue(self.role.before_tool(self.f.binding,self.scope,self.tool('life_say'))['ok'])

    def test_manual_tools_persists_and_auto_unlocks(self):
        self.mode.user_select(self.scope,'tools')
        self.mode.finish(self.scope)
        self.assertEqual(self.mode.state(self.scope)['mode'],'tools')
        with self.assertRaises(SessionFailure):self.mode.select(self.scope,'chat')
        self.mode.user_select(self.scope,'chat')
        self.mode.user_select(self.scope,'auto')
        self.mode.select(self.scope,'tools')
        self.mode.finish(self.scope)
        self.assertEqual(self.mode.state(self.scope)['mode'],'chat')

    def test_mode_is_scoped_and_manual_changes_require_owner_private(self):
        self.mode.user_select(self.scope,'chat')
        other = self.f.life.scope('qq:private:2','2')
        self.assertEqual(self.mode.state(other)['policy'],'auto')
        with self.assertRaises(SessionFailure):self.mode.user_select(other,'tools')
        group = self.f.life.scope('qq:group:3','1')
        with self.assertRaises(SessionFailure):self.mode.user_select(group,'tools')

    def test_manual_exit_pauses_tasks_without_claiming_completion(self):
        item = self.role.sessions.enqueue(self.f.binding,self.f.event)
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1 WHERE input_id=?",(item['input_id'],))
        task = self.f.task()
        self.mode.user_select(self.scope,'chat')
        saved = self.role.tasks.get(task['ref'])
        self.assertEqual(saved['status'],'paused')
        self.assertEqual(saved['pause_owner'],'user')
        self.assertEqual(saved['pause_ordinal'],item['ordinal'])
        self.mode.user_select(self.scope,'tools')
        self.assertEqual(self.role.tasks.get(task['ref'])['status'],'paused')

    async def test_child_and_media_consult_callbacks_cannot_bypass_manual_exit(self):
        self.role._delegate_started(dict(parentSessionId=self.f.binding['session_id'],childSessionId='child'))
        self.mode.user_select(self.scope,'chat')
        with self.assertRaises(SessionFailure):
            self.role.before_tool(self.f.binding,self.scope,self.tool(sid='child'))
        self.role.local_media = object()  # Fence must run before any submit method.
        for n,kind in enumerate(('life.media','life.consult','life.change_self')):
            result = await self.role.callback(dict(root_session_id=self.f.binding['session_id'],
                session_id=self.f.binding['session_id'],call_id=f'blocked-{n}',kind=kind,proposal={}))
            self.assertEqual(result.get('code'),'mode_locked',result)

    def test_switch_contracts(self):
        validate('life_intent',dict(action='set_mode',mode='tools'))
        validate('life_read',dict(kind='interaction_mode'))
        with self.assertRaises(SessionFailure):validate('life_intent',dict(action='set_mode',mode='auto'))

    async def test_loading_work_tools_is_blocked_but_does_not_switch_presentation_mid_receipt(self):
        self.role.capability_protocol=1
        self.mode.user_select(self.scope,'chat')
        with self.assertRaises(SessionFailure) as got:
            await self.role.capabilities.read(self.scope,dict(kind='capabilities',category='tools',
                ref='tool:dsh:pwsh',schema_hash='test',load=True),self.f.binding)
        self.assertEqual(got.exception.code,'mode_locked')
        self.assertEqual(self.mode.state(self.scope)['mode'],'chat')

    def test_stop_controls_remain_available_in_manual_chat(self):
        self.f.task()
        self.mode.user_select(self.scope,'chat')
        for name in ('job_kill','job_output','subagent_cancel'):
            self.assertTrue(self.role.before_tool(self.f.binding,self.scope,self.tool(name))['ok'])


if __name__ == '__main__':
    unittest.main()
