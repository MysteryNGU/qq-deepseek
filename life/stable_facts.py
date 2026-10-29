"""Extract reported facts through the existing fact revision and source graph."""
import re

from .life_access import RecallError
from .learning_validity import qualification
from .memory import MemoryService
from .text import normalize_value


class StableFacts:
    def __init__(self, catalog, access):
        self.catalog, self.access, self.repo = catalog, access, catalog.repo

    def commit(self, ctx, proposals, allowed_refs):
        if not isinstance(proposals, list) or len(proposals) > 8:
            raise RecallError('invalid_facts')
        results = []
        for proposal in proposals:
            if not isinstance(proposal, dict):
                raise RecallError('invalid_facts')
            actor, topic = proposal.get('actor_id'), proposal.get('topic_key')
            refs, quote = proposal.get('evidence_refs'), proposal.get('quote')
            operation = proposal.get('operation', 'observe')
            if (not isinstance(actor, str) or not re.fullmatch(r'[0-9]+', actor)
                    or not isinstance(topic, str) or not re.fullmatch(r'[a-z0-9_.-]{1,80}', topic)
                    or not isinstance(quote, str) or not quote.strip() or len(quote) > 400
                    or not isinstance(refs, list) or not 1 <= len(refs) <= 8
                    or any(not isinstance(ref, str) for ref in refs)
                    or not set(refs) <= allowed_refs
                    or operation not in {'observe', 'correct', 'change'}):
                raise RecallError('invalid_facts')
            events = []
            for ref in refs:
                resource, _ = self.access.load(ctx, ref)
                event = self.repo.get_event(resource['native_id']) if resource['kind'] == 'event' else None
                # Keep the full original statement, including negation and context.
                # The model chooses the stable topic; it cannot manufacture a quote.
                if (not event or event.kind != 'message' or event.source_kind != 'user_report'
                        or event.actor_id != actor or quote.strip() != event.text.strip()):
                    raise RecallError('fact_evidence_mismatch')
                events.append(event)
            if len({e.scope.scope_id for e in events}) != 1:
                raise RecallError('fact_scope_mismatch')
            state = qualification(self.catalog, [e.id for e in events])
            if not state['eligible']:
                results.append(dict(topic_key=topic, saved=False, reason='historical_evidence'))
                continue
            scope = events[0].scope
            old = self.repo.find_facts(scope, actor, topic)
            # Do not resurrect forgotten / no-longer-permitted facts when merging.
            current = []
            for fact in old:
                try:
                    self.access.load(ctx, fact.id)
                except RecallError:
                    continue
                current.append(fact)
            if len(current) != len(old):
                results.append(dict(topic_key=topic, saved=False, reason='existing_fact_unavailable'))
                continue
            latest = max((e.received_at for m in current for s in m.sources
                          if (e := self.repo.get_event(s.event_id))), default=0)
            if (current and max(e.received_at for e in events) <= latest
                    and (operation != 'observe' or all(m.normalized_value != normalize_value(quote) for m in current))):
                results.append(dict(topic_key=topic, saved=False, reason='older_than_current'))
                continue
            memory = MemoryService(self.repo).revise_fact(scope, actor, topic, quote,
                [e.id for e in events], summary=quote, explicit_correction=operation == 'correct',
                explicit_change=operation == 'change', valid_from=max(e.occurred_at for e in events))
            if memory.status == 'disputed':
                for old_fact in current:
                    if old_fact.status == 'active':
                        self.repo.update_memory(old_fact.id, status='disputed')
            resource = self.catalog.register('memory', memory.id)
            results.append(dict(resource_ref=resource['resource_id'], topic_key=topic,
                                saved=True, status=memory.status, supersedes=memory.supersedes_id))
        return results
