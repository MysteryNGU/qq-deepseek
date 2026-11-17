"""Long interactive work must report real progress without manufacturing sends."""
import unittest

from life.agent_session import SessionFailure,canonical
from life.work_progress import require,state
from tests.role_fixture import Fixture


class WorkProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role=self.f.role;self.sid=self.f.binding['session_id'];self.seq=0
        self.at=self.f.event.received_at+1;self.f.life.clock=lambda:self.at
        self.f.life.delivery.clock=lambda:self.at
        self.role.interaction_mode.select(self.f.scope,'tools')
        self.event('turn/start',dict(turn=6));self.event('step/start',dict(turn=6,step=1))
        self.role.sessions.enqueue(self.f.binding,self.f.event)
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1,claimed_turn=6,claimed_step=1 WHERE input_id=?",(self.f.event.id,))

    def event(self,kind,data):
        self.role.sessions.apply_event(self.sid,dict(seq=self.seq,type=kind,data=data,time=int(self.at*1000)))
        self.seq+=1

    def operation(self,name='pwsh',args=None,text='found material',error=False):
        self.at+=1;call_id='call-'+str(self.seq);args=args or dict(command='read diary')
        self.event('tool/call',dict(turn=6,step=1,callId=call_id,name=name,arguments=canonical(args)))
        self.event('tool/result',dict(turn=6,step=1,message=dict(toolCallId=call_id,isError=error,content=[dict(type='text',text=text)])))

    def current(self):return state(self.role,self.f.scope,self.f.binding)

    async def test_six_calls_require_a_report_and_actual_delivery_releases_it(self):
        for i in range(6):self.operation(text='result '+str(i))
        self.assertTrue(self.current()['due'])
        admission=self.role.work_admission(self.f.scope,self.f.binding)
        self.assertEqual(admission['next_tool'],'life_say');self.assertFalse(admission['ready'])
        with self.assertRaises(SessionFailure) as error:require(self.role,self.f.scope,self.f.binding,'pwsh',{})
        self.assertEqual(error.exception.code,'progress_required')
        require(self.role,self.f.scope,self.f.binding,'job_kill',{})
        self.assertFalse(self.f.sent)
        async def send(action,params):return dict(retcode=0,data=dict(message_id=123))
        self.f.expressions.transport=send
        # A model-written statement is sent through the real expression ledger.
        await self.f.expressions(self.f.scope,'progress',dict(text='I found the diary folder, but yesterday is missing.'),[self.f.event.id])
        self.assertFalse(self.current()['due'])

    async def test_unknown_delivery_does_not_satisfy_progress(self):
        for i in range(6):self.operation(text=str(i))
        with self.assertRaises(TimeoutError):
            await self.f.expressions(self.f.scope,'unknown',dict(text='I found the folder.'),[self.f.event.id])
        self.assertTrue(self.current()['due'])

    def test_elapsed_time_is_checked_only_at_a_work_boundary(self):
        self.operation();self.at+=95
        self.assertTrue(self.current()['due'])
        self.assertEqual(self.f.sent,[])
        self.role.interaction_mode.select(self.f.scope,'chat')
        self.assertFalse(self.current()['due'])

    def test_repeated_operation_is_not_reenabled_by_a_different_call_id(self):
        for _ in range(3):self.operation(text='no such file')
        self.assertIsNotNone(self.current()['repeat'])
        with self.assertRaises(SessionFailure) as error:
            require(self.role,self.f.scope,self.f.binding,'pwsh',dict(command='read diary'))
        self.assertEqual(error.exception.code,'work_repeated')
        require(self.role,self.f.scope,self.f.binding,'pwsh',dict(command='inspect known archive index'))

    async def test_life_reads_and_native_executor_both_enforce_the_boundary(self):
        for i in range(6):self.operation(text=str(i))
        result=await self.role.callback(dict(kind='life.read',proposal=dict(kind='self'),call_id='read',
            root_session_id=self.sid,session_id=self.sid))
        self.assertEqual(result['code'],'progress_required')
        with self.assertRaises(SessionFailure) as error:
            self.role.before_tool(self.f.binding,self.f.scope,dict(tool='pwsh',call_id='native',session_id=self.sid,arguments={}))
        self.assertEqual(error.exception.code,'progress_required')

    def test_new_user_input_and_new_turn_are_not_mistaken_for_old_repeats(self):
        for _ in range(3):self.operation(text='no such file')
        self.at+=1
        new,_=self.f.life.ingest_message(self.f.scope,'Try that exact read again now')
        self.role.sessions.enqueue(self.f.binding,new)
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1,claimed_turn=6 WHERE input_id=?",(new.id,))
        require(self.role,self.f.scope,self.f.binding,'pwsh',dict(command='read diary'))
        self.event('turn/start',dict(turn=7));self.event('step/start',dict(turn=7,step=1))
        self.assertFalse(self.current()['due']);self.assertIsNone(self.current()['repeat'])


if __name__=='__main__':unittest.main()
