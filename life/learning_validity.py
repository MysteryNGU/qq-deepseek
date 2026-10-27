"""Learning qualifications, distinct from retention and historical read access."""
import json

from .memory_policy import INFERENCE_DAYS, interpretation_current, reset_boundary


def qualification(catalog, refs, *, interpreted=False):
    repo,now=catalog.repo,catalog.clock()
    pending,seen=list(refs),set()
    reasons=set() if refs else {'source_removed'}
    deadline=None
    while pending:
        ref=pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        if len(seen)>1024:
            reasons.add('source_chain_too_large')
            break
        event=repo.get_event(ref)
        if not event or event.scope.agent_id!=catalog.agent_id or event.redacted_at is not None:
            reasons.add('source_removed')
            continue
        if event.received_at<reset_boundary(repo,event.scope):
            reasons.add('learning_reset')
        if repo._one('SELECT 1 FROM learning_source_barriers WHERE scope_id=? AND event_id=?',(event.scope.scope_id,event.id)):
            reasons.add('learning_revoked')
        if interpreted or event.source_kind=='inference':
            limit=min(event.occurred_at,event.received_at)+INFERENCE_DAYS*86400
            deadline=limit if deadline is None else min(deadline,limit)
        # The original thought's own applicability/expiry must not be replaced
        # by the internal summary scope or by its later creation date.
        if event.source_kind=='inference':
            for row in repo._all("SELECT m.id FROM memories m JOIN memory_sources s ON s.memory_id=m.id "
                    "WHERE s.event_id=? AND m.kind='interpretation' AND m.agent_id=?",(event.id,catalog.agent_id)):
                note=repo.get_memory(row['id'])
                if note.status not in {'active','disputed'} or not interpretation_current(repo,note,now):
                    reasons.add('interpretation_not_current')
                if note.valid_until is not None:
                    deadline=note.valid_until if deadline is None else min(deadline,note.valid_until)
                media=[tag for tag in note.topic_tags if tag.startswith('media-version:')]
                if media and not getattr(catalog,'provider_current',lambda tags:False)(media):
                    reasons.add('media_version_changed')
        jid=event.payload.get('job_id')
        job=repo._one('SELECT payload FROM work_jobs WHERE id=? AND scope_id=?',(jid,event.scope.scope_id)) if jid else None
        if job:
            spec=json.loads(job['payload']).get('spec',{})
            adapter=spec.get('_adapter',{})
            if adapter.get('provider_id') and spec.get('model_version'):
                tag='media-version:'+json.dumps(dict(provider_id=adapter['provider_id'],model_version=spec['model_version'],
                    adapter_version=adapter.get('adapter_version'),digest=adapter.get('digest')),sort_keys=True)
                if not getattr(catalog,'provider_current',lambda tags:False)([tag]):
                    reasons.add('media_version_changed')
        pending.extend(event.source_event_ids)
    if deadline is not None and deadline<=now:
        reasons.add('inference_expired')
    return dict(eligible=not reasons,reasons=sorted(reasons),valid_until=deadline)


def summary_claims(catalog, resource):
    row=catalog.repo._one('SELECT v FROM meta WHERE k=?',('memory_summary:'+resource['native_id'],))
    if not row:
        return None
    manifest=json.loads(row['v'])
    result=[]
    for claim in manifest.get('claims',[]):
        native=[]
        for ref in claim['evidence_refs']:
            source=catalog.resolve(ref)
            if source:
                native.append(source['native_id'])
        state=qualification(catalog,native,interpreted=claim['kind'] in {'interpreted','hypothetical'})
        frozen=claim.get('learning') or {}
        if frozen.get('eligible') is False:
            state['eligible']=False
            state['reasons']=sorted(set(state['reasons']+frozen.get('reasons',[])))
        if frozen.get('valid_until') is not None:
            state['valid_until']=min(state['valid_until'] or frozen['valid_until'],frozen['valid_until'])
            if state['valid_until']<=catalog.clock():
                state.update(eligible=False,reasons=sorted(set(state['reasons']+['inference_expired'])))
        result.append(dict(claim,learning=state))
    return result
