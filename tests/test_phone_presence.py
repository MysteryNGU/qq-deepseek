"""Phone presence (P21): a locked phone must stay visible, a stream is still required to dispatch.

Runs inside the repository's disposable sandbox (``tests/run_isolated_suite.py``): temporary
HOME, fresh database, no network. Nothing here touches the production home or life.db.

Covers the two defects from ``docs/mobile-sse-diagnosis.md``:

* ``PhoneControl.context()`` used to require a live SSE subscription, so a phone that only
  keeps registering (locked screen, app in background) reported ``connected=false``.
* ``PhoneControl.disconnected()`` used to delete the device record, so a brief lock erased
  the device instead of marking it absent.

plus the pairing-code regeneration that used to be impossible once a code was used.
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from life.companion_mobile import MobileAPI, create_pairing_code, pairing_status  # noqa: E402
from life.companion_phone import RECENT_SECONDS, FORGET_TEST_SECONDS, PhoneControl  # noqa: E402

DEVICE = 'device-under-test'
TOKEN = 'mob_' + 'a' * 43


class _Router:
    def __init__(self):
        self.routes = []

    def add_post(self, path, handler):
        self.routes.append(('POST', path, handler))

    def add_get(self, path, handler):
        self.routes.append(('GET', path, handler))


class _App:
    def __init__(self):
        self.router = _Router()


class _Server:
    def __init__(self, home):
        self.home = str(home)
        self.app = _App()
        self.token = 'server-token-for-tests'


class _Request(dict):
    """Minimal stand-in for aiohttp.web.Request (mapping with .json())."""

    def __init__(self, payload, path='/mobile/device', device=None, headers=None, query=None):
        super().__init__()
        self._payload = payload
        self.path = path
        self.query = dict(query or {})
        self.headers = dict(headers or {})
        self.remote = '127.0.0.1'
        if device is not None:
            self['mobile_device'] = device

    async def json(self):
        return self._payload


def _capabilities(enabled=True, operations=('info', 'screenshot'), **extra):
    phone = dict(enabled=enabled, operations=list(operations), workspaces=[])
    phone.update(extra)
    return dict(capabilities=dict(phone=phone))


class PhonePresenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='p21-phone-')
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.addCleanup(lambda: sys.modules.pop('life.companion_mobile_audio', None))
        self.mobile = MobileAPI(_Server(self.home), state_dir=self.home / 'state')
        self.addCleanup(self.mobile.close)
        self.phone = self.mobile.phone

    # ------------------------------------------------------------------ A
    def test_registered_phone_counts_online_without_any_stream(self):
        """A: registration alone is enough for `connected`, before any SSE exists."""
        self.assertFalse(self.phone.context()['connected'])
        asyncio.run(self.phone.register(
            _Request(_capabilities(), device=DEVICE,
                     headers={'Authorization': 'Bearer ' + TOKEN})))
        context = self.phone.context()
        self.assertTrue(context['connected'])
        self.assertEqual([row['device_id'] for row in context['devices']], [DEVICE])
        self.assertFalse(context['devices'][0]['has_stream'])

    def test_registration_window_is_ninety_seconds(self):
        """A: inside RECENT_SECONDS the device is online; past it, it is not."""
        asyncio.run(self.phone.register(_Request(_capabilities(), device=DEVICE)))
        now = time.time()
        self.assertIn(DEVICE, self.phone.online_devices(now + RECENT_SECONDS - 1))
        self.assertNotIn(DEVICE, self.phone.online_devices(now + RECENT_SECONDS + 1))
        self.assertIn(DEVICE, self.phone.online_devices(now + 89.5))
        self.assertNotIn(DEVICE, self.phone.online_devices(now + 90.5))

    def test_recent_registration_survives_stream_loss_exactly_as_required(self):
        """A: register → SSE opens → SSE drops; connected must stay true within 90 s."""
        asyncio.run(self.phone.register(_Request(_capabilities(), device=DEVICE)))
        queue = asyncio.Queue(maxsize=8)
        self.mobile.subscribers[queue] = DEVICE
        self.assertTrue(self.phone.context()['connected'])

        self.mobile.subscribers.pop(queue, None)
        self.phone.disconnected(queue, DEVICE)
        self.assertTrue(self.phone.context()['connected'],
                        '断流后 90 秒内必须仍然显示已连接')

        # 91 秒后不再注册：既没有流，也超出了注册窗口。
        self.phone.devices[DEVICE]['last_seen'] = time.time() - (RECENT_SECONDS + 1)
        self.assertFalse(self.phone.context()['connected'])

    def test_enabled_and_operations_still_gate_the_row(self):
        """A: widening presence must not drop the enabled / operations conditions."""
        asyncio.run(self.phone.register(_Request(_capabilities(enabled=False), device=DEVICE)))
        self.assertFalse(self.phone.context()['connected'])
        asyncio.run(self.phone.register(_Request(_capabilities(enabled=True), device=DEVICE)))
        self.assertTrue(self.phone.context()['connected'])
        self.phone.devices[DEVICE]['operations'] = []
        self.assertFalse(self.phone.context()['connected'])

    def test_missing_or_bogus_last_seen_is_never_online(self):
        """A: the old record shape (no last_seen) must not read as online."""
        self.phone.devices[DEVICE] = dict(enabled=True, operations=['info'])
        self.assertFalse(self.phone.context()['connected'])
        self.phone.devices[DEVICE]['last_seen'] = 'yesterday'
        self.assertFalse(self.phone.context()['connected'])
        self.phone.devices[DEVICE]['last_seen'] = True
        self.assertFalse(self.phone.context()['connected'])

    def test_dispatch_still_needs_a_live_stream(self):
        """A (guard): a recently registered phone with no stream must not receive actions."""
        async def scenario():
            await self.phone.register(_Request(_capabilities(), device=DEVICE))
            result = await self.phone.run(dict(op='info', arguments='{}'))
            return result
        result = asyncio.run(scenario())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'phone_unavailable')
        self.assertIn('活动连接', result['output'])

    def test_dispatch_reaches_the_live_stream(self):
        """A (guard): with a stream the action is queued and completes on a receipt."""
        async def scenario():
            await self.phone.register(_Request(_capabilities(), device=DEVICE))
            queue = asyncio.Queue(maxsize=8)
            self.mobile.subscribers[queue] = DEVICE
            task = asyncio.ensure_future(self.phone.run(dict(op='info', arguments='{}')))
            event = await asyncio.wait_for(queue.get(), 2)
            self.assertEqual(event['type'], 'phone_control')
            self.assertEqual(event['op'], 'info')
            receipt = _Request(dict(id=event['id'], result=dict(status='completed', output='ok')),
                               path='/mobile/control-result', device=DEVICE)
            await self.phone.receipt(receipt)
            return await asyncio.wait_for(task, 2)
        result = asyncio.run(scenario())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['device_id'], DEVICE)

    # ------------------------------------------------------------------ B
    def test_disconnect_keeps_the_device_record(self):
        """B: losing the stream must not delete the device."""
        asyncio.run(self.phone.register(_Request(_capabilities(), device=DEVICE)))
        queue = asyncio.Queue(maxsize=8)
        self.mobile.subscribers[queue] = DEVICE
        self.mobile.subscribers.pop(queue, None)
        self.phone.disconnected(queue, DEVICE)
        self.assertIn(DEVICE, self.phone.devices)

    def test_record_is_forgotten_only_after_the_retention_window(self):
        """B: expiry replaces the old immediate delete."""
        asyncio.run(self.phone.register(_Request(_capabilities(), device=DEVICE)))
        self.assertEqual(self.phone.prune(time.time()), [])
        self.assertEqual(self.phone.prune(time.time() + 3600), [])
        self.assertEqual(self.phone.prune(time.time() + 86401), [DEVICE])
        self.assertNotIn(DEVICE, self.phone.devices)

    def test_test_devices_are_forgotten_sooner(self):
        """B: a record named like a test device expires after an hour."""
        self.mobile.devices = {hashlib.sha256(TOKEN.encode()).hexdigest():
                               dict(id='verif-1', name='verification', created_at=time.time())}
        asyncio.run(self.phone.register(_Request(dict(capabilities=dict(phone=dict(
            enabled=True, operations=['info'], workspaces=[]))), device='verif-1')))
        self.assertTrue(self.phone.devices['verif-1']['test'])
        self.assertEqual(self.phone.prune(time.time() + FORGET_TEST_SECONDS - 1), [])
        self.assertEqual(self.phone.prune(time.time() + FORGET_TEST_SECONDS + 1), ['verif-1'])

    def test_registration_keeps_the_record_name(self):
        """B: the name comes from the paired device file once; registration does not erase it."""
        self.mobile.devices = {hashlib.sha256(TOKEN.encode()).hexdigest():
                               dict(id=DEVICE, name='小鱼安卓', created_at=time.time())}
        asyncio.run(self.phone.register(_Request(_capabilities(), device=DEVICE)))
        self.assertEqual(self.phone.devices[DEVICE]['name'], '小鱼安卓')

    # ------------------------------------------------------------------ C
    def test_pairing_state_reports_used_code_as_unusable(self):
        """C: after a code is used there is no code, so pairing must report it."""
        path = self.mobile.pairing_path
        path.write_text(json.dumps(dict(created_at=time.time(), expires_at=time.time() + 3600,
                                        used_at=time.time())), encoding='utf8')
        status = pairing_status(self.home, state_dir=self.home / 'state')
        self.assertTrue(status['configured'])
        self.assertFalse(status['can_pair'])
        self.assertIn('重新生成', status['reason'])

    def test_new_code_is_issued_and_old_one_dies(self):
        """C: regeneration makes pairing possible again without touching paired phones."""
        path = self.mobile.pairing_path
        path.write_text(json.dumps(dict(code='111111', created_at=time.time(),
                                        expires_at=time.time() + 3600, used_at=time.time())),
                        encoding='utf8')
        before = json.loads(self.mobile.devices_path.read_text('utf8')) if self.mobile.devices_path.exists() else {}
        code, written = create_pairing_code(self.home, state_dir=self.home / 'state')
        self.assertEqual(written, path)
        self.assertRegex(code, r'^\d{6}$')
        self.assertNotEqual(code, '111111')
        value = json.loads(path.read_text('utf8'))
        self.assertEqual(value['code'], code)
        self.assertTrue(value['replaces_used'])
        self.assertTrue(value['expires_at'] > time.time())
        self.assertEqual(json.loads(self.mobile.devices_path.read_text('utf8'))
                         if self.mobile.devices_path.exists() else {}, before)
        self.assertTrue(pairing_status(self.home, state_dir=self.home / 'state')['can_pair'])

    def test_regenerated_code_can_pair_a_new_phone(self):
        """C: the new code actually pairs and the old one is refused."""
        path = self.mobile.pairing_path
        path.write_text(json.dumps(dict(created_at=time.time(), expires_at=time.time() + 3600,
                                        used_at=time.time())), encoding='utf8')
        code, _ = create_pairing_code(self.home, state_dir=self.home / 'state')
        paired = asyncio.run(self.mobile.pair(_Request(dict(code=code, device_name='小鱼安卓'),
                                                       path='/mobile/pair')))
        body = json.loads(paired.body.decode('utf8'))
        self.assertTrue(body['token'].startswith('mob_'))
        self.assertEqual(len(self.mobile.devices), 1)
        with self.assertRaises(Exception):
            asyncio.run(self.mobile.pair(_Request(dict(code=code, device_name='小鱼安卓'),
                                                  path='/mobile/pair')))

    def test_pairing_route_is_owner_only(self):
        """C: the pairing surface needs the server token; a phone token must not read it."""
        handler = self.mobile.pairing_state
        phone_request = _Request(None, path='/mobile/pairing',
                                 headers={'Authorization': 'Bearer ' + TOKEN})
        raised = None
        try:
            asyncio.run(self.mobile.authenticate(phone_request, handler))
        except Exception as error:  # noqa: BLE001 - aiohttp HTTPException
            raised = error
        self.assertIsNotNone(raised, '手机 token 不能读配对码')
        self.assertEqual(getattr(raised, 'status', None), 401)

        owner_request = _Request(None, path='/mobile/pairing',
                                 headers={'Authorization': 'Bearer server-token-for-tests'})
        response = asyncio.run(self.mobile.authenticate(owner_request, handler))
        body = json.loads(response.body.decode('utf8'))
        self.assertTrue(body['ok'])
        self.assertIn('can_pair', body['status'])


if __name__ == '__main__':
    unittest.main()
