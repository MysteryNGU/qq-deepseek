"""Host-owned execution attribution, one autonomous lane and persistent local guards."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .agent_session import SessionFailure,canonical,digest
from .contracts import Scope
from .text import stable_id


# These tools return their own synchronous read result and have no write effect.
READ_ONLY = {'read_file','read','glob','grep','search','web_search','web_fetch','fetch','list_directory',
    'skill','job_output'}
CONTROL = {'list_agents','wait_agent','cancel_agent','interrupt_agent','subagent_status','subagent_cancel'}
# These DSH tools own synchronous execution; their structured abort receipt
# follows settlement. An arbitrary plugin may instead have launched remote work.
SETTLED_CANCEL_TOOLS = READ_ONLY | {'pwsh','bash','edit','write','write_file','apply_patch'}
UNSTABLE = {'call_id','callId','request_id','timestamp','created_at','updated_at','checkpoint','trace_id','seed'}


def normalized(value):
    """Discard transport identity, retaining method and meaningful target arguments."""
    if isinstance(value,dict):
        return {k:normalized(v) for k,v in sorted(value.items()) if k not in UNSTABLE}
    if isinstance(value,list):
        return [normalized(v) for v in value]
    if isinstance(value,str):
        value = re.sub(r'(?i)([_-])[a-f0-9]{12,}(?=\.[a-z0-9]+$|$)',r'\1<generated>',value)
        return re.sub(r'(?i)([_-])\d{10,}(?=\.[a-z0-9]+$|$)',r'\1<generated>',value)
    return value


def request_signature(tool, arguments):
    return digest(dict(tool=tool,arguments=normalized(arguments)))


class Initiative:
    def __init__(self, role):
        self.role,self.repo = role,role.repo
        self.resource_snapshot = dict(at=0,known=False)

    @property
    def enabled(self):
        return self.role.continuity_ready

    def get(self,key):
        row = self.repo._one('SELECT v FROM meta WHERE k=?',(key,))
        return json.loads(row['v']) if row else None

    def put(self,key,value):
        value = dict(value,schema=1,version=(self.get(key) or {}).get('version',0)+1)
        self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,canonical(value)))
        return value

    def slot_key(self):
        return 'initiative:slot:'+self.role.life.agent_id

    def arguments(self,operation_id):
        row = self.repo._one('SELECT arguments FROM role_operations WHERE id=?',(operation_id,))
        if row:
            return json.loads(row['arguments']) or {}
        job = self.repo._one('SELECT payload FROM work_jobs WHERE id=?',(operation_id,))
        return json.loads(job['payload']).get('spec',{}) if job else {}

    def root_task(self,scope,ref):
        seen = set()
        task = None
        while ref:
            if ref in seen:
                raise SessionFailure('invalid_input','打算的父级存在循环')
            seen.add(ref)
            task = self.role.tasks.get(ref,scope.scope_id)
            if not task:
                raise SessionFailure('stale_context','执行所属打算已失效；请读取 intents')
            ref = task.get('parent_ref')
        return task

    def remember_intent_origin(self, scope, task, root, proposed_origin):
        key = 'initiative:intent:'+task['ref']
        if self.get(key):
            return
        parent = task.get('parent_ref')
        if parent:
            ancestor = self.root_task(scope,parent)
            inherited = self.get('initiative:intent:'+ancestor['ref']) or dict(classification='autonomous',trigger_refs=[])
        else:
            inherited = None
        trigger_refs = []
        turn = self.role.turn_number(root['session_id'])
        for eid in task.get('source_event_ids',[]):
            event = self.repo.get_event(eid)
            item = self.repo._one('SELECT state,claimed_turn FROM agent_inputs WHERE session_id=? AND input_id=?',
                (root['session_id'],eid))
            if (event and event.source_kind=='user_report' and not event.redacted_at and event.scope.scope_id==scope.scope_id
                    and item and item['state'] in {'consumed','settled'} and item['claimed_turn']==turn):
                trigger_refs.append(eid)
        classification = (inherited['classification'] if inherited else
            'interactive' if proposed_origin=='user' and trigger_refs and task.get('revision',1)==1 else 'autonomous')
        self.put(key,dict(scope_id=scope.scope_id,forget_epoch=self.repo.forget_epoch(scope.scope_id),
            classification=classification,trigger_refs=inherited['trigger_refs'] if inherited else trigger_refs))

    def attribution(self,scope,binding,call=None):
        call = call or {}
        parent = self.get('initiative:delegate:'+binding['session_id'])
        if parent:
            return parent['attribution']
        task = self.root_task(scope,binding['intent_ref']) if binding.get('intent_ref') else None
        frozen = self.get('initiative:intent:'+task['ref']) if task else None
        refs = []
        if frozen:
            mode,refs = frozen['classification'],frozen['trigger_refs']
        elif task:
            # Older tasks lack a host-frozen trigger; a later greeting cannot upgrade them.
            mode = 'autonomous'
        else:
            turn = self.role.turn_number(binding['session_id'])
            rows = self.repo._all('SELECT e.id FROM agent_inputs i JOIN events e ON e.id=i.input_id '
                "WHERE i.session_id=? AND i.claimed_turn IS ? AND e.source_kind='user_report' "
                "AND e.redacted_at IS NULL AND i.state IN ('consumed','settled') LIMIT 20",(binding['session_id'],turn))
            refs = [r['id'] for r in rows]
            mode = 'interactive' if refs else 'autonomous'
        owner = task['ref'] if task else binding['session_id']
        return dict(run_id=stable_id('initiative-run',scope.scope_id,owner,str(binding['generation']),str(binding['forget_epoch'])),
            classification=mode,owner_ref=owner,root_scope_id=scope.scope_id,root_session_id=binding['parent_session_id'] or binding['session_id'],
            generation=binding['generation'],forget_epoch=binding['forget_epoch'],trigger_refs=refs)

    def managed(self,binding):
        return bool(self.get('initiative:delegate:'+binding['session_id']) or
                    binding.get('intent_ref') and self.get('initiative:intent:'+binding['intent_ref']))

    def media_handoff(self, binding, job_id):
        turn = self.role.turn_number(binding['session_id'])
        if turn is not None:
            self.put('initiative:media_handoff:'+binding['session_id'],dict(job_id=job_id,turn=turn,
                generation=binding['generation']))

    def clear_media_handoff(self, sid):
        self.repo._exec('DELETE FROM meta WHERE k=?',('initiative:media_handoff:'+sid,))

    def interactive_busy(self, media_job=None):
        if self.repo._one("SELECT 1 FROM agent_inputs i JOIN events e ON e.id=i.input_id JOIN agent_sessions s USING(session_id) "
                "WHERE s.status='current' AND i.handled=0 AND e.source_kind='user_report' AND e.redacted_at IS NULL LIMIT 1"):
            return True
        for row in self.repo._all("SELECT session_id FROM agent_sessions WHERE status='current' AND parent_session_id IS NULL"):
            sid = row['session_id']
            latest = self.repo._one("SELECT payload FROM agent_events WHERE session_id=? AND "
                "json_extract(payload,'$.type') IN ('turn/start','turn/end') ORDER BY seq DESC LIMIT 1",(sid,))
            if latest:
                value = json.loads(latest['payload'])
                if value['type']=='turn/start' and self.repo._one('SELECT 1 FROM agent_inputs i JOIN events e ON e.id=i.input_id '
                        "WHERE i.session_id=? AND i.claimed_turn=? AND e.source_kind='user_report' AND e.redacted_at IS NULL LIMIT 1",
                        (sid,value.get('data',{}).get('turn'))):
                    handoff = self.get('initiative:media_handoff:'+sid) or {}
                    binding = self.role.sessions.require(sid)
                    if (media_job and handoff.get('job_id') == media_job and handoff.get('turn') == value.get('data',{}).get('turn')
                            and handoff.get('generation') == binding['generation']):
                        continue
                    return True
        return False

    def wait(self,scope,attribution,reason):
        slot = self.get(self.slot_key()) or {}
        raise SessionFailure('resource_wait','后台执行尚未释放或交互轮尚未结束；已登记等待。仍可聊天、使用已声明的只读工具核对或取消',
            reason=reason,execution_phase=slot.get('phase','idle'))

    def member_allowed(self,slot,attribution,sid):
        if slot and slot.get('members'):
            if slot['run_id']!=attribution['run_id'] or slot['phase']=='outcome_unknown':
                return False
            for member in slot['members'].values():
                if member['kind']!='delegate':
                    return False
                # A delegate's own sequential tools share its existing lane.
                current = sid
                ancestors = set()
                while current and current not in ancestors:
                    ancestors.add(current)
                    link = self.get('delegate_parent:'+current)
                    current = link if isinstance(link,str) else None
                if member['session_id'] not in ancestors:
                    return False
        return True

    def reserve(self,scope,attribution,member_id,kind,sid):
        if attribution['classification']!='autonomous':
            return
        slot = self.get(self.slot_key())
        if self.interactive_busy(member_id if kind=='job' else None):
            self.wait(scope,attribution,'interactive_turn')
        if not self.member_allowed(slot,attribution,sid):
            self.wait(scope,attribution,'execution_busy')
        value = dict(slot or {},**attribution,phase='reserved',updated_at=time.time())
        value['members'] = dict((slot or {}).get('members',{}))
        value['members'][member_id] = dict(kind=kind,session_id=sid,phase='running')
        self.put(self.slot_key(),value)

    def conditions(self,attribution,tool,arguments):
        args = normalized(arguments or {})
        target = {k:args[k] for k in ('path','file_path','url','target','asset_id','voice_id','job_id') if k in args} if isinstance(args,dict) else {}
        file_version = None
        file_path = args.get('file_path',args.get('path')) if isinstance(args,dict) else None
        if isinstance(file_path,str) and not file_path.startswith(('\\\\','//')):
            path = (self.role.root/file_path).resolve()
            target = {k:v for k,v in target.items() if k not in {'path','file_path'}}
            target['file_path']=str(path).casefold() if os.name=='nt' else str(path)
            if path==self.role.root or self.role.root in path.parents:
                try:
                    info = path.stat()
                    file_version = [info.st_mtime_ns,info.st_size]
                except OSError:
                    file_version = 'missing'
        media_revision=None
        local=self.role.local_media
        if tool=='life_media' and local and isinstance(args,dict):
            provider_id=args.get('_adapter',{}).get('provider_id') or local.adapters.defaults.get(args.get('kind'))
            provider=local.adapters.providers.get(provider_id)
            media_revision=digest(provider) if provider else 'provider_unavailable'
        return dict(forget_epoch=attribution['forget_epoch'],target=digest(target),file_version=file_version,
            capability=str(getattr(self.role.bridge,'loaded_adapter_version','')),
            media_config=media_revision or (digest({k:v for k,v in getattr(self.role.cfg,'local_media',{}).items()
                if k.startswith('voice_' if isinstance(args,dict) and args.get('kind')=='speech' else 'image_')
                or k in {'timeout_seconds','worker_threads'}}) if tool=='life_media' else None))

    def guard_key(self,attribution,tool,arguments):
        target = self.conditions(attribution,tool,arguments)['target']
        # Shell commands have no structured file/url target. Keep their unknown
        # outcomes separate so one interrupted command does not block all commands.
        if target==digest({}):
            target = request_signature(tool,arguments)
        owner = stable_id('guard-owner',attribution['owner_ref'],tool,target)
        return 'initiative:guard:'+attribution['root_scope_id']+':'+owner

    def guard_allows(self,attribution,tool,arguments,operation_id):
        key = self.guard_key(attribution,tool,arguments)
        signature = request_signature(tool,arguments)
        guard = self.get(key)
        # A declared changed-method probe may address another tool in the same root matter.
        for row in self.repo._all("SELECT k,v FROM meta WHERE k LIKE 'initiative:guard:%' "
                "AND json_extract(v,'$.scope_id')=? AND json_extract(v,'$.owner_ref')=?",
                (attribution['root_scope_id'],attribution['owner_ref'])):
            candidate = json.loads(row['v'])
            # Older guards grouped targetless commands together. Keep protection
            # for the original request while allowing a different diagnostic.
            if candidate['phase']=='outcome_unknown':
                prior = self.get('initiative:operation:'+candidate.get('last_operation_ref','')) or {}
                if prior.get('request_hash')==signature:
                    raise SessionFailure('outcome_unknown','原操作的效果尚未核对，不能重放；可使用其他只读工具检查',guard_ref=row['k'])
            if candidate.get('probe',{}).get('request_hash')==signature:
                key,guard = row['k'],candidate
                break
        if not guard:
            return key
        current = self.conditions(attribution,guard['tool'],self.arguments(guard.get('condition_operation_ref')))
        if guard['phase']=='outcome_unknown':
            raise SessionFailure('outcome_unknown','原操作可能已产生效果；先读取 continuity 活动并核对原结果，不能重放',guard_ref=key)
        if guard.get('relevant_versions')!=current:
            guard.update(count=0,phase='active',relevant_versions=current,probe={})
            self.put(key,guard)
        if guard['phase']=='suspended':
            probe = guard.get('probe') or {}
            if probe.get('request_hash')!=signature or probe.get('consumed'):
                raise SessionFailure('retry_suspended','同条件连续失败已暂停；用 life_read(kind=continuity) 查看保护，相关条件变化或声明一次实质不同的 retry_probe 后再试',
                    guard_ref=key,base_version=guard['version'])
            probe.update(consumed=True,operation_id=operation_id)
            guard['probe'] = probe
            self.put(key,guard)
        return key

    def begin_operation(self,scope,binding,call,operation_id):
        attribution = self.attribution(scope,binding,call)
        guard_key = self.guard_allows(attribution,call['tool'],call.get('arguments'),operation_id)
        effect = self.role.capabilities.policy(call['tool'],call.get('tool_identity'))['effect']
        if call['tool'] not in CONTROL and effect!='read':
            self.reserve(scope,attribution,operation_id,'operation',binding['session_id'])
        self.put('initiative:operation:'+operation_id,dict(attribution=attribution,scope_id=scope.scope_id,
            guard_ref=guard_key,tool=call['tool'],request_hash=request_signature(call['tool'],call.get('arguments')),
            effect=effect,
            call_id=call['call_id'],
            parent_operation_id=call.get('direct_parent_call_id')))
        return attribution

    def record_result(self,operation_id,receipt_ref,*,failure=None,unknown=False,progress=False,cancelled=False):
        op = self.get('initiative:operation:'+operation_id)
        if not op:
            return
        key = op['guard_ref']
        if not self.repo.claim_effect(receipt_ref,'initiative_guard',key):
            return
        attribution = op['attribution']
        if attribution['forget_epoch']!=self.repo.forget_epoch(attribution['root_scope_id']):
            return
        arguments = self.arguments(operation_id)
        versions = self.conditions(attribution,op['tool'],arguments)
        guard = self.get(key) or dict(scope_id=attribution['root_scope_id'],owner_ref=attribution['owner_ref'],
            count=0,phase='active',probe={},relevant_versions=versions,tool=op['tool'],condition_operation_ref=operation_id)
        probing = guard.get('probe',{}).get('operation_id')==operation_id
        if probing:
            versions = guard['relevant_versions']
        if unknown:
            guard.update(phase='outcome_unknown',resume_condition='核对原操作的实际结果')
        elif failure:
            fingerprint = digest(dict(owner=attribution['owner_ref'],tool=op['tool'],code=failure,versions=versions))
            count = guard['count']+1 if guard.get('fingerprint')==fingerprint else 1
            probe_failed = probing
            guard.update(fingerprint=fingerprint,count=count,
                phase='suspended' if probe_failed or count>=self.role.resources.autonomous_no_progress_limit else 'active',
                relevant_versions=versions,last_request_hash=op['request_hash'],
                condition_operation_ref=guard['condition_operation_ref'] if probing else operation_id,
                resume_condition='相关条件发生真实变化，或一次有实质差别的方法探测')
        elif progress or cancelled:
            guard.update(count=0,phase='active',relevant_versions=versions)
        guard['last_receipt_ref'] = receipt_ref
        guard['last_operation_ref'] = operation_id
        self.put(key,guard)
        if guard['phase']=='suspended':
            self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND intent_ref=? "
                "AND state IN ('pending','waiting_resource') AND kind IN ('task_due','recovery')",
                (attribution['root_scope_id'],attribution['owner_ref']))

    def finish_member(self,member_id,*,terminal=True):
        slot = self.get(self.slot_key())
        if not slot or member_id not in slot.get('members',{}):
            return
        if not terminal:
            slot['phase']='outcome_unknown'
            slot['members'][member_id]['phase']='outcome_unknown'
        else:
            del slot['members'][member_id]
            slot['phase']='running' if slot['members'] else 'idle'
        slot['updated_at']=time.time()
        self.put(self.slot_key(),slot)
        if not slot['members']:
            self.notify_one_waiter()

    def started(self,operation_id):
        slot = self.get(self.slot_key())
        if slot and operation_id in slot.get('members',{}) and slot['phase']=='reserved':
            slot['phase']='running'
            slot['updated_at']=time.time()
            self.put(self.slot_key(),slot)

    def cancellation(self,operation,data):
        """Only structured runtime receipts establish cancellation, never output text."""
        error = data.get('error')
        message = data.get('message',{})
        if not isinstance(error,dict) or not message.get('isError') or error.get('name')!='AbortError':
            return None
        code = error.get('code')
        if code=='ABORTED_BEFORE_DISPATCH':
            return code
        if code=='ABORTED' and operation['tool'] in SETTLED_CANCEL_TOOLS:
            return code
        return None

    def record_cancellation(self,operation,data,receipt_ref):
        code = self.cancellation(operation,data)
        metadata = self.get('initiative:operation:'+operation['id'])
        if not code or not metadata or metadata.get('awaiting_async'):
            return None
        self.put('initiative:operation:'+operation['id'],dict(metadata,
            execution_state='settled',execution_code=code,execution_receipt_ref=receipt_ref))
        if code=='ABORTED_BEFORE_DISPATCH':
            guard = self.get(metadata['guard_ref'])
            if guard and guard.get('last_operation_ref')==operation['id'] and guard['phase']=='outcome_unknown':
                guard.update(phase='active',count=0,resume_condition='原调用未派发；可以重新选择行动')
                self.put(metadata['guard_ref'],guard)
        return code

    def preflight_failure(self,operation,data):
        """Native file guards reject before mutation; arbitrary error text proves nothing."""
        error = data.get('error')
        message = data.get('message',{})
        if operation['tool'] not in {'write','edit'} or not isinstance(error,dict) or not isinstance(message,dict):
            return None
        call = message.get('toolCallId') or data.get('callId')
        if (message.get('isError') is True and error.get('name')=='FsError'
                and error.get('code') in {'FS_NOT_OBSERVED','FS_STALE_VERSION'} and call
                and stable_id('roleop',operation['session_id'],str(call))==operation['id']):
            return error['code']
        return None

    def record_preflight_failure(self,operation,code,receipt_ref):
        metadata = self.get('initiative:operation:'+operation['id'])
        if metadata:
            self.put('initiative:operation:'+operation['id'],dict(metadata,awaiting_async=False,
                execution_state='settled',execution_code=code,execution_receipt_ref=receipt_ref))

    def observe_tool(self,operation,event):
        data = event.get('data',{})
        message = data.get('message',{})
        tool = operation['tool']
        content = message.get('content') or []
        value = data.get('value')
        if value is None:
            for block in content if isinstance(content,list) else []:
                if isinstance(block,dict) and block.get('type')=='text':
                    try:
                        value = json.loads(block.get('text',''))
                    except (ValueError,TypeError):
                        continue
        is_error = bool(message.get('isError'))
        explicit_failure = isinstance(value,dict) and value.get('ok') is False
        preflight = self.preflight_failure(operation,data)
        failure = preflight
        declared=self.get('initiative:operation:'+operation['id']) or {}
        read_only=declared.get('effect',self.role.capabilities.policy(tool)['effect'])=='read'
        cancellation = self.cancellation(operation,data)
        if read_only and (is_error or explicit_failure) and not cancellation:
            body = canonical(value if value is not None else content)
            code = value.get('code') if isinstance(value,dict) else None
            failure = str(code) if code else 'error:'+digest(normalized(re.sub(r'\b\d{10,}\b','<time>',body)))[:24]
        unknown = bool((is_error or explicit_failure) and not read_only and not preflight
                       and cancellation!='ABORTED_BEFORE_DISPATCH')
        op_ref = operation['id']
        receipt_ref = operation['session_id']+':'+str(event['seq'])
        child = self.repo._one("SELECT k,v FROM meta WHERE k LIKE 'initiative:delegate:%' "
            "AND json_extract(v,'$.parent_operation_id')=? LIMIT 1",(op_ref,))
        pending = not read_only and not is_error and not explicit_failure and (tool=='subagent' or isinstance(value,dict) and (
            value.get('status') in {'pending','running','accepted','queued'} or
            (value.get('job_id') or value.get('subagentId')) and value.get('status') not in
            {'done','completed','failed','cancelled'} and value.get('settled') is not True))
        child_finished = child and json.loads(child['v']).get('terminal_receipt')
        unknown = unknown or bool(pending and not child)
        self.record_result(op_ref,receipt_ref,failure=failure,unknown=unknown,
                           progress=not is_error and not explicit_failure and read_only)
        if preflight:
            self.record_preflight_failure(operation,preflight,receipt_ref)
        metadata = self.get('initiative:operation:'+op_ref)
        if metadata and isinstance(value,dict):
            metadata['coverage'] = 'partial' if value.get('truncated') or value.get('coverage')=='partial' else 'reported'
            self.put('initiative:operation:'+op_ref,metadata)
        settled_cancel = self.record_cancellation(operation,data,receipt_ref) if not pending else None
        if child:
            self.finish_member(op_ref)
        elif settled_cancel:
            self.finish_member(op_ref)
        elif pending or unknown:
            self.put('initiative:operation:'+op_ref,dict(self.get('initiative:operation:'+op_ref),awaiting_async=bool(pending)))
            self.finish_member(op_ref,terminal=False)
        else:
            self.finish_member(op_ref)
        return ('unknown' if unknown else 'cancelled') if settled_cancel else (
            'failed' if failure else 'unknown' if unknown else 'dispatched' if pending and not child_finished else 'succeeded')

    def untracked_tool_status(self,operation,data):
        """Classify proved failures even when initiative tracking is disabled."""
        if self.preflight_failure(operation,data):
            return 'failed'
        message = data.get('message',{})
        value = data.get('value')
        for block in message.get('content',[]) if value is None else []:
            if block.get('type')=='text':
                try:
                    value = json.loads(block.get('text',''))
                except (ValueError,TypeError):
                    continue
        failed = bool(message.get('isError') or isinstance(value,dict) and value.get('ok') is False)
        code = self.cancellation(operation,data)
        if code=='ABORTED_BEFORE_DISPATCH':
            return 'cancelled'
        declared = self.get('initiative:operation:'+operation['id']) or {}
        if declared.get('effect',self.role.capabilities.policy(operation['tool'])['effect'])=='read':
            return 'cancelled' if code=='ABORTED' else 'failed' if failed else 'succeeded'
        return 'unknown' if failed else 'succeeded'

    def reconcile_read_results(self):
        """Repair matched read failures and native preflight refusals without replaying."""
        count = 0
        with self.repo.tx():
            for op in self.repo._all("SELECT * FROM role_operations WHERE status='unknown' AND result IS NOT NULL"):
                if op['scope_id'].split('|',1)[0]!=self.role.life.agent_id:
                    continue
                declared = self.get('initiative:operation:'+op['id']) or {}
                try:
                    data = json.loads(op['result'])
                except (ValueError,TypeError):
                    continue
                if not isinstance(data,dict):
                    continue
                message = data.get('message',{})
                if not isinstance(message,dict):
                    continue
                call = message.get('toolCallId') or data.get('callId')
                if not call or stable_id('roleop',op['session_id'],str(call))!=op['id']:
                    continue
                # Native cancellation proves that execution stopped, not that a
                # write had no effect. Release its lane while retaining unknown
                # outcome and the operation's replay guard.
                cancellation = self.record_cancellation(op,data,'reconciled-cancellation:'+op['id']) if declared else None
                if cancellation:
                    self.finish_member(op['id'])
                    if cancellation=='ABORTED_BEFORE_DISPATCH':
                        self.repo._exec("UPDATE role_operations SET status='cancelled' WHERE id=?",(op['id'],))
                        count += 1
                        continue
                preflight = self.preflight_failure(op,data)
                read_only = declared.get('effect',self.role.capabilities.policy(op['tool'])['effect'])=='read'
                if not read_only and not preflight:
                    continue
                status = self.untracked_tool_status(op,data)
                if status in {'failed','cancelled'}:
                    self.repo._exec('UPDATE role_operations SET status=? WHERE id=?',(status,op['id']))
                    if declared:
                        declared['awaiting_async'] = False
                        self.put('initiative:operation:'+op['id'],declared)
                        receipt_ref = 'reconciled-tool:'+op['id']
                        self.record_result(op['id'],receipt_ref,failure=preflight or 'recorded_read_failure')
                        if preflight:
                            self.record_preflight_failure(op,preflight,receipt_ref)
                        self.finish_member(op['id'])
                    count += 1
        return count

    def attach_delegate(self,scope,parent_id,child_id,parent_call_id,activation_id=None):
        op_id = stable_id('roleop',parent_id,str(parent_call_id)) if parent_call_id else None
        op = self.get('initiative:operation:'+op_id) if op_id else None
        if not op or op['scope_id']!=scope.scope_id or op['tool'] not in {'subagent','send_message'}:
            raise SessionFailure('outcome_unknown','子执行缺少稳定父调用归属；先核对原委派，不猜测最后一个 subagent 调用')
        old = self.get('initiative:delegate:'+child_id)
        if old:
            if old.get('parent_operation_id')!=op_id:
                target = self.arguments(op_id).get('agent_id')
                if not activation_id or op['tool']!='send_message' or target!=child_id or not old.get('terminal_receipt'):
                    raise SessionFailure('scope_mismatch','子执行已绑定另一在途调用；先核对原执行终态')
            else:
                if activation_id:
                    self.put('initiative:delegate:'+child_id,dict(old,activation_id=activation_id))
                return self.repo._one('SELECT intent_ref,intent_revision FROM role_operations WHERE id=?',(op_id,))
        attr = op['attribution']
        self.put('initiative:delegate:'+child_id,dict(scope_id=scope.scope_id,parent_operation_id=op_id,
            activation_id=activation_id,attribution=attr))
        slot = self.get(self.slot_key())
        if attr['classification']=='autonomous':
            if not slot or slot['run_id']!=attr['run_id'] or op_id not in slot.get('members',{}):
                raise SessionFailure('outcome_unknown','原委派执行名额无法核对；保留未知，不新开后台工作')
            del slot['members'][op_id]
            slot['members']['delegate:'+child_id] = dict(kind='delegate',phase='running',session_id=child_id)
            slot['phase']='running'
            self.put(self.slot_key(),slot)
        return self.repo._one('SELECT intent_ref,intent_revision FROM role_operations WHERE id=?',(op_id,))

    def delegate_finished(self,child_id,receipt):
        row = self.get('initiative:delegate:'+child_id)
        if row and row.get('activation_id') and receipt.get('runId')!=row['activation_id']:
            return False
        if receipt.get('status') not in {'completed','cancelled','failed','succeeded','error'}:
            self.finish_member('delegate:'+child_id,terminal=False)
            return False
        if row:
            self.put('initiative:delegate:'+child_id,dict(row,terminal_receipt=receipt.get('status')))
            self.repo._exec("UPDATE role_operations SET status=? WHERE id=?",
                ('cancelled' if receipt.get('status')=='cancelled' else 'succeeded',row['parent_operation_id']))
        self.finish_member('delegate:'+child_id)
        return True

    def prepare_job(self,scope,root,proposal):
        attribution = self.attribution(scope,root)
        if attribution['classification']=='autonomous':
            prior = self.repo._one("SELECT id FROM work_jobs WHERE kind='local_media' AND scope_id=? "
                "AND status IN ('pending','running','waiting') AND json_extract(payload,'$.initiative.run_id')=? LIMIT 1",
                (scope.scope_id,attribution['run_id']))
            if prior:
                return attribution,prior['id']
        return attribution,None

    def begin_job(self,scope,jid,payload):
        attr = payload.get('initiative')
        if not attr:
            if not self.enabled:
                return
            # An older frozen job has no trustworthy interactive trigger.
            # Adopt it at admission without changing its source, seed or media specification.
            binding = dict(self.role.sessions.binding(scope),intent_ref=payload.get('intent_ref'))
            attr = dict(self.attribution(scope,binding),classification='autonomous',trigger_refs=[],
                        classification_basis='legacy_job_without_host_origin')
            payload['initiative'] = attr
            self.repo._exec('UPDATE work_jobs SET payload=? WHERE id=? AND scope_id=?',
                (canonical(payload),jid,scope.scope_id))
        if attr['forget_epoch']!=self.repo.forget_epoch(scope.scope_id):
            raise SessionFailure('source_removed','生成任务所属来源已遗忘')
        try:
            root = self.role.sessions.require(attr['root_session_id'])
        except SessionFailure as error:
            if error.code!='stale_context':
                raise
            old = self.repo._one('SELECT scope_id,forget_epoch FROM agent_sessions WHERE session_id=?',
                (attr['root_session_id'],))
            job = self.repo._one("SELECT status FROM work_jobs WHERE id=? AND kind='local_media' AND scope_id=?",
                (jid,scope.scope_id))
            if (not old or old['scope_id']!=scope.scope_id or old['forget_epoch']!=attr['forget_epoch']
                    or not job or job['status'] not in {'pending','running','waiting'}):
                raise
            root = self.role.sessions.binding(scope)
            # A queued job outlives its model session. Keep its request, origin,
            # owner and run id, so rebinding cannot bypass retry guards or holds.
            attr = dict(attr,adopted_from_session_id=attr['root_session_id'],
                root_session_id=root['session_id'],generation=root['generation'])
            payload['initiative'] = attr
            self.repo._exec('UPDATE work_jobs SET payload=? WHERE id=?',(canonical(payload),jid))
        if root['generation']!=attr['generation']:
            raise SessionFailure('stale_context','生成任务的根执行已变化')
        task = self.role.tasks.get(attr['owner_ref'],scope.scope_id)
        if task and self.role.tasks.blocker(task['ref'],scope.scope_id):
            raise SessionFailure('cancel_requested','生成所属事项已暂停、取消或等待答复')
        if attr['classification']=='autonomous' and not self.enabled:
            raise SessionFailure('retry_suspended','连续性增强已关闭；已排队的自发任务保留，等待明确恢复')
        signature = request_signature('life_media',payload['spec'])
        key = self.guard_allows(attr,'life_media',payload['spec'],jid)
        self.reserve(scope,attr,jid,'job',root['session_id'])
        self.put('initiative:operation:'+jid,dict(attribution=attr,scope_id=scope.scope_id,guard_ref=key,
            tool='life_media',request_hash=signature))

    def finish_job(self,jid,status,error='',*,stopped=True):
        if not self.get('initiative:operation:'+jid):
            return
        self.record_result(jid,'media-terminal:'+jid,failure=error if status=='failed' else None,
            unknown=status=='unknown',progress=status=='done')
        self.finish_member(jid,terminal=stopped)

    def request_probe(self,scope,task,proposal):
        if not isinstance(proposal,dict) or set(proposal)-{'guard_ref','base_version','change_note','changed_source_refs','candidate'}:
            raise SessionFailure('invalid_input','retry_probe 参数无效')
        key = proposal.get('guard_ref','')
        if not isinstance(key,str) or not key.startswith('initiative:guard:'+scope.scope_id+':'):
            raise SessionFailure('scope_mismatch','保护记录不属于本会话')
        guard = self.get(key)
        root = self.root_task(scope,task['ref'])
        if not guard or guard['owner_ref']!=root['ref'] or guard['version']!=proposal.get('base_version'):
            raise SessionFailure('stale_context','保护或事项版本已变化；请重新读取 continuity')
        if guard['phase']=='outcome_unknown':
            raise SessionFailure('outcome_unknown','先核对原写入结果，不能用探测重放未知效果')
        if self.role.tasks.blocker(task['ref'],scope.scope_id):
            raise SessionFailure('cancel_requested','事项已暂停、取消或等待答复；技术探测不能恢复它')
        candidate = proposal.get('candidate')
        if (not isinstance(candidate,dict) or set(candidate)!={'tool','arguments'} or
                not isinstance(candidate['tool'],str) or not candidate['tool'] or
                not isinstance(proposal.get('change_note'),str) or not proposal['change_note'].strip()):
            raise SessionFailure('invalid_input','探测需要 change_note 和 candidate:{tool,arguments}')
        self.role.continuity.evidence(scope,proposal.get('changed_source_refs',[]))
        candidate_arguments = candidate['arguments']
        if candidate['tool']=='life_media':
            if not self.role.local_media or not isinstance(candidate_arguments,dict):
                raise SessionFailure('invalid_input','媒体探测需要当前媒体接口和实际生成参数')
            candidate_arguments = self.role.local_media.prepare(scope,candidate_arguments.get('proposal',candidate_arguments))
        signature = request_signature(candidate['tool'],candidate_arguments)
        attr = self.attribution(scope,self.role.sessions.binding(scope))
        current = self.conditions(attr,guard['tool'],self.arguments(guard.get('condition_operation_ref')))
        conditions_changed = current!=guard['relevant_versions']
        if signature==guard.get('last_request_hash') and not conditions_changed:
            raise SessionFailure('retry_suspended','候选方法与失败请求没有实质差异；改调用编号、时间或随机后缀不算修复')
        condition_signature = digest(current)
        previous = guard.get('probe') or {}
        if previous.get('condition_signature')==condition_signature:
            if not previous.get('consumed') and previous.get('request_hash')==signature:
                return dict(guard_ref=key,version=guard['version'],probe='duplicate')
            raise SessionFailure('retry_suspended','同一条件仅有一次方法探测，已申请或消费；等待真实相关条件变化')
        guard['probe'] = dict(condition_signature=condition_signature,request_hash=signature,consumed=False)
        guard['relevant_versions'] = current
        value = self.put(key,guard)
        if candidate['tool']=='life_media':
            for row in self.repo._all("SELECT id,payload FROM work_jobs WHERE scope_id=? AND kind='local_media' AND status='waiting'",
                    (scope.scope_id,)):
                payload = json.loads(row['payload'])
                if payload.get('initiative',{}).get('owner_ref')==root['ref'] and request_signature('life_media',payload['spec'])==signature:
                    self.repo._exec("UPDATE work_jobs SET status='pending',due=0 WHERE id=?",(row['id'],))
                    self.role.local_media.wake.set()
        return dict(guard_ref=key,version=value['version'],probe='ready',request_hash=signature)

    def guards(self,scope):
        return [dict(ref=r['k'],**json.loads(r['v'])) for r in self.repo._all("SELECT k,v FROM meta "
            "WHERE k LIKE 'initiative:guard:%' AND json_extract(v,'$.scope_id')=? ORDER BY rowid DESC LIMIT 20",(scope.scope_id,))]

    def protocol_result(self,scope,sid,receipt_ref):
        key = 'initiative:guard:'+scope.scope_id+':protocol:'+sid
        pending = [i['input_id'] for i in self.role.sessions.pending(sid)
                   if self.repo.get_event(i['input_id']).source_kind=='user_report']
        prior = self.get(key) or dict(scope_id=scope.scope_id,owner_ref='protocol:'+sid,count=0,phase='active',remaining=pending)
        if not self.repo.claim_effect(receipt_ref,'initiative_protocol',key):
            return prior['phase']!='suspended'
        if set(pending)<set(prior.get('remaining',[])):
            prior['count']=0
        prior.update(count=prior['count']+1,remaining=pending,last_receipt_ref=receipt_ref)
        if prior['count']>=self.role.resources.autonomous_no_progress_limit:
            prior['phase']='suspended'
            self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND kind='recovery' AND state='pending'",(scope.scope_id,))
        self.put(key,prior)
        return prior['phase']!='suspended'

    def protocol_completed(self,scope_id,sid):
        key = 'initiative:guard:'+scope_id+':protocol:'+sid
        state = self.get(key)
        if state and not self.role.sessions.pending(sid):
            state.update(count=0,phase='active',remaining=[])
            self.put(key,state)

    def new_input(self,event):
        if event.source_kind!='user_report':
            return
        for row in self.repo._all("SELECT k,v FROM meta WHERE k LIKE 'initiative:guard:%' "
                "AND json_extract(v,'$.scope_id')=? AND json_extract(v,'$.owner_ref') LIKE 'protocol:%'",(event.scope.scope_id,)):
            guard = json.loads(row['v'])
            if guard.get('new_input_ref')!=event.id:
                guard.update(phase='active',count=min(guard['count'],self.role.resources.autonomous_no_progress_limit-1),new_input_ref=event.id)
                self.put(row['k'],guard)

    def notify_one_waiter(self):
        if not self.enabled:
            return
        slot = self.get(self.slot_key())
        if slot and slot.get('members') or self.interactive_busy():
            return
        self.role.attention.resource_ready_one()

    def reconcile(self):
        """Only persisted terminal evidence releases members; time alone never does."""
        self.reconcile_read_results()
        slot = self.get(self.slot_key())
        if not slot or not slot.get('members'):
            return
        for ref,member in list(slot['members'].items()):
            if member['kind']=='job':
                job = self.repo._one('SELECT status,error FROM work_jobs WHERE id=?',(ref,))
                if job and job['status'] in {'done','failed','cancelled'} and job['error']!='stop_unknown':
                    self.finish_member(ref)
                    continue
            elif member['kind']=='operation':
                op = self.repo._one('SELECT * FROM role_operations WHERE id=?',(ref,))
                metadata = self.get('initiative:operation:'+ref) or {}
                if metadata.get('execution_state')=='settled' and metadata.get('execution_receipt_ref') and not metadata.get('awaiting_async'):
                    self.finish_member(ref)
                    continue
                if op and op['status']=='unknown' and op['result']:
                    data = json.loads(op['result'])
                    metadata = self.get('initiative:operation:'+ref) or {}
                    call_id = data.get('message',{}).get('toolCallId') or data.get('callId')
                    if call_id and call_id==metadata.get('call_id'):
                        code = self.record_cancellation(op,data,'operation-result:'+ref)
                        if code:
                            if code=='ABORTED_BEFORE_DISPATCH':
                                self.repo._exec("UPDATE role_operations SET status='cancelled' WHERE id=?",(ref,))
                            self.finish_member(ref)
                            continue
                if op and op['status'] in {'succeeded','failed','cancelled'}:
                    attribution = self.get('initiative:operation:'+ref) or {}
                    if attribution.get('awaiting_async'):
                        self.finish_member(ref,terminal=False)
                        continue
                    child = self.repo._one("SELECT 1 FROM meta WHERE k LIKE 'initiative:delegate:%' AND json_extract(v,'$.parent_operation_id')=?",(ref,))
                    if op['tool']!='subagent' or child:
                        self.finish_member(ref)
                        continue
            elif member['kind']=='delegate':
                sid = member['session_id']
                ownership = self.get('initiative:delegate:'+sid) or {}
                if ownership.get('terminal_receipt'):
                    self.finish_member(ref)
                    continue
            self.finish_member(ref,terminal=False)
