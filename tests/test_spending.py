"""No paid requests: durable admission, unknown bills and calendar boundaries."""
from datetime import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from life.agent_session import SessionFailure,canonical
from life.runtime import LifeRuntime
from life.spending import Spending,SpendingPolicy,reported_cost,UNIT,ZONE


class SpendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.life=LifeRuntime(Path(self.tmp.name)/'life.db')
        self.repo=self.life.repo
        self.repo._exec('CREATE TABLE usage_calls(call_key TEXT PRIMARY KEY,payload TEXT)')
        self.repo._exec('CREATE TABLE agent_inputs(session_id TEXT,input_id TEXT,claimed_turn INTEGER)')
        self.role=SimpleNamespace(repo=self.repo,cfg=SimpleNamespace(spending_policy=dict(enabled=True)),turn_number=lambda sid:None)
        self.money=Spending(self.role)
        self.root=dict(session_id='root')

    def tearDown(self):self.life.close();self.tmp.cleanup()

    def request(self,key='call',sid='root'):
        return dict(session_id=sid,record=dict(call_key=key,provider='deepseek-official',model='deepseek-flash',max_tokens=16384))

    def settle(self,request,usage=None):
        value=dict(request,record=dict(request['record'],usage=usage))
        return self.money.settle(value)

    def test_concurrent_reservations_share_limit_and_survive_reopen(self):
        for i in range(4):self.money.reserve(self.root,self.request(str(i),'child' if i else 'root'))
        with self.assertRaisesRegex(SessionFailure,'余额'):self.money.reserve(self.root,self.request('fifth'))
        self.money=Spending(self.role)
        self.assertEqual(self.money.snapshot()['unknown_calls'],4)
        self.assertLessEqual(self.money.snapshot()['reserved_nano'],10*UNIT)

    def test_settlement_releases_only_unused_reservation_and_is_idempotent(self):
        q=self.request();self.money.reserve(self.root,q)
        usage=dict(inputTokens=100,cacheReadTokens=1000,outputTokens=50,reasoningTokens=40)
        self.settle(q,usage);first=self.money.snapshot()
        self.settle(q,dict(inputTokens=0,cacheReadTokens=0,outputTokens=0))
        self.assertEqual(first,self.money.snapshot())
        self.assertEqual(first['reserved_nano'],0)
        self.assertGreater(first['charged_nano'],0)
        with self.assertRaises(SessionFailure):self.money.reserve(self.root,q)

    def test_missing_usage_survives_midnight_and_restart(self):
        t=datetime(2026,10,24,23,59,tzinfo=ZONE).timestamp()
        with patch('life.spending.time.time',return_value=t):self.money.reserve(self.root,self.request());self.settle(self.request())
        later=self.money.snapshot(t+120)
        self.assertEqual(later['unknown_calls'],1)
        self.assertGreater(later['reserved_nano'],0)

    def test_today_existing_usage_is_counted_without_projection_double_charge(self):
        q=self.request();self.money.reserve(self.root,q)
        self.settle(q,dict(inputTokens=100,cacheReadTokens=0,outputTokens=20))
        first=self.money.snapshot()
        self.repo._exec('INSERT INTO usage_calls VALUES(?,?)',('call',canonical(dict(call_key='call',started_at=__import__('time').time()))))
        self.assertEqual(first,self.money.snapshot())

    def test_unknown_route_or_output_cap_never_admitted(self):
        for field,value in [('model','unpriced'),('max_tokens',None),('max_tokens',True)]:
            q=self.request();q['record'][field]=value
            with self.assertRaises(SessionFailure):self.money.reserve(self.root,q)
        self.assertFalse(self.repo._all('SELECT * FROM spending_calls'))

    def test_lost_settlement_recovered_from_exact_durable_usage(self):
        q=self.request();self.money.reserve(self.root,q)
        self.settle(q)
        import time
        value=dict(call_key='call',provider='deepseek-official',model='deepseek-flash',started_at=time.time(),
            finished_at=time.time(),uncached_input=100,cache_read=20,output=10,cache_write=0)
        with self.repo.tx():self.repo._exec('INSERT INTO usage_calls VALUES(?,?)',('call',canonical(value)))
        self.money=Spending(self.role)
        self.assertEqual(self.money.snapshot()['unknown_calls'],0)
        self.assertGreater(self.money.snapshot()['charged_nano'],0)

    def test_goal_defers_background_but_new_user_input_still_admitted(self):
        state=self.money.snapshot()
        with self.repo.tx():
            self.repo._exec('INSERT INTO spending_calls VALUES(?,?,?,?,?,?,?)',('past','root',state['day'],0,5*UNIT,'settled','{}'))
        with self.assertRaisesRegex(SessionFailure,'节省目标'):self.money.reserve(self.root,self.request())
        event,_=self.life.ingest_message(self.life.scope('qq:private:1','1'),'Synthetic user input')
        q=self.request();q['record']['input_refs']=[event.id]
        self.assertTrue(self.money.reserve(self.root,q)['ok'])

    def test_weekend_tariff_and_reasoning_not_double_counted(self):
        t=datetime(2026,10,24,10,tzinfo=ZONE).timestamp()
        r=dict(provider='deepseek-official',model='deepseek-flash',started_at=t,
            usage=dict(inputTokens=1_000_000,cacheReadTokens=1_000_000,outputTokens=1_000_000,reasoningTokens=20))
        self.assertEqual(reported_cost(r)/UNIT,5.02)
        r['started_at']=datetime(2026,10,26,10,tzinfo=ZONE).timestamp()
        self.assertEqual(reported_cost(r)/UNIT,10.04)
        r['started_at']=datetime(2026,10,26,8,59,tzinfo=ZONE).timestamp()
        r['finished_at']=datetime(2026,10,26,12,1,tzinfo=ZONE).timestamp()
        self.assertEqual(reported_cost(r)/UNIT,10.04)

    def test_config_rejects_ambiguous_limits(self):
        for raw in (dict(enabled=1),dict(daily_limit_fen=True),dict(daily_target_fen=1001),dict(unknown=1)):
            with self.assertRaises(ValueError):SpendingPolicy.parse(raw)
