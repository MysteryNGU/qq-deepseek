"""One business lifecycle for motivation, plans, promises and execution jobs."""
import json
import time


class TaskService:
    def __init__(self, repo):
        self.repo = repo

    def get(self, ref, scope_id=None):
        row = self.repo._one('SELECT v FROM meta WHERE k=?', ('action_task:' + ref,))
        value = json.loads(row['v']) if row else None
        return value if value and (scope_id is None or value['scope_id'] == scope_id) else None

    def ensure(self, scope, ref, goal, **links):
        with self.repo.tx():
            task = self.get(ref, scope.scope_id)
            if task is None:
                task = dict(ref=ref, scope_id=scope.scope_id, conversation_id=scope.conversation_id,
                            goal=goal, status='active')
            elif goal is not None:
                task['goal'] = goal
            if task.get('status') in {'done','cancelled'} and links.get('status',task['status'])!=task['status']:
                raise ValueError('terminal_task_requires_explicit_transition')
            task.update(links)
            self.repo._exec('INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)',
                            ('action_task:' + ref, json.dumps(task, ensure_ascii=False)))
            return task

    def blocker(self, ref, scope_id):
        """A waiting/stopped ancestor also holds its delegated subgoals."""
        seen = set()
        while ref:
            if ref in seen:
                raise ValueError('cyclic_task_parent')
            seen.add(ref)
            task = self.get(ref,scope_id)
            if not task or task['status']!='active':
                return task or dict(ref=ref,status='missing')
            ref = task.get('parent_ref')
        return None

    def waiting(self, scope_id):
        return [json.loads(row['v']) for row in self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' "
            "AND json_extract(v,'$.scope_id')=? AND json_extract(v,'$.status')='waiting'",(scope_id,))]

    def dependents(self, ref, scope_id):
        refs = {ref}
        tasks = [json.loads(r['v']) for r in self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' "
                 "AND json_extract(v,'$.scope_id')=?",(scope_id,))]
        while True:
            added = {t['ref'] for t in tasks if t.get('parent_ref') in refs}-refs
            if not added:
                return refs
            refs.update(added)

    def transition(self, scope_id, ref, status, *, cause='', user_resume=False):
        if status not in {'active','waiting','paused','cancelled','done'}:
            raise ValueError('invalid_task_status')
        with self.repo.tx():
            task = self.get(ref, scope_id)
            if task is None:
                raise ValueError('unknown_scoped_task')
            if task.get('merged_into') and status!='cancelled':
                raise ValueError('merged_task_cannot_resume')
            if status == 'active' and (task.get('status') in {'cancelled','done'} or
                    (task.get('status')=='paused' and task.get('pause_owner')=='user') or
                    (task.get('status')=='waiting' and task.get('wait_reason')=='user_reply')) and not user_resume:
                raise ValueError('explicit_resume_required')
            task.update(status=status, transition_cause=cause, updated_at=time.time())
            if status in {'done','cancelled'}:
                task['closed_after_ingest_seq'] = self.repo._one(
                    'SELECT COALESCE(MAX(ingest_seq),0) AS n FROM events WHERE agent_id=? AND conversation_id=?',
                    (scope_id.split('|',1)[0],task['conversation_id']))['n']
            self.repo._exec('UPDATE meta SET v=? WHERE k=?',
                            (json.dumps(task, ensure_ascii=False), 'action_task:' + ref))
            if task.get('intent_id'):
                mapped = {'active':'active','waiting':'blocked','paused':'paused','cancelled':'dropped','done':'done'}[status]
                self.repo._exec('UPDATE intents SET status=?,job_id=?,blocked_reason=?,updated_at=? WHERE id=?',
                    (mapped, 'solve:'+ref if status=='active' else '', '' if status=='active' else cause,
                     time.time(),task['intent_id']))
            if task.get('want_id'):
                self.repo._exec('UPDATE wants SET status=? WHERE id=?',
                    ('complete' if status=='done' else status,task['want_id']))
            for loop_id in task.get('loop_ids', []):
                self.repo._exec('UPDATE open_loops SET status=?,updated_at=? WHERE id=?',
                    ('done' if status=='done' else status,time.time(),loop_id))
            if status != 'active':
                self.repo._exec("UPDATE work_jobs SET status=?,lease_token='',lease_until=0,updated=? "
                    "WHERE root_id=? AND scope_id=? AND status IN ('pending','waiting')", (status,time.time(),ref,scope_id))
            return task
