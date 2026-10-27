"""Keep user statements separate from model-written retrieval summaries."""
import json

from .memory_policy import INFERENCE_DAYS, reset_boundary, source_deadline


def scoped_sources(repo,scope,source_ids):
    ids = list(dict.fromkeys(source_ids))
    events = repo.events_by_ids(ids)
    if not ids or len(events)!=len(ids) or any(e.redacted_at is not None or
            e.scope.private_key!=scope.private_key or e.scope.audience_policy_id!=scope.audience_policy_id for e in events):
        raise ValueError('learning_requires_current_scoped_sources')
    by_id = {e.id:e for e in events}
    return [by_id[i] for i in ids]


def user_statements(events):
    """Whole messages retain negations, questions and limits on an instruction.

    The model cannot select a convenient substring or supply replacement text.
    """
    return [dict(event_id=e.id,text=e.text) for e in events if e.source_kind=='user_report' and e.text.strip()]


def annotation_preview(row):
    sources=row['sources']
    result=dict(id=row['id'],kind=row['kind'],text=row['text'],
        sources=json.loads(sources) if isinstance(sources,str) else sources,
        authority=row['authority'],applicability=row.get('applicability','legacy'))
    refs=row.get('reference_asset_ids',[])
    if refs:
        result['reference_asset_ids']=refs
    return result


def validate_assets(repo,scope,asset_ids):
    if not isinstance(asset_ids,(list,tuple)) or len(asset_ids)>10 or any(not isinstance(a,str) for a in asset_ids):
        raise ValueError('learning_references_require_at_most_ten_assets')
    result=list(dict.fromkeys(asset_ids))
    for identity in result:
        row=repo._one("SELECT a.id FROM media_assets a JOIN events e ON e.id=a.event_id WHERE a.id=? "
            "AND a.scope_id=? AND a.forget_epoch=? AND a.status='ready' AND e.redacted_at IS NULL "
            "AND e.audience_policy_id=?",(identity,scope.scope_id,repo.forget_epoch(scope.scope_id),scope.audience_policy_id))
        if row is None:
            raise ValueError('learning_reference_not_visible')
    return result


def latest_input(repo,scope):
    row = repo._one("SELECT id FROM events WHERE agent_id=? AND conversation_id=? AND audience_policy_id=? "
        "AND source_kind='user_report' AND redacted_at IS NULL ORDER BY ingest_seq DESC LIMIT 1",
        (*scope.private_key,scope.audience_policy_id))
    return row['id'] if row else ''


def applicability_binding(repo,scope,sources,applicability='task',intent_ref=None):
    if applicability not in {'task','ongoing'}:
        raise ValueError('applicability_requires_task_or_ongoing')
    if applicability=='ongoing':
        return 'ongoing','',0
    if intent_ref:
        row = repo._one('SELECT v FROM meta WHERE k=?',('action_task:'+intent_ref,))
        task = json.loads(row['v']) if row else None
        if not task or task.get('scope_id')!=scope.scope_id or task.get('status') not in {'active','waiting'}:
            raise ValueError('learning_requires_current_task')
        return 'task',intent_ref,int(task.get('revision',1))
    latest = latest_input(repo,scope)
    events = scoped_sources(repo,scope,sources)
    if all(e.source_kind=='user_report' for e in events) and latest not in sources:
        raise ValueError('temporary_preference_requires_current_user_input_or_task')
    return 'exchange',latest or sources[-1],0


def binding_current(repo,scope,application,reference,revision):
    if application=='ongoing' or application=='legacy':
        return True
    if application=='exchange':
        return latest_input(repo,scope)==reference
    if application!='task':
        return False
    row = repo._one('SELECT v FROM meta WHERE k=?',('action_task:'+reference,))
    task = json.loads(row['v']) if row else None
    return bool(task and task.get('scope_id')==scope.scope_id and task.get('status') in {'active','waiting'}
        and int(task.get('revision',1))==revision)


def annotation_view(repo,scope,row,now):
    """Only original user words can appear as a user's saved requirement.

    The stored text remains a search index/audit summary, not authority. Legacy
    records receive the same projection; they cannot smuggle an old summary in.
    """
    row = dict(row)
    try:
        row['reference_asset_ids']=validate_assets(repo,scope,json.loads(row.pop('reference_assets','[]')))
    except ValueError:
        return None
    if row.get('kind')=='relationship_note' and 'origin' not in row:
        link=repo._one('SELECT origin FROM relationship_annotation_links WHERE annotation_id=?',(row['id'],))
        row['origin']=link['origin'] if link else 'legacy_unverified'
    try:
        events = scoped_sources(repo,scope,json.loads(row['sources']))
    except ValueError:
        return None
    application = row.get('applicability','legacy')
    if not binding_current(repo,scope,application,row.get('context_ref',''),row.get('context_revision',0)):
        return None
    explicit = all(e.source_kind=='user_report' for e in events) and row.get('origin')!='interpretation' and row.get('kind')!='interpretation'
    if not explicit and (row.get('expires') or row['created']+INFERENCE_DAYS*86400)<=now:
        return None
    if explicit:
        quotes = user_statements(events)
        if len(quotes)!=len(events) or all(e.received_at<reset_boundary(repo,scope) for e in events):
            return None
        row.update(text='\n'.join(q['text'] for q in quotes),authority='user_statement',source_quotes=quotes,
            usage='按完整原话理解问题、否定和范围；检索摘要不构成附加要求。')
    else:
        try:
            source_deadline(repo,[e.id for e in events],row.get('expires') or row['created']+INFERENCE_DAYS*86400,now,scope)
        except ValueError:
            return None
        row.update(authority='model_interpretation',source_quotes=[],
            usage='模型归纳；不是用户要求，不得据此新增长期限制。')
    return row


def learning_tags(repo,scope,sources,intent_ref=None):
    tags = ['model-derived','not-user-instruction']
    if intent_ref:
        app,ref,revision = applicability_binding(repo,scope,sources,intent_ref=intent_ref)
        tags.extend(['intent:'+ref,'intent-revision:'+str(revision)])
    # Freeze the actual backend versions used by the evidence, not model claims.
    for event in scoped_sources(repo,scope,sources):
        jid = event.payload.get('job_id')
        row = repo._one('SELECT payload FROM work_jobs WHERE id=? AND scope_id=?',(jid,scope.scope_id)) if jid else None
        spec = json.loads(row['payload']).get('spec',{}) if row else {}
        adapter = spec.get('_adapter',{})
        if adapter.get('provider_id') and spec.get('model_version'):
            tags.append('media-version:'+json.dumps(dict(provider_id=adapter['provider_id'],
                model_version=spec['model_version'],adapter_version=adapter.get('adapter_version'),
                digest=adapter.get('digest')),sort_keys=True))
    return list(dict.fromkeys(tags))
