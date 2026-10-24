"""Deferred DSH edits use the existing native workshop and the one life journal.

The tool callback acknowledges a durable proposal first. Restarting the SDK
while it is answering that callback would lose its own response.
"""
import asyncio
import json
import sqlite3
import time

from .agent_session import canonical,SessionFailure
from .services.dsh_workshop import DshWorkshop


class EditJournal:
    def __init__(self,database):
        self.database = str(database)

    def get_dsh_edit(self,identity):
        with sqlite3.connect(self.database,timeout=30) as db:
            row = db.execute('SELECT v FROM meta WHERE k=?',('dsh_edit:'+identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self,identity,record):
        with sqlite3.connect(self.database,timeout=30) as db:
            db.execute('INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)',('dsh_edit:'+identity,canonical(record)))

    def add_dsh_edit(self,identity,paths,hashes,patch,reason,backup_dir,**values):
        self._save(identity,dict(id=identity,paths=paths,hashes=hashes,patch=patch,reason=reason,
            backup_dir=backup_dir,ts=time.time(),**values))

    def update_dsh_edit(self,identity,**values):
        self._save(identity,dict(self.get_dsh_edit(identity) or {},**values))


def queue_change(service,scope,proposal,identity,checksum):
    if proposal.get('target')=='worker' and proposal.get('component','dsh') not in {'dsh','media'}:
        raise SessionFailure('invalid_input','worker component 为 dsh 或 media')
    if proposal.get('target')=='dsh' and (not isinstance(proposal.get('edits'),list) or not proposal['edits']):
        raise SessionFailure('invalid_input','DSH 修改需要 edits:[{path,old,new}] 和具体验证目标')
    if proposal.get('target')=='plugin' and (proposal.get('action')!='remove' or not proposal.get('bundle')):
        raise SessionFailure('invalid_input','插件安装和发现使用原生 plugin_manager；此入口仅移除指定 bundle')
    if not proposal.get('verification_goal'):
        raise SessionFailure('invalid_input','请记录升级后需要再次验证的原目标')
    receipt = dict(change_id=identity,phase='dsh_queued',applied=False,behavior_validated=False,
                   verification_goal=proposal['verification_goal'])
    proposal = dict(proposal)
    binding = service.repo._one("SELECT session_id,intent_ref,intent_revision FROM agent_sessions WHERE scope_id=? AND status='current'",(scope.scope_id,)) if service.repo._one("SELECT 1 FROM sqlite_master WHERE name='agent_sessions'") else None
    if binding:
        proposal['_intent_ref'],proposal['_intent_revision'] = binding['intent_ref'],binding['intent_revision']
        proposal['_origin_session_id']=binding['session_id']
    with service.repo.tx():
        service.repo._exec('INSERT INTO self_changes VALUES(?,?,?,?,?,?,?,?)',
            (identity,checksum,scope.scope_id,'dsh_queued',canonical(proposal),canonical(receipt),time.time(),time.time()))
    return receipt


async def process_queued(role):
    service = role._self_change
    rows = service.repo._all("SELECT * FROM self_changes WHERE phase='dsh_queued' ORDER BY created LIMIT 1")
    if not rows:
        return
    # Shared worker changes wait until every role session is idle and has no
    # unhandled input. A new message can still enter during build; activation
    # performs the same check again on the owning event loop.
    def eligible():
        proposal=json.loads(rows[0]['manifest'])
        if proposal.get('target')=='worker' and proposal.get('component')=='media':
            media=role.local_media
            if not media or media.active or media.blocked_reason or role.repo._one("SELECT 1 FROM work_jobs WHERE kind='local_media' AND status='running'"):
                return False
        for root in role.repo._all("SELECT * FROM agent_sessions WHERE status='current'"):
            if role.sessions.pending(root['session_id']):
                return False
            last = role.repo._one("SELECT payload FROM agent_events WHERE session_id=? AND "
                "json_extract(payload,'$.type') IN ('turn/start','turn/end') ORDER BY seq DESC LIMIT 1",(root['session_id'],))
            if last and json.loads(last['payload'])['type']=='turn/start':
                return False
        return True
    if not eligible():
        return
    row = dict(rows[0])
    proposal = json.loads(row['manifest'])
    def intent_current():
        if not proposal.get('_intent_ref'):
            return True
        task = role.tasks.get(proposal['_intent_ref'],row['scope_id'])
        return bool(task and task['status']=='active' and task['revision']==proposal['_intent_revision']
                    and not role.tasks.blocker(task['ref'],row['scope_id']))
    if proposal.get('_intent_ref'):
        if not intent_current():
            with role.repo.tx():
                role.repo._exec("UPDATE self_changes SET phase='cancelled',receipt=?,updated=? WHERE id=?",
                    (canonical(dict(change_id=row['id'],phase='cancelled',applied=False,reason='原目标已改变或中止')),
                     time.time(),row['id']))
            return
    with role.repo.tx():
        role.repo._exec("UPDATE self_changes SET phase='dsh_checking',updated=? WHERE id=?",(time.time(),row['id']))
    class Workshop(DshWorkshop):
        def activate(self,timeout=240):
            async def check():
                return eligible() and intent_current()
            if not asyncio.run_coroutine_threadsafe(check(),role.loop).result(30):
                return dict(ok=False,error='input_pending: 新消息到达，本次升级不切换 worker')
            return super().activate(timeout)
    try:
        if proposal['target']=='worker' and proposal.get('component')=='media':
            result=await role.local_media.refresh()
            role.capabilities.snapshots.clear()
        elif proposal['target']=='dsh':
            shop = Workshop(role.bridge.dsh_root,EditJournal(role.repo.path),role.bridge,role.cfg)
            result = await asyncio.to_thread(shop.apply_edits,proposal['edits'],proposal.get('reason',''),True,True)
        else:
            if proposal['target']=='plugin':
                removed = await asyncio.to_thread(role.bridge.plugin_remove,str(proposal['bundle']))
                if removed['exitCode']:
                    raise RuntimeError(str(removed))
            else:
                await asyncio.to_thread(role.bridge.restart_worker)
            hello = await asyncio.to_thread(role.bridge.hello)
            result = dict(activated=True,worker_generation=hello.get('workerGeneration'),
                          tool_snapshot='pending_current_agent_recovery')
        receipt = dict(change_id=row['id'],phase='applied' if result.get('activated') else 'failed',
            applied=bool(result.get('activated')),behavior_validated=False,verification_goal=proposal['verification_goal'],
            result=result,loaded_adapter_version=getattr(role.bridge,'loaded_adapter_version',''))
    except Exception as error:
        receipt = dict(change_id=row['id'],phase='outcome_unknown',applied=False,behavior_validated=False,error=str(error))
    with role.repo.tx():
        role.repo._exec('UPDATE self_changes SET phase=?,receipt=?,updated=? WHERE id=?',
            (receipt['phase'],canonical(receipt),time.time(),row['id']))
    if not (proposal['target']=='worker' and proposal.get('component')=='media'):
        await role.recover()
    from .contracts import Scope
    binding = role.repo._one("SELECT * FROM agent_sessions WHERE scope_id=? AND status='current'",(row['scope_id'],))
    if binding:
        if receipt.get('applied') and not (proposal['target']=='worker' and proposal.get('component')=='media'):
            try:
                tools=await asyncio.to_thread(role.bridge.tools,scope=binding['session_id'])
                receipt['current_agent_snapshot']=dict(session_id=binding['session_id'],generation=binding['generation'],
                    tool_names=[t.get('name') for t in tools],original_intent_ref=proposal.get('_intent_ref'),
                    verified_use=False)
            except Exception as error:
                receipt['current_agent_snapshot']=dict(status='unavailable',error=str(error)[:500],verified_use=False)
            with role.repo.tx():
                role.repo._exec('UPDATE self_changes SET receipt=? WHERE id=?',(canonical(receipt),row['id']))
        role.schedule(Scope(**json.loads(binding['scope_json'])),proposal.get('_intent_ref'),1,
            'DSH 自我修改有新回执，请读取 self_changes，核对运行版本后回到原目标验证；不要把构建成功当成目标完成。',kind='recovery')
