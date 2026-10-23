import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from life.agent_session import SessionFailure
from life.runtime import LifeRuntime
from life.self_change import SelfChange,file_hash


class SelfChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'persona.md').write_text('Initial persona',encoding='utf-8')
        self.life = LifeRuntime(self.root/'life.db')
        self.life.persona.activate(self.root/'persona.md')
        self.scope = self.life.scope('qq:private:1','1')
        self.change = SelfChange(self.life,SimpleNamespace(owner=1,persona='persona.md'),self.root)

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    async def test_persona_skill_and_dynamics_are_applied_and_verified(self):
        receipt = await self.change.apply(self.scope,dict(target='persona',content='Updated speaking style'),'persona-edit')
        self.assertEqual(self.life.persona.current().text,'Updated speaking style')
        self.assertTrue(receipt['effective'])
        self.assertFalse(receipt['behavior_validated'])
        skill = dict(target='skill',name='search',content='# Search\nCheck primary evidence.')
        await self.change.apply(self.scope,skill,'skill-edit')
        self.assertEqual((self.root/'skills/search.md').read_text('utf-8'),skill['content'])
        await self.change.apply(self.scope,dict(target='dynamics',patch={'affect':{'half_life':{'hurt':90}}}),'rate-edit')
        self.assertEqual(self.life.config.affect.half_life['hurt'],90)
        SelfChange(self.life,self.change.cfg,self.root).recover()
        self.assertEqual(self.life.config.affect.half_life['hurt'],90)

    async def test_stale_patch_and_invalid_dynamics_do_not_change_live_file(self):
        before = file_hash(self.root/'persona.md')
        with self.assertRaises(SessionFailure):
            await self.change.apply(self.scope,dict(content='wrong',base_hash='stale'),'stale-edit')
        self.assertEqual(file_hash(self.root/'persona.md'),before)
        with self.assertRaises(SessionFailure):
            await self.change.apply(self.scope,dict(files=[dict(path='life-dynamics.json',content='{"affect":{"half_life":{"hurt":0}}}')]),'invalid-rate')
        self.assertFalse((self.root/'life-dynamics.json').exists())

    async def test_invalid_target_or_missing_content_returns_typed_error_without_edit(self):
        for i,proposal in enumerate((dict(target='config',patch={}),dict(target='persona'),
                                     dict(target='skill',name='missing-content'),dict(target='code'))):
            with self.subTest(proposal=proposal):
                with self.assertRaises(SessionFailure) as error:
                    await self.change.apply(self.scope,proposal,'invalid-'+str(i))
                self.assertEqual(error.exception.code,'invalid_input')
        self.assertEqual((self.root/'persona.md').read_text('utf-8'),'Initial persona')
        self.assertFalse(self.life.repo._one('SELECT 1 FROM self_changes'))

    async def test_rejected_code_receipt_explicitly_says_runtime_is_unchanged(self):
        source = 'def calculate(value):\n    return value - 1\n'
        path = self.root/'worker.py'
        path.write_text(source,encoding='utf-8')
        proposal = dict(target='code',patch=dict(file='worker.py',old='value - 1',new='value - 2'),
            probes=[dict(module='worker',function='calculate',arguments={'value':0},expected=1)])
        receipt = await self.change.apply(self.scope,proposal,'rejected-code')
        self.assertEqual(receipt['phase'],'rejected')
        self.assertFalse(receipt['applied'])
        self.assertEqual(receipt['activation_blocked']['code'],'candidate_not_publishable')
        self.assertEqual(path.read_text('utf-8'),source)
        self.assertEqual(await self.change.apply(self.scope,proposal,'rejected-code'),receipt)

    async def test_dsh_queue_is_durable_idempotent_and_interrupted_check_is_unknown(self):
        request = dict(target='dsh',edits=[dict(path='packages/demo/src/index.ts',old='before',new='after')],
                       verification_goal='the original capability can read a test file')
        receipt = await self.change.apply(self.scope,request,'dsh-1')
        self.assertEqual(receipt['phase'],'dsh_queued')
        self.assertFalse(receipt['applied'])
        self.assertEqual(await self.change.apply(self.scope,request,'dsh-1'),receipt)
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE self_changes SET phase='dsh_checking' WHERE id='dsh-1'")
        self.change.recover()
        self.assertEqual(self.change.history(self.scope)[0]['phase'],'outcome_unknown')

    async def test_new_input_while_waiting_for_change_lock_prevents_activation(self):
        import asyncio
        from life.expression import expression_guard
        await self.change.lock.acquire()
        blocked = False
        def eligibility():
            if blocked:
                raise SessionFailure('input_pending','new input')
        token = expression_guard.set(eligibility)
        task = asyncio.create_task(self.change.apply(self.scope,dict(content='stale persona'),'stale-waiter'))
        await asyncio.sleep(0)
        blocked = True
        self.change.lock.release()
        try:
            with self.assertRaises(SessionFailure):
                await task
            self.assertEqual((self.root/'persona.md').read_text('utf-8'),'Initial persona')
        finally:
            expression_guard.reset(token)


if __name__=='__main__':
    unittest.main()
