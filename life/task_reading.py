"""Complete task rows with revision-bound pagination and explicit read progress."""
import json

from .agent_session import SessionFailure,canonical,digest
from .resource_policy import estimate_tokens


class TaskReading:
    def __init__(self,role):
        self.role,self.repo = role,role.repo

    def key(self,sid):
        return 'task_read_pending:'+sid

    def clear(self,sid):
        with self.repo.tx():
            self.repo._exec('DELETE FROM meta WHERE k=?',(self.key(sid),))

    def catalog(self,scope):
        return [json.loads(r['v']) for r in self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' "
            "AND json_extract(v,'$.scope_id')=? ORDER BY k",(scope.scope_id,))]

    def review_version(self,scope,tasks):
        return digest(dict(scope=scope.scope_id,audience=scope.audience_policy_id,epoch=self.repo.forget_epoch(scope.scope_id),
            tasks=[t for t in tasks if t['status'] in {'active','waiting','paused'}]))

    def require_review(self,scope,root):
        tasks = self.catalog(scope)
        if not any(t['status'] in {'active','waiting','paused'} for t in tasks):
            return
        row = self.repo._one('SELECT v FROM meta WHERE k=?',('task_catalog_review:'+root['session_id'],))
        if not row or row['v']!=self.review_version(scope,tasks):
            raise SessionFailure('task_review_required','建立新任务前先读完当前未结束任务，判断是否应更新或合并已有事项。',
                                 next_read=dict(kind='intents',status='open'))

    def require_complete(self,sid):
        row = self.repo._one('SELECT v FROM meta WHERE k=?',(self.key(sid),))
        if row:
            raise SessionFailure('incomplete_task_read','任务列表还有未读页；先按 next_read 继续读取，再回答或结束本轮。',
                                 next_read=json.loads(row['v']))

    def read(self,scope,p,root):
        if p.get('related') is True:
            return dict(items=self.role.activities.related(scope),meaning='accessible_same_person_group_tasks',transfer_action='life.intent action=transfer with ref/base_revision and current user source')
        if p.get('ref'):
            task = self.role.tasks.get(str(p['ref']),scope.scope_id)
            if not task:
                raise SessionFailure('unknown_scoped_task','此会话中没有该任务')
            return dict(task=task)
        status = p.get('status','open')
        allowed = {'open','all','active','waiting','paused','done','cancelled'}
        if status not in allowed:
            raise SessionFailure('invalid_input','任务 status 应为 open、all 或具体状态')
        limit = p.get('limit',20)
        if type(limit) is not int or not 1<=limit<=200:
            raise SessionFailure('invalid_input','任务 limit 应为 1 到 200 的整数')
        catalog = self.catalog(scope)
        tasks = [t for t in catalog if status=='all' or t['status'] in (
            {'active','waiting','paused'} if status=='open' else {status})]
        snapshot = digest(dict(scope=scope.scope_id,audience=scope.audience_policy_id,status=status,tasks=tasks))
        offset = 0
        if p.get('cursor'):
            try:
                cursor = json.loads(p['cursor'])
                offset = cursor['offset']
                if cursor['snapshot']!=snapshot:
                    raise SessionFailure('stale_context','任务已变化，请从第一页重新读取',next_read=dict(kind='intents',status=status))
                if type(offset) is not int or not 0<=offset<=len(tasks):
                    raise ValueError()
            except (ValueError,TypeError,KeyError):
                raise SessionFailure('invalid_input','任务分页 cursor 无效')
        cap = min(self.role.resources.text_preview_target_tokens,self.role.resources.text_result_max_tokens)
        byte_cap = self.role.resources.text_result_max_utf8_bytes
        def fits(page):
            body = canonical(dict(ok=True,**page))
            return estimate_tokens(body)<=cap and len(body.encode('utf-8'))<=byte_cap
        def envelope(items):
            end = offset+len(items)
            cursor = canonical(dict(snapshot=snapshot,offset=end)) if end<len(tasks) else None
            return dict(items=items,total=len(tasks),status_filter=status,coverage='partial' if cursor else 'reported',
                has_more=bool(cursor),next_cursor=cursor,
                next_read=dict(kind='intents',status=status,limit=limit,cursor=cursor) if cursor else None)
        items = []
        for task in tasks[offset:offset+limit]:
            item = {k:task[k] for k in ('ref','goal','status','kind','origin','revision','checkpoint') if k in task}
            item['origin_evidence'] = task.get('origin_evidence','legacy_unverified')
            item['execution_status'] = self.execution_status(task)
            if task.get('merged_into'):
                item['merged_into'] = task['merged_into']
            if not fits(envelope([*items,item])):
                if items:
                    break
                # Keep a whole, identified row even for an unusually long goal.
                item.update(details_truncated=True,next_read=dict(kind='intents',ref=task['ref']))
                while not fits(envelope([item])):
                    key = max(('goal','checkpoint'),key=lambda k:len(item.get(k,'')))
                    value = item.get(key,'')
                    if len(value)<=16:
                        raise SessionFailure('resource_limit','任务分页元数据超过配置上限；请提高结果预算')
                    item[key] = value[:len(value)//2]
            items.append(item)
        result = envelope(items)
        sid = root['session_id']
        if result['has_more']:
            with self.repo.tx():
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(self.key(sid),canonical(result['next_read'])))
        else:
            self.clear(sid)
            if status in {'open','all'}:
                with self.repo.tx():
                    self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                        ('task_catalog_review:'+sid,self.review_version(scope,catalog)))
        return result

    def execution_status(self,task):
        if task['status']!='active':
            return task['status']
        running = self.repo._one("SELECT 1 FROM role_operations o JOIN agent_sessions s USING(session_id) "
            "WHERE o.intent_ref=? AND o.scope_id=? AND o.status='dispatched' "
            "AND s.status IN ('current','delegate') LIMIT 1",(task['ref'],task['scope_id']))
        running = running or self.repo._one("SELECT 1 FROM work_jobs WHERE root_id=? AND scope_id=? "
            "AND status IN ('running','pending','waiting') LIMIT 1",(task['ref'],task['scope_id']))
        return 'in_progress' if running else 'pending'
