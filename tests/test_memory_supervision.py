import json
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from life.supervisor import HostSupervisor


class MemorySupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.host=HostSupervisor(SimpleNamespace(control=root),root/'config.toml')
        self.host.started=time.monotonic()-300
        self.host.token='isolated-test-token'
        self.host.tree=SimpleNamespace(process=SimpleNamespace(poll=lambda:None),owns_pid=lambda pid:pid==123)
        self.value=dict(token=self.host.token,pid=123,at=1000,cognition_ready=True,channel_connected=True,completed_turns=3)
        self.host.health_path.write_text(json.dumps(self.value),'utf-8')

    def test_transient_windows_file_lock_uses_only_authenticated_fresh_heartbeat(self):
        with patch('life.supervisor.time.time',return_value=1000):self.assertEqual(self.host.health(),'observed')
        with patch.object(Path,'read_text',side_effect=PermissionError('fake Windows sharing lock')):
            with patch('life.supervisor.time.time',return_value=1001):self.assertEqual(self.host.health(),'observed')
            with patch('life.supervisor.time.time',return_value=1021):self.assertEqual(self.host.health(),'failed')

    def test_unreadable_health_without_prior_authentication_still_fails(self):
        with patch.object(Path,'read_text',side_effect=PermissionError('fake sharing lock')):
            self.assertEqual(self.host.health(),'failed')

    def test_cached_heartbeat_does_not_override_wrong_token(self):
        with patch('life.supervisor.time.time',return_value=1000):self.assertEqual(self.host.health(),'observed')
        self.host.health_path.write_text(json.dumps(dict(self.value,token='wrong')),'utf-8')
        with patch('life.supervisor.time.time',return_value=1001):self.assertEqual(self.host.health(),'failed')

    def test_cached_heartbeat_still_requires_ownership_and_live_process(self):
        with patch('life.supervisor.time.time',return_value=1000):self.assertEqual(self.host.health(),'observed')
        self.host.tree.owns_pid=lambda pid:False
        with patch.object(Path,'read_text',side_effect=PermissionError('fake sharing lock')):
            with patch('life.supervisor.time.time',return_value=1001):self.assertEqual(self.host.health(),'failed')

    def test_login_after_long_offline_gets_bounded_cognition_initialization_time(self):
        self.host.started=100
        self.host.health_path.write_text(json.dumps(dict(self.value,channel_connected=False,cognition_ready=False)),'utf-8')
        with patch('life.supervisor.time.time',return_value=1000),patch('life.supervisor.time.monotonic',return_value=500):
            self.assertEqual(self.host.health(),'channel_offline')
        self.host.health_path.write_text(json.dumps(dict(self.value,cognition_ready=False)),'utf-8')
        with patch('life.supervisor.time.time',return_value=1000),patch('life.supervisor.time.monotonic',return_value=501):
            self.assertEqual(self.host.health(),'starting')
        with patch('life.supervisor.time.time',return_value=1000),patch('life.supervisor.time.monotonic',return_value=562):
            self.assertEqual(self.host.health(),'failed')


class NotificationFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_replay_yields_between_durable_events(self):
        from tests.role_fixture import Fixture
        fixture=Fixture();self.addCleanup(fixture.close)
        role=fixture.role;sid=fixture.binding['session_id'];applied=[]
        original=role.sessions.apply_event
        def apply(session,event):
            original(session,event);applied.append(event['seq'])
        role.sessions.apply_event=apply
        fixture.bridge.session_events=lambda *a,**kw:dict(has_more=False,events=[
            dict(seq=i,type='step/start',data=dict(turn=1,step=i+1)) for i in range(50)])
        async def observe():
            while not applied:await asyncio.sleep(0)
            return len(applied)
        observer=asyncio.create_task(observe())
        try:
            await role.sessions.replay(sid)
            self.assertLess(await observer,50,'replay must allow heartbeat work before the page is exhausted')
        finally:
            observer.cancel();await asyncio.gather(observer,return_exceptions=True)

    async def test_notification_backlog_yields_for_heartbeat_and_lease_tasks(self):
        from tests.role_fixture import Fixture
        fixture=Fixture();self.addCleanup(fixture.close)
        role=fixture.role;role.running=True
        for _ in range(100):role.queue.put_nowait((dict(method='irrelevant.notification'),1))
        consumer=asyncio.create_task(role.consume())
        try:
            await asyncio.sleep(0)
            self.assertGreater(role.queue.qsize(),0,'a ready queue must not monopolize the event loop')
            await role.queue.join()
        finally:
            role.running=False;consumer.cancel()
            await asyncio.gather(consumer,return_exceptions=True)


if __name__=='__main__':unittest.main()
