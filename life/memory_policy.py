"""Bounds and validity shared by memory writers and automatic retrieval."""
import json
import math
import re
import unicodedata

from .text import stable_id

INFERENCE_DAYS = 7
MAX_REMINDERS = 128
MAX_STANDING_REMINDERS = 12
STANDING_TOKEN_BUDGET = 800
MAX_INTERPRETATIONS = 64


def reset_boundary(repo, scope):
    row = repo._one('SELECT v FROM meta WHERE k=?',('memory_policy_floor:'+scope.scope_id,))
    return float(row['v']) if row else 0


def topic_key(value, text):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('topic_key_requires_at_most_80_characters')
    value = unicodedata.normalize('NFKC', value).strip().casefold()
    normalized = re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text).casefold())
    return value or stable_id('content', normalized)


def inference_expiry(days=None, *, now):
    days = INFERENCE_DAYS if days is None else days
    if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days) or not 0 < days <= INFERENCE_DAYS:
        raise ValueError('interpretation_lifetime_requires_0_to_7_days')
    return now + days * 86400


def source_deadline(repo, sources, deadline, now, scope):
    """Rephrasing an older thought cannot give its descendants a fresh lifetime."""
    pending, seen = list(sources), set()
    floor = reset_boundary(repo,scope)
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        if len(seen) > 1024:
            raise ValueError('interpretation_source_chain_too_large')
        visible = repo._one('SELECT id FROM events WHERE id=? AND agent_id=? AND conversation_id=? AND audience_policy_id=?',
                            (ref,*scope.private_key,scope.audience_policy_id))
        if not visible:
            raise ValueError('interpretation_source_scope_mismatch')
        event = repo.get_event(ref)
        if event is None or event.redacted_at is not None:
            raise ValueError('interpretation_source_removed')
        if repo._one('SELECT 1 FROM learning_source_barriers WHERE scope_id=? AND event_id=?',(scope.scope_id,ref)):
            raise ValueError('retired_learning_source_requires_new_evidence')
        if event.source_kind == 'inference' and event.kind in {'thought','reflection'}:
            if event.received_at < floor:
                raise ValueError('retired_interpretation_source_requires_new_evidence')
            deadline = min(deadline,event.occurred_at + INFERENCE_DAYS * 86400)
        pending.extend(event.source_event_ids)
    if deadline <= now:
        raise ValueError('expired_interpretation_source_requires_new_evidence')
    return deadline


def interpretation_current(repo, memory, now):
    """Historical interpretations remain readable but cannot steer current retrieval."""
    if memory.kind != 'interpretation':
        return True
    expiry = memory.valid_until or memory.recorded_at + INFERENCE_DAYS * 86400
    if expiry <= now:
        return False
    try:
        source_deadline(repo,[s.event_id for s in memory.sources],expiry,now,memory.scope)
    except ValueError:
        return False
    for tag in memory.topic_tags:
        if not tag.startswith('intent:'):
            continue
        row = repo._one('SELECT v FROM meta WHERE k=?', ('action_task:' + tag[7:],))
        task = json.loads(row['v']) if row else None
        if not task or task.get('scope_id') != memory.scope.scope_id or task.get('status') != 'active':
            return False
        versions = [t[16:] for t in memory.topic_tags if t.startswith('intent-revision:')]
        if versions and versions != [str(task.get('revision', 1))]:
            return False
    return True
