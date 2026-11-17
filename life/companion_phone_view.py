"""Owner-only, read-only view of the phone link.

`PhoneControl.context()` answers "which devices may be used right now". This module answers
"what does the host actually hold" — including devices that exist but fell outside the
registration window, and how long ago each was last seen — so a verification can tell
"phone is asleep" from "phone is broken" without an nginx log.

No token, no device token hash, no message text. It never dispatches an action and never
writes `last_seen`.
"""
import time

from .companion_phone import FORGET_SECONDS, FORGET_TEST_SECONDS, RECENT_SECONDS


def _age(phone, value, now):
    if not hasattr(phone, '_last_seen'):
        return None
    seen = phone._last_seen(value, now)
    return None if not seen else round(now - seen, 1)


def phone_link(mobile):
    """Summarise the live phone link; never raises for a partially built server.

    Accepts the object that owns `.phone` — the mobile API itself or the server, which holds
    it as `.mobile` — so both the desktop server and the executor wrapper can call it.
    """
    if mobile is not None and getattr(mobile, 'phone', None) is None:
        mobile = getattr(mobile, 'mobile', mobile)
    phone = getattr(mobile, 'phone', None)
    if phone is None:
        return dict(available=False, reason='phone_control_missing', connected=False, devices=[])
    now = time.time()
    subscribers = getattr(mobile, 'subscribers', {}) or {}
    streaming = {device for device in subscribers.values() if device}
    rows = []
    for device, value in (getattr(phone, 'devices', {}) or {}).items():
        age = _age(phone, value, now)
        operations = value.get('operations') or []
        retention = FORGET_TEST_SECONDS if value.get('test') else FORGET_SECONDS
        rows.append(dict(device_id=device, enabled=bool(value.get('enabled')),
                         operations=len(operations), has_stream=device in streaming,
                         last_seen_seconds=age,
                         online=age is not None and age <= RECENT_SECONDS,
                         forgets_in_seconds=None if age is None else round(retention - age, 1),
                         test_device=bool(value.get('test'))))
    rows.sort(key=lambda row: (not row['online'], row['last_seen_seconds'] if row['last_seen_seconds'] is not None else 1e9))
    connected = any(row['online'] and row['enabled'] and row['operations'] for row in rows)
    return dict(available=True, connected=connected, window_seconds=RECENT_SECONDS,
                streams=len(streaming), devices=rows, gap='' if connected else phone_gap(rows))


def phone_gap(rows):
    """Why nothing is connected, in one line for the owner reading the snapshot."""
    if not rows:
        return '没有任何手机注册过；需要重新配对'
    offline = [row for row in rows if not row['online']]
    if not offline:
        return '设备在注册窗口内，但无障碍未开或没有可用操作'
    ages = [row['last_seen_seconds'] for row in offline if row['last_seen_seconds'] is not None]
    if not ages:
        return '设备没有上报过注册时间'
    return ('最近一次注册在 %d 秒前；超过 %d 秒未注册即视为离线'
            % (round(min(ages)), RECENT_SECONDS))
