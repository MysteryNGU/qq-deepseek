"""A memory she can reach for on her own, never a random draw.

One candidate per wake at most, always with a real source and a reason why it
surfaced now. Being injected is not the same as having been said out loud.
"""
import json
import time
import uuid

from .companion_context import compact_recall

REPEAT_HOURS = 6.0


class Recollection:
    def __init__(self, core):
        self.core = core
        self.repo = core.repo
        self.scope = core.scope

    def _last(self):
        return self.core._state('last_recollection') or {}

    def pick(self, *, cues=(), now=None, budget=800):
        """Return one candidate, or None. Never searches on empty clues."""
        now = time.time() if now is None else now
        text_cues = [str(cue).strip() for cue in cues if str(cue).strip()]
        if not text_cues:
            return None
        last = self._last()
        if isinstance(last.get('at'), (int, float)) and now - last['at'] < REPEAT_HOURS * 3600:
            return None
        try:
            raw = self.core.life.life_recall.context(self.scope, text_cues, budget)
        except Exception:
            return None
        items = [item for item in compact_recall(raw) if str(item.get('preview') or '').strip()]
        if not items:
            return None
        chosen = items[0]
        source_ref = chosen.get('resource_ref') or ''
        record = dict(text=str(chosen.get('preview'))[:160],
                      why='这和你现在关心的事有关：' + text_cues[0][:24],
                      source_ref=source_ref,
                      age=chosen.get('occurred_at'))
        # No nested transaction: this runs while a cognition turn may already
        # hold one, and SQLite refuses a second BEGIN.
        self.repo._exec('INSERT INTO companion_activity VALUES(?,?,?,?,?,?)',
            (uuid.uuid4().hex, self.core.life.agent_id,
             'recollection', record['text'],
             json.dumps(dict(stable_key=source_ref, kind='recollection', summary=record['text'],
                             why=record['why'], source_ref=source_ref, observed_at=now,
                             expires_at=now + 3600), ensure_ascii=False),
             now))
        self.core._save('last_recollection', dict(source_ref=source_ref, at=now, shared=False))
        return record

    def mark_shared(self, *, source_ref=None, now=None):
        """Called only when a bubble actually went out in the same turn."""
        now = time.time() if now is None else now
        last = self._last()
        if not last.get('source_ref'):
            return False
        if source_ref is not None and source_ref != last['source_ref']:
            return False
        self.core._save('last_recollection', dict(last, shared=True, shared_at=now))
        return True
