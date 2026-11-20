"""A small, model-free view of the world around her.

Every entry is a real observation with a source and an expiry. Nothing here
calls the model, invents weather, or guesses a city from the server's IP.
"""
import json
import time

TIME_TTL = 300.0
CONNECTION_TTL = 900.0
SOCIAL_TTL = 1800.0
WEATHER_TTL = 3600.0


def _entry(key, kind, summary, *, now, ttl, source_refs=(), details=None):
    return dict(stable_key=key, kind=kind, summary=summary, observed_at=now,
                expires_at=now+ttl, source_refs=list(source_refs),
                details=dict(details or {}))


class AmbientCollector:
    def __init__(self, core, *, weather=None):
        self.core = core
        self.cfg = core.cfg
        self._weather = weather          # injectable for tests
        self.last_weather = {}

    # --- individual sources -------------------------------------------------
    def _clock(self, now):
        text = time.strftime('%Y-%m-%d %H:%M %a', time.localtime(now))
        return _entry('clock', 'time', text, now=now, ttl=TIME_TTL)

    def _connection(self, now):
        entries = []
        body = getattr(self.core, '_body_context', {}) or {}
        entries.append(_entry('desktop', 'device',
            '桌面的身体已连接' if body.get('connected') else '桌面身体当前未连接',
            now=now, ttl=CONNECTION_TTL, details=dict(device='desktop', connected=bool(body.get('connected')))))
        phone = {}
        executor = getattr(self.core, 'executor', None)
        if executor is not None and hasattr(executor, 'phone_context'):
            try:
                phone = executor.phone_context() or {}
            except Exception:
                phone = {}
        devices = phone.get('devices') or []
        # 她的眼睛：每轮都把「手机现在有什么」摊开——在线台数、操作项、
        # 已打开的工作区（id/尺寸）。判断归她，这里只报告世界。
        text = '手机已连接' if devices else '手机当前未连接'
        details = dict(device='phone', connected=bool(devices))
        if devices:
            first = devices[0] if isinstance(devices[0], dict) else {}
            operations = [str(item) for item in (first.get('operations') or [])]
            workspaces = [item for item in (first.get('workspaces') or []) if isinstance(item, dict)]
            details.update(operations=operations,
                           workspace_state=first.get('workspace_state') or 'unknown',
                           workspaces=[{key: item[key] for key in
                                        ('workspace_id', 'session_id', 'width', 'height', 'package', 'activity', 'state')
                                        if key in item} for item in workspaces[:4]])
            parts = ['%d 台在线' % len(devices), '%d 项操作' % len(operations)]
            if workspaces:
                parts.append('工作区 ' + '、'.join(str(item.get('workspace_id')) for item in workspaces))
            elif first.get('workspace_state'):
                parts.append('工作区状态 ' + str(first.get('workspace_state')))
            text = '手机已连接：' + '，'.join(parts)
        entries.append(_entry('phone', 'device', text, now=now, ttl=CONNECTION_TTL, details=details))
        return entries

    def _social(self, now):
        entries = []
        town = getattr(self.core, 'town', None)
        if town is not None and getattr(town, 'config', None) is not None and getattr(town.config, 'enabled', False):
            snapshot = town.snapshot() if hasattr(town, 'snapshot') else {}
            entries.append(_entry('town', 'town',
                '小镇在线' if snapshot.get('state') == 'online' else '小镇当前不在线',
                now=now, ttl=SOCIAL_TTL, details=dict(state=snapshot.get('state'))))
        bilibili = getattr(self.core, 'bilibili_social', None)
        if bilibili is not None:
            context = bilibili.context() if hasattr(bilibili, 'context') else {}
            entries.append(_entry('bilibili', 'bilibili',
                'B站有未处理的来信' if context.get('pending') else 'B站没有新的来信',
                now=now, ttl=SOCIAL_TTL, details=dict(login=context.get('login'))))
        return entries

    def _weather_entry(self, now, city):
        """Only a real reading counts. A failed call produces nothing at all."""
        if not city:
            return None
        try:
            reading = self._weather(city) if self._weather else {}
        except Exception:
            return None
        summary = str((reading or {}).get('summary') or '').strip()
        if not summary:
            return None
        self.last_weather = dict(reading)
        return _entry('weather', 'weather', summary, now=now, ttl=WEATHER_TTL,
                      source_refs=[(reading or {}).get('source', '')], details=dict(city=city))

    # --- public -------------------------------------------------------------
    def collect(self, *, now=None):
        now = time.time() if now is None else now
        entries = [self._clock(now)]
        entries.extend(self._connection(now))
        entries.extend(self._social(now))
        city = str(getattr(self.cfg, 'weather_city', '') or '').strip()
        weather = self._weather_entry(now, city)
        if weather:
            entries.append(weather)
        return entries

    def persist(self, entries):
        """Store the latest value per stable key; older rows stay as history."""
        rows = 0
        for item in entries:
            details = {key: item[key] for key in
                       ('stable_key', 'kind', 'observed_at', 'expires_at', 'source_refs', 'details')}
            self.core.repo._exec('INSERT INTO companion_activity VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET text=excluded.text,details=excluded.details,created=excluded.created '
                'WHERE excluded.created>=companion_activity.created',
                ('ambient:' + item['stable_key'] + ':' + str(int(item['observed_at'])),
                 self.core.life.agent_id, 'ambient', item['summary'], json.dumps(details, ensure_ascii=False),
                 item['observed_at']))
            rows += 1
        return rows
