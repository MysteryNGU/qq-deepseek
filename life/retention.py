"""Deletion intents and redaction share the life.db transaction boundary."""
from __future__ import annotations

import json

from .contracts import Scope, new_id, now_ts


class TombstoneLedger:
    def __init__(self, repo):
        self.repo = repo
        repo.db.execute("CREATE TABLE IF NOT EXISTS deletion_intents("
                        "id TEXT PRIMARY KEY,agent TEXT,conversation TEXT,memories TEXT,events TEXT,created REAL)")

    def append(self, scope, memory_ids, event_ids):
        identity = new_id("delete")
        with self.repo.tx():
            self.repo._exec("INSERT INTO deletion_intents VALUES(?,?,?,?,?,?)",
                         (identity, scope.agent_id, scope.conversation_id,
                          json.dumps(sorted(set(memory_ids))), json.dumps(sorted(set(event_ids))), now_ts()))
        return identity

    def apply(self, identity, scope, memory_ids, event_ids, *, epoch=None):
        repo = self.repo
        with repo.tx():
            if repo._one("SELECT 1 FROM tombstones WHERE id=?", (identity,)):
                return
            # Only the identifiers present at deletion are recorded; future
            # messages in a reset conversation remain valid after restoration.
            closure = set(str(x) for x in memory_ids)
            events = set(str(x) for x in event_ids)
            while True:
                before = (len(closure), len(events))
                closure.update(repo.memory_ids_sharing_events(list(events)))
                closure.update(repo.memory_ids_derived_from(list(closure)))
                for memory_id in closure:
                    memory = repo.get_memory(memory_id)
                    if memory and memory.scope.agent_id != scope.agent_id:
                        raise ValueError('cross_agent_deletion_dependency')
                for event_id in events:
                    event = repo.get_event(event_id)
                    if event and event.scope.agent_id != scope.agent_id:
                        raise ValueError('cross_agent_deletion_dependency')
                # Invalidate descendants, not the other independent parents of
                # a shared summary. The explicitly forgotten roots are supplied
                # by MemoryService before expanding the dependent memories.
                for row in repo._all("SELECT id,agent_id,source_event_ids FROM events WHERE redacted_at IS NULL"):
                    if events.intersection(json.loads(row["source_event_ids"])):
                        if row['agent_id'] != scope.agent_id:
                            raise ValueError('cross_agent_deletion_dependency')
                        events.add(row["id"])
                if before == (len(closure), len(events)):
                    break
            epoch = repo.bump_forget_epoch(scope.scope_id) if epoch is None else epoch
            repo.redact_memories(list(closure), epoch)
            repo.redact_events(list(events))
            # Relationship material retains the union of necessary sources
            # across revisions; deleting any one erases its dependent body.
            for note in repo._all('SELECT a.id,a.scope_id,a.sources,l.origin FROM memory_annotations a '
                    'LEFT JOIN relationship_annotation_links l ON l.annotation_id=a.id'):
                if events.intersection(json.loads(note['sources'])) or (note['scope_id']==scope.scope_id and note['origin']=='legacy_unverified'):
                    repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?',(note['id'],))
                    repo._exec("UPDATE memory_annotations SET text='',cues='[]',sources='[]',skill_refs='[]',status='redacted' WHERE id=?",(note['id'],))
                    repo._exec('DELETE FROM relationship_annotation_links WHERE annotation_id=?',(note['id'],))
            repo.cancel_open_loops_for_events(list(events))
            repo.redact_knowledge_cards_for_events(list(events))
            # Model results, drafts and state causes may contain paraphrases.
            # They must be invalidated at the same commit as the source text.
            repo._exec("UPDATE work_jobs SET status='cancelled',payload='{}',checkpoint='{}',lease_token='' WHERE scope_id=?", (scope.scope_id,))
            repo._exec("UPDATE deliveries SET payload='{}',sources='[]',status=CASE WHEN status='confirmed' THEN status ELSE 'cancelled' END WHERE scope_id=?", (scope.scope_id,))
            journal = [dict(row) for row in repo._all(
                "SELECT * FROM state_journal WHERE scope_id=? ORDER BY id", (scope.scope_id,))]
            # The role state spans conversations. Rebuild its ordered journal
            # before erasing invalid transitions; keep unrelated experiences.
            repo._exec("DELETE FROM interests WHERE scope_id=?", (scope.scope_id,))
            repo._exec("DELETE FROM wants WHERE scope_id=?", (scope.scope_id,))
            repo._exec("DELETE FROM study_threads WHERE agent_id=? AND conversation_id=?", scope.private_key)
            runtime = getattr(self, "runtime", None)
            if runtime is not None:
                runtime.rebuild_after_forget(scope, journal)
            if repo._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_sessions'"):
                repo._exec("UPDATE agent_sessions SET status='retired' WHERE scope_id=?",(scope.scope_id,))
                for table,column in (('agent_inputs','payload'),('agent_events','payload'),('life_calls','receipt')):
                    repo._exec(f"UPDATE {table} SET {column}='{{}}' WHERE session_id IN "
                               '(SELECT session_id FROM agent_sessions WHERE scope_id=?)',(scope.scope_id,))
                repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=?",(scope.scope_id,))
                repo._exec("UPDATE role_operations SET arguments='{}',result='{}',cancel_requested=1 WHERE scope_id=?",(scope.scope_id,))
                repo._exec("DELETE FROM meta WHERE k LIKE 'action_task:%' AND json_extract(v,'$.scope_id')=?",(scope.scope_id,))
                repo._exec("DELETE FROM meta WHERE k IN (SELECT 'delegate_methods:'||session_id FROM agent_sessions WHERE scope_id=?)",(scope.scope_id,))
                repo._exec("DELETE FROM meta WHERE k IN (SELECT 'delegate_method_sources:'||id FROM role_operations WHERE scope_id=?)",(scope.scope_id,))
                for row in repo._all("SELECT k,v FROM meta WHERE k LIKE 'skill_meta:%'"):
                    method=json.loads(row['v'])
                    if method.get('scope_id')!=scope.scope_id:
                        continue
                    if method.get('visibility')=='public' and not method.get('sources'):
                        method['forget_epoch']=epoch
                    else:
                        method.update(state='revoked',description='',sources=[],version=method['version']+1)
                    repo._exec('UPDATE meta SET v=? WHERE k=?',(json.dumps(method,ensure_ascii=False),row['k']))
                repo._exec("DELETE FROM meta WHERE k=? OR (k LIKE 'continuity:expression:%' AND json_extract(v,'$.scope_id')=?)",
                           ('continuity:reflection:'+scope.scope_id,scope.scope_id))
                for control in repo._all("SELECT k,v FROM meta WHERE k LIKE 'initiative:%'"):
                    value = json.loads(control['v'])
                    if value.get('scope_id')!=scope.scope_id and value.get('root_scope_id')!=scope.scope_id:
                        continue
                    if control['k'].startswith(('initiative:guard:','initiative:intent:')) or control['k']=='initiative:resource_offer':
                        repo._exec('DELETE FROM meta WHERE k=?',(control['k'],))
                    else:
                        # Keep only ownership needed to stop/reconcile live work.
                        value.pop('arguments',None)
                        value.pop('trigger_refs',None)
                        if 'attribution' in value:
                            value['attribution']['trigger_refs']=[]
                        if value.get('members'):
                            value['phase']='outcome_unknown'
                        repo._exec('UPDATE meta SET v=? WHERE k=?',(json.dumps(value,ensure_ascii=False),control['k']))
            if repo._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='expressions'"):
                repo._exec("UPDATE expressions SET plan='{}',rendered=NULL WHERE scope_id=?",(scope.scope_id,))
            if repo._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='self_changes'"):
                repo._exec("UPDATE self_changes SET phase='retired',manifest='{}',receipt='{}' WHERE scope_id=?",(scope.scope_id,))
            if repo._one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_assets'"):
                repo._exec("DELETE FROM media_perceptions WHERE asset_id IN (SELECT id FROM media_assets WHERE scope_id=?)",(scope.scope_id,))
                repo._exec("UPDATE media_assets SET status='redacted',source_ref='{}',metadata='{}' WHERE scope_id=?",(scope.scope_id,))
            repo._exec("INSERT INTO tombstones VALUES(?,?,?,?,?,?)",
                       (identity, scope.scope_id, epoch, json.dumps(sorted(closure)), json.dumps(sorted(events)), now_ts()))

    def recover(self):
        rows = self.repo._all('SELECT * FROM deletion_intents ORDER BY created,id')
        for row in rows:
            self.apply(row['id'],Scope(row['agent'],row['conversation']),json.loads(row['memories']),json.loads(row['events']))

    def purge(self):
        """Erase redacted bodies and checkpoint WAL. Call while workers stopped.

        Historical external backup files must be removed by their custodian;
        restored files remain fenced by the independent ledger regardless.
        """
        repo = self.repo
        with repo.tx():
            repo._exec("PRAGMA secure_delete=ON")
            repo._exec("UPDATE events SET payload='{}',source_event_ids='[]' WHERE redacted_at IS NOT NULL")
            repo._exec("UPDATE memories SET summary='',normalized_value='',topic_tags='[]',uncertainty_reason='' WHERE status='redacted'")
            repo._exec("DELETE FROM knowledge_evidence WHERE card_id NOT IN (SELECT id FROM knowledge_cards)")
        repo.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
