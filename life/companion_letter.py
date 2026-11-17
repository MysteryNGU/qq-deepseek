"""One readable weekly letter, at most once per ISO week, from real material."""
import json
import time
from datetime import datetime
from .companion_life_view import BEIJING, LIVED_SQL, _item

SUNDAY = 6          # datetime.weekday(), Beijing time
START_HOUR = 20
LOOKBACK = 7 * 86400


def week_key(now):
    year,week,_=datetime.fromtimestamp(now,BEIJING).isocalendar()
    return f'{year}-W{week:02d}'


class Letter:
    def __init__(self, core):
        self.core = core
        self.repo = core.repo

    def material(self, now=None):
        now = time.time() if now is None else now
        rows = self.repo._all("SELECT id,type,text,details,created FROM companion_activity WHERE agent=? "
            "AND created>=? AND created<=? AND "+LIVED_SQL+
            " ORDER BY created DESC LIMIT 60", (self.core.life.agent_id, now - LOOKBACK,now))
        items = [dict(id=row['id'],kind=row['type'], text=_item(row,now)['text'][:160])
                 for row in rows if str(row['text']).strip()]
        notes = [dict(key=n['key'], text=str(n['text'])[:160]) for n in self.core._notes()[:20]]
        return dict(entries=items, notes=notes)

    def due(self, now=None):
        now = time.time() if now is None else now
        moment = datetime.fromtimestamp(now,BEIJING)
        if moment.weekday() != SUNDAY or moment.hour < START_HOUR:
            return False
        if self.core._state('last_letter') == week_key(now):
            return False
        material = self.material(now)
        return bool(material['entries'])

    def context(self, now=None):
        now = time.time() if now is None else now
        if not self.due(now):
            return None
        material = self.material(now)
        return dict(due=True, week=week_key(now),
                    entries=material['entries'][:20], notes=material['notes'][:10])

    def record(self, text, now=None, source_refs=()):
        """Store a real letter once. Re-reading costs nothing."""
        now = time.time() if now is None else now
        key = week_key(now)
        if self.core._state('last_letter') == key:
            return None
        body = str(text or '').strip()
        if not body:
            return None
        # No nested transaction: callers may already be inside one (_commit).
        self.repo._exec('INSERT INTO companion_activity VALUES(?,?,?,?,?,?)',
            ('letter:' + key, self.core.life.agent_id, 'letter', body[:4000],
             json.dumps(dict(week=key, source_refs=list(source_refs), created_at=now),
                        ensure_ascii=False), now))
        self.core._save('last_letter', key)
        return dict(week=key, text=body[:4000])
