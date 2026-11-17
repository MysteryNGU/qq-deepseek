"""phone_link: owner diagnostics for the phone link (read-only, no dispatch).

Runs in the isolation sandbox: temporary HOME, fresh database, no network.
"""
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from life.companion_phone import RECENT_SECONDS, PhoneControl  # noqa: E402
from life.companion_phone_view import phone_link  # noqa: E402


def _mobile(devices=None, subscribers=None):
    control = PhoneControl.__new__(PhoneControl)
    control.devices = dict(devices or {})
    control.pending = {}
    control.mobile = SimpleNamespace(subscribers=dict(subscribers or {}))
    return SimpleNamespace(phone=control, subscribers=control.mobile.subscribers)


class PhoneLinkTests(unittest.TestCase):
    def test_missing_phone_control_is_reported_not_raised(self):
        link = phone_link(None)
        self.assertFalse(link['available'])
        self.assertFalse(link['connected'])
        self.assertEqual(link['devices'], [])

    def test_fresh_registration_is_online_and_counted(self):
        now = time.time()
        mobile = _mobile({'dev': dict(enabled=True, operations=['info'], last_seen=now - 10)})
        link = phone_link(mobile)
        self.assertTrue(link['connected'])
        row = link['devices'][0]
        self.assertTrue(row['online'])
        self.assertEqual(row['operations'], 1)
        self.assertFalse(row['has_stream'])
        self.assertEqual(link['gap'], '')

    def test_sleeping_phone_reports_age_and_gap(self):
        now = time.time()
        mobile = _mobile({'dev': dict(enabled=True, operations=['info', 'screenshot'],
                                      last_seen=now - 300)})
        link = phone_link(mobile)
        self.assertFalse(link['connected'])
        row = link['devices'][0]
        self.assertGreaterEqual(row['last_seen_seconds'], 295)
        self.assertFalse(row['online'])
        self.assertGreater(row['forgets_in_seconds'], 0)
        self.assertIn(str(RECENT_SECONDS), link['gap'])
        self.assertTrue(link['gap'])

    def test_stream_shows_up_without_changing_online_decision(self):
        now = time.time()
        queue = object()
        mobile = _mobile({'dev': dict(enabled=True, operations=['info'], last_seen=now - 5)},
                         subscribers={queue: 'dev'})
        link = phone_link(mobile)
        row = link['devices'][0]
        self.assertTrue(row['has_stream'])
        self.assertEqual(link['streams'], 1)
        self.assertTrue(row['online'])

    def test_disabled_or_no_operations_is_reported_as_gap(self):
        now = time.time()
        mobile = _mobile({'dev': dict(enabled=False, operations=['info'], last_seen=now - 5)})
        link = phone_link(mobile)
        self.assertFalse(link['connected'])
        self.assertIn('无障碍', link['gap'])

    def test_no_payload_contains_a_token_or_hash(self):
        now = time.time()
        mobile = _mobile({'dev': dict(enabled=True, operations=['info'], last_seen=now - 5,
                                      name='小鱼安卓', token='mob_secret')})
        text = repr(phone_link(mobile))
        self.assertNotIn('mob_secret', text)
        self.assertNotIn('token', text)

    def test_accepts_server_wrapper(self):
        now = time.time()
        mobile = _mobile({'dev': dict(enabled=True, operations=['info'], last_seen=now - 5)})
        server = SimpleNamespace(mobile=mobile)
        self.assertTrue(phone_link(server)['connected'])


if __name__ == '__main__':
    unittest.main()
