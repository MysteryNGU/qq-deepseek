"""Bot-owned consultation admission and durable result adoption; cognition stays in the role."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time

from scripts.codex_consult import Bridge, ConsultError, TERMINAL, canonical, sha
from .agent_session import SessionFailure, digest
from .contracts import Scope
from .text import stable_id

log = logging.getLogger(__name__)


class Consult:
    def __init__(self, role):
        self.role, self.repo = role, role.repo
        configured = getattr(role.cfg, 'consult_config', '')
        self.bridge = Bridge((role.root/configured).resolve()) if configured else None
        self.task = None

    @property
    def enabled(self):
        return bool(self.bridge and self.bridge.cfg['enabled'])

    def require(self):
        if self.bridge is None:
            raise SessionFailure('unavailable', '尚未配置 Codex 咨询')
        return self.bridge

    def scope(self, scope):
        if scope.conversation_id != 'qq:private:'+str(self.role.cfg.owner) or scope.participant_id != str(self.role.cfg.owner):
            raise SessionFailure('scope_not_supported', '首版 Codex 咨询仅用于主人私聊')

    async def handle(self, scope, root, call):
        self.scope(scope)
        if call['session_id'] != root['session_id']:
            raise SessionFailure('scope_mismatch', '咨询由主角色亲自提出')
        bridge = self.require()
        proposal = call.get('proposal') or {}
        if not isinstance(proposal, dict):
            raise SessionFailure('invalid_input', '咨询参数必须为对象')
        action = proposal.get('action', 'ask')
        try:
            if call['kind'] == 'consult_bind':
                record = next((r for r in bridge.records() if r['request_id'] == call.get('request_id')), None)
                if not record or record['operation_id'] != stable_id('roleop', call['session_id'], call['call_id']):
                    raise SessionFailure('scope_mismatch', '作业不属于原咨询调用')
                # Recheck the actual user's latest input and intent immediately before start permission.
                self.role.commit_eligibility(root, call)
                return dict(ok=True, **bridge.bind_job(call['request_id'], scope.scope_id, call['job_id']))
            if call['kind'] == 'consult_settle':
                bridge.status(call['request_id'], scope.scope_id)
                return dict(ok=True, **bridge.settle(call['request_id'], stopped=call.get('stopped') is True, exit_code=call.get('exit_code')))
            if action in {'status', 'read', 'cancel'}:
                request_id = proposal.get('request_id')
                if action == 'status':
                    return dict(ok=True, **bridge.status(request_id, scope.scope_id))
                if action == 'cancel':
                    return dict(ok=True, **bridge.cancel(request_id, scope.scope_id))
                return dict(ok=True, **bridge.read(request_id, scope.scope_id, proposal.get('cursor'), proposal.get('limit_bytes', 8192)))
            if action == 'close':
                return dict(ok=True, **bridge.close_topic(proposal.get('topic_id'), scope.scope_id))
            if action != 'ask':
                raise SessionFailure('invalid_input', '咨询操作为 ask、status、read、cancel、close')
            if not self.enabled or getattr(self.role, 'consult_protocol', None) != 1:
                raise SessionFailure('unavailable', 'Codex 咨询尚未启用或 worker 协议未匹配')
            # Input acknowledgement is a durable operation independent of dispatch.
            with self.repo.tx():
                self.role.resolve(root, proposal)
                self.role.commit_eligibility(root, call)
            if proposal.get('intent_ref') and proposal['intent_ref'] != root['intent_ref']:
                raise SessionFailure('scope_mismatch', '咨询须沿用当前事项')
            sources = self.role._sources(scope, proposal, root)
            if proposal.get('source_event_ids'):
                for event_id in proposal['source_event_ids']:
                    event = self.repo.get_event(event_id)
                    if not event or event.scope.scope_id != scope.scope_id or event.redacted_at:
                        raise SessionFailure('scope_mismatch', '咨询来源不属于当前范围')
                sources = list(dict.fromkeys([*sources, *proposal['source_event_ids']]))
            origin = 'owner_relay' if any((event := self.repo.get_event(eid)) and event.source_kind == 'user_report'
                and event.actor_id == str(self.role.cfg.owner) for eid in sources) else 'bot_self'
            operation = stable_id('roleop', call['session_id'], call['call_id'])
            binding = dict(operation_id=operation, dsh_session_id=call['session_id'], generation=root['generation'],
                scope_json=root['scope_json'], forget_epoch=self.repo.forget_epoch(scope.scope_id), intent_ref=root['intent_ref'],
                intent_revision=root['intent_revision'], origin=origin, source_event_ids=sources)
            result = await asyncio.to_thread(bridge.prepare, scope.scope_id, proposal, binding)
            if result['launch']:
                try:
                    with self.repo.tx():
                        self.role.commit_eligibility(root, call)
                        from .budget import Budget
                        if not Budget(type('Store', (), {'repo': self.repo})(), self.role.cfg).reserve(operation):
                            raise SessionFailure('resource_wait', '当前行动预算已用完')
                        if self.role.continuity_ready:
                            identity = dict(package_id='qq-codex-consult', version='1', policy=dict(effect='write',
                                principal_adoption=False, audience='owner', resource='external'))
                            self.role.initiative.begin_operation(scope, root, dict(call, tool='life_consult', arguments=proposal, tool_identity=identity), operation)
                        self.repo._exec('INSERT INTO role_operations(id,session_id,scope_id,intent_ref,intent_revision,tool,request_hash,arguments,status,created) '
                            "VALUES(?,?,?,?,?,?,?,?,?,?)", (operation, call['session_id'], scope.scope_id, root['intent_ref'],
                            root['intent_revision'], 'life_consult', digest(proposal), canonical(proposal), 'dispatched', time.time()))
                except Exception:
                    bridge.commit(result['request_id'], 'failed', reason='admission_rejected_before_dispatch', evidence=dict(process_stopped=True))
                    raise
            return dict(ok=True, **result)
        except ConsultError as error:
            raise SessionFailure(error.code, str(error)) from error

    async def adopt(self):
        bridge = self.require()
        await asyncio.to_thread(bridge.reconcile)
        for record in bridge.records():
            if record['result_ref'] and record.get('process_stopped') is True:
                # Execution can be settled while the answer remains unknown.
                # Repair old adopted receipts too, without reinjecting their input.
                with self.repo.tx():
                    metadata = self.role.initiative.get('initiative:operation:'+record['operation_id'])
                    if metadata and metadata.get('execution_state')!='settled':
                        self.role.initiative.put('initiative:operation:'+record['operation_id'],dict(metadata,
                            awaiting_async=False,execution_state='settled',execution_code='CONSULT_PROCESS_STOPPED',
                            execution_receipt_ref=record['result_ref']+':'+str(record['version'])))
                    self.role.initiative.finish_member(record['operation_id'])
            if not record['result_ref'] or record['delivery'] == 'adopted':
                continue
            scope = Scope(**json.loads(record['scope_json']))
            if self.repo.forget_epoch(scope.scope_id) != record['forget_epoch']:
                # A memory reset must not reinject old private context into a fresh role.
                bridge.adopted(record['request_id'], record['version'])
                continue
            result = bridge.read(record['request_id'], scope.scope_id, limit_bytes=4096)
            external_id = 'codex-consult:'+record['request_id']+':'+str(record['version'])
            text = 'Codex 咨询结果（建议不是已经实施的修改）：\n'+canonical(result)
            async with self.role._expression_locks.setdefault(scope.scope_id, asyncio.Lock()):
                with self.repo.tx():
                    event, _ = self.role.life.ingest(scope, 'consult_result', 'tool_observation', text,
                        external_id=external_id, actor_id='self:role', source_event_ids=record['source_event_ids'],
                        payload=dict(tool='life_consult', request_id=record['request_id'], result_ref=record['result_ref'],
                            operation_id=record['operation_id'], status=record['status'], version=record['version']))
                    handled = self.repo._one('SELECT 1 FROM agent_inputs WHERE input_id=? AND handled=1', (event.id,))
                    if not handled:
                        self.role.stage_input(event)
                    status = {'completed':'succeeded', 'failed':'failed', 'cancelled':'cancelled'}.get(record['status'], 'unknown')
                    self.repo._exec('UPDATE role_operations SET status=?,result=? WHERE id=?', (status, canonical(result), record['operation_id']))
                    if self.role.continuity_ready:
                        self.role.initiative.record_result(record['operation_id'], external_id,
                            failure=record.get('reason') if record['status'] == 'failed' else None,
                            unknown=record['status'] == 'unknown', progress=record['status'] == 'completed',
                            cancelled=record['status'] == 'cancelled')
                        self.role.initiative.finish_member(record['operation_id'],
                            terminal=record['status'] in TERMINAL or record.get('process_stopped') is True)
            # Mark adopted only after the logical input is durable. Existing recovery delivers it.
            bridge.adopted(record['request_id'], record['version'])
            if not handled and self.role.running and not self.role.spending.admission_paused():
                await self.role.ingest(event)

    async def watch(self):
        while self.role.running:
            try:
                await self.adopt()
            except Exception:
                log.exception('Codex 咨询对账暂未完成，保留原调用，不重放')
            await asyncio.sleep(2)

    async def cancel_intents(self, scope_id, refs):
        if self.bridge:
            for record in self.bridge.records():
                if record['scope_id'] == scope_id and record.get('intent_ref') in refs and record['status'] not in TERMINAL:
                    self.bridge.cancel(record['request_id'], scope_id)
