# -*- coding: utf-8 -*-
"""P31 server-side change: same-id retry, four boundaries.

Real sqlite + the real `MobileAPI.chat()`. Only `core.chat` and `spawn` are stubs,
so what gets verified is `chat()`'s dedupe/rerun branching itself.

Cases:
1. same id + same content + failed      -> rerun (retried=true), ends completed
2. same id + same content + completed   -> no rerun (duplicate=true)
3. same id + different content          -> 409 (unchanged behaviour)
4. same id + accepted                   -> no rerun (anti double-run; deliberate, see chat())

Run: python -B -X utf8 -m unittest tests.test_mobile_retry -v
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RID = '11111111-2222-4333-8444-555555555555'


def _digest(text, images):
    return hashlib.sha256(json.dumps(dict(text=text, images=images), sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class _Request:
    """Smallest aiohttp request shell: only what chat() reads."""

    def __init__(self, payload, device='dev-1'):
        self._payload = payload
        self.query = {}
        self._device = device

    async def json(self):
        return self._payload

    def __getitem__(self, key):
        if key == 'mobile_device':
            return self._device
        raise KeyError(key)


class _FakeCore:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0
        self.supports_message_routing = True

    async def chat(self, text, images=None, *, source=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError('model down')


class _FakeServer:
    def __init__(self, core):
        self.core = core
        self.spawned = []

    def spawn(self, coro):
        self.spawned.append(coro)
        return coro


def _make_api(db_path, core):
    from life import companion_mobile as mobile
    api = mobile.MobileAPI.__new__(mobile.MobileAPI)
    api.db = sqlite3.connect(str(db_path))
    api.db.execute('CREATE TABLE IF NOT EXISTS requests(device TEXT,id TEXT,digest TEXT,status TEXT,'
                   'created REAL,updated REAL,PRIMARY KEY(device,id))')
    api.db.commit()
    api.server = _FakeServer(core)
    api.devices = {}
    api.emit = lambda event: None
    api.device_source = lambda device: dict(kind='mobile', device_id=device, name='test')
    return api


class RetryContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mobile-retry-')
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'mobile.sqlite3'

    def _api(self, core=None):
        core = core or _FakeCore()
        api = _make_api(self.db_path, core)
        self.addCleanup(api.db.close)
        return api, core

    def _status(self, api, identity=RID):
        return api.db.execute('SELECT status FROM requests WHERE device=? AND id=?',
                              ('dev-1', identity)).fetchone()[0]

    def _seed(self, api, status, text='hello', identity=RID):
        api.db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',
                       ('dev-1', identity, _digest(text, []), status, 1.0, 1.0))
        api.db.commit()

    def _post(self, api, text='hello', identity=RID):
        import asyncio
        return asyncio.run(api.chat(_Request(dict(id=identity, text=text, images=[]))))

    def test_1_failed_same_id_reruns_and_completes(self):
        api, core = self._api()
        self._seed(api, 'failed')
        response = self._post(api)
        self.assertEqual(response.status, 202)
        body = json.loads(response.body)
        self.assertTrue(body.get('retried'), 'failed must trigger rerun: ' + str(body))
        self.assertEqual(body.get('previous'), 'failed')
        self.assertEqual(len(api.server.spawned), 1, 'must actually dispatch again')
        self.assertEqual(self._status(api), 'accepted', 'after rerun it goes back to accepted')
        import asyncio
        asyncio.run(api.server.spawned[0])
        self.assertEqual(self._status(api), 'completed', 'rerun must end completed')

    def test_2_completed_same_id_does_not_rerun(self):
        api, core = self._api()
        self._seed(api, 'completed')
        body = json.loads(self._post(api).body)
        self.assertTrue(body.get('duplicate'), 'completed must not rerun: ' + str(body))
        self.assertEqual(body.get('status'), 'completed')
        self.assertEqual(len(api.server.spawned), 0, 'must not dispatch twice')
        self.assertEqual(core.calls, 0, 'must not call core.chat twice')

    def test_3_different_content_is_409(self):
        api, core = self._api()
        self._seed(api, 'failed', text='original')
        import asyncio
        from aiohttp import web
        with self.assertRaises(web.HTTPConflict):
            asyncio.run(api.chat(_Request(dict(id=RID, text='changed', images=[]))))
        self.assertEqual(len(api.server.spawned), 0, 'content conflict must not dispatch')
        self.assertEqual(self._status(api), 'failed', 'conflict must not touch the row')

    def test_4_accepted_same_id_does_not_rerun(self):
        """accepted may have a live _chat; rerunning would double-run."""
        api, core = self._api()
        self._seed(api, 'accepted')
        body = json.loads(self._post(api).body)
        self.assertTrue(body.get('duplicate'), 'accepted must not rerun: ' + str(body))
        self.assertEqual(len(api.server.spawned), 0, 'must not double-run')

    def test_5_restart_sweeps_accepted_to_interrupted_so_retry_works(self):
        """Closed loop: crash before finally -> stuck accepted -> swept to interrupted -> retryable."""
        api, core = self._api()
        self._seed(api, 'accepted')
        api.db.execute("UPDATE requests SET status='interrupted',updated=? WHERE status='accepted'",
                       (2.0,))
        api.db.commit()
        self.assertEqual(self._status(api), 'interrupted')
        body = json.loads(self._post(api).body)
        self.assertTrue(body.get('retried'), 'after sweep it must be retryable: ' + str(body))

    def test_6_rerun_that_fails_again_stays_retryable(self):
        """Rerun fails again -> back to failed, user can retry again (never stuck)."""
        api, core = self._api(_FakeCore(fail=True))
        self._seed(api, 'failed')
        body = json.loads(self._post(api).body)
        self.assertTrue(body.get('retried'))
        import asyncio
        asyncio.run(api.server.spawned[0])
        self.assertEqual(self._status(api), 'failed', 'failing again must return to failed')


if __name__ == '__main__':
    unittest.main()
