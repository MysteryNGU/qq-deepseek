"""Render a model-chosen expression once and dispatch through Delivery."""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar

from .agent_session import SessionFailure,canonical,digest
from .delivery import DeliveryContext, DeliveryNotSent
from .message_layout import DEFAULT_PASTE_THRESHOLD,bubble_delay,content_blocks,expression_body,expression_parts,group_chat,transport_parts

expression_guard = ContextVar('expression_guard',default=None)


class Expressions:
    accepts_plan = True
    def __init__(self,life,media,voices,transport,*,typing_cps=3,paste_threshold=DEFAULT_PASTE_THRESHOLD,sleep=asyncio.sleep):
        self.life,self.repo,self.media,self.voices,self.transport = life,life.repo,media,voices,transport
        self.typing_cps,self.paste_threshold,self.sleep = typing_cps,paste_threshold,sleep
        self.repo.db.execute('CREATE TABLE IF NOT EXISTS expressions('
            'id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,request_hash TEXT NOT NULL,'
            'plan TEXT NOT NULL,rendered TEXT,context_revision INTEGER NOT NULL,forget_epoch INTEGER NOT NULL)')
        self._locks = {}
        self._conversation_locks = {}

    async def __call__(self,scope,logical,proposal,sources):
        if isinstance(proposal,str):
            proposal = dict(text=proposal)
        checksum = digest(expression_body(proposal))
        async with self._locks.setdefault(logical,asyncio.Lock()):
            row = self.repo._one('SELECT * FROM expressions WHERE id=?',(logical,))
            if row and (row['scope_id']!=scope.scope_id or row['request_hash']!=checksum):
                raise SessionFailure('idempotency_conflict','表达编号已冻结为另一份内容')
            if not row:
                parts = proposal.get('parts')
                if parts is None:
                    parts = [dict(kind='text',text=proposal.get('text',''),mode=proposal.get('mode','auto'))]
                if not isinstance(parts,list) or not parts:
                    raise SessionFailure('invalid_input','表达需要至少一个实际片段')
                parts = json.loads(canonical(parts))
                for part in parts:
                    if part.get('kind')=='voice':
                        part['voice_id'] = part.get('voice_id') or self.voices.default(scope)
                        voice = self.voices.require(part['voice_id'],False)
                        part['voice_revision'] = voice['revision']
                event,_ = self.life.ingest(scope,'expression','inference','',external_id=logical,
                    source_event_ids=sources,payload=dict(parts=parts))
                plan = dict(parts=parts,event_id=event.id,sources=sources)
                with self.repo.tx():
                    self.repo._exec('INSERT INTO expressions VALUES(?,?,?,?,NULL,?,?)',
                        (logical,scope.scope_id,checksum,canonical(plan),self.life.delivery.revision(scope),
                         self.repo.forget_epoch(scope.scope_id)))
                row = self.repo._one('SELECT * FROM expressions WHERE id=?',(logical,))
            plan = json.loads(row['plan'])
            self.require_current(scope,row)
            if row['rendered'] is None:
                rendered,delays = [],[]
                for index,part in enumerate(expression_parts(plan['parts'],self.paste_threshold)):
                    kind = part.get('kind')
                    if kind=='text':
                        blocks = group_chat(content_blocks(str(part.get('text','')),part.get('mode','auto'),
                                                          paste_threshold=self.paste_threshold))
                        for bubble in blocks:
                            for piece in transport_parts(bubble):
                                rendered.append([dict(type='text',data=dict(text=piece.text))])
                                delays.append(bubble_delay(piece.text,piece.kind,cps=self.typing_cps))
                    elif kind in {'voice','image','audio'}:
                        if kind=='voice':
                            if not str(part.get('text','')).strip():
                                raise SessionFailure('invalid_input','语音合成正文不能为空')
                            asset = await self.voices.synthesize(scope,plan['event_id'],str(index),
                                str(part['text']),part['voice_id'],part.get('parameters'))
                        else:
                            asset = await self.media.resolve(scope,str(part['asset_id']))
                        expected = 'image' if kind=='image' else 'audio'
                        if asset['kind']!=expected:
                            raise SessionFailure('invalid_input','资产类型与表达片段不符')
                        self.media.read_bytes(scope,asset['id'])
                        from pathlib import Path
                        rendered.append([dict(type='image' if expected=='image' else 'record',
                            data=dict(file=Path(asset['storage_ref']).as_uri()))])
                        delays.append(1)
                    elif kind=='mention':
                        if not scope.conversation_id.startswith('qq:group:'):
                            raise SessionFailure('scope_mismatch','私聊不能使用群 @')
                        rendered.append([dict(type='at',data=dict(qq=str(int(part['user_id']))))])
                        delays.append(1)
                    else:
                        raise SessionFailure('invalid_input','不支持的表达片段',kind=kind)
                    self.require_current(scope,row)
                if not rendered:
                    raise SessionFailure('invalid_input','表达没有可发送内容')
                with self.repo.tx():
                    self.repo._exec('UPDATE expressions SET rendered=? WHERE id=?',
                        (canonical(dict(messages=rendered,delays=delays)),logical))
            else:
                frozen = json.loads(row['rendered'])
                if isinstance(frozen,list):
                    # Pending expressions from the immediately preceding runtime.
                    rendered = frozen
                    delays = [bubble_delay(''.join(s['data'].get('text','') for s in segments),
                        'artifact' if len(segments)!=1 or segments[0]['type']!='text' else 'chat',
                        cps=self.typing_cps) for segments in rendered]
                else:
                    rendered,delays = frozen['messages'],frozen['delays']
            ctx = DeliveryContext(self.life.delivery,scope,logical,row['context_revision'],tuple(plan['sources']),
                row['forget_epoch'],self.life.jobs.policy_epoch(scope.scope_id))
            group = scope.conversation_id.startswith('qq:group:')
            action = 'send_group_msg' if group else 'send_private_msg'
            key = 'group_id' if group else 'user_id'
            target = int(scope.conversation_id.rsplit(':',1)[1])
            deliveries = []
            for index,segments in enumerate(rendered):
                identity = self.life.delivery.prepare(ctx,action,{key:target,'message':segments},segment=index)
                deliveries.append(identity)
            async with self._conversation_locks.setdefault(scope.conversation_id,asyncio.Lock()):
                await self.dispatch(scope,row,deliveries,delays)
            return dict(sent_confirmed=True,logical_message_id=logical,delivery_ids=deliveries)

    async def dispatch(self,scope,row,deliveries,delays):
        for identity,delay in zip(deliveries,delays,strict=True):
            existing = self.repo._one('SELECT status FROM deliveries WHERE id=?',(identity,))
            if existing['status']=='confirmed':
                continue
            if existing['status'] in {'dispatching','unknown'}:
                raise SessionFailure('outcome_unknown','原发送结果尚未对账，不重新发送',delivery_id=identity)
            if existing['status']=='deferred':
                raise SessionFailure('channel_offline','此片段尚未发出，等待通道恢复后的复核事件',delivery_id=identity)
            self.require_current(scope,row)
            # No transaction or delivery lease is held while simulating typing.
            remaining = delay
            while remaining > 0:
                step = min(1,remaining)
                await self.sleep(step)
                remaining -= step
                self.require_current(scope,row)
            token = self.life.delivery.claim(identity)
            if not token:
                raise SessionFailure('stale_context','表达已被新输入替代')
            payload = json.loads(self.repo._one('SELECT payload FROM deliveries WHERE id=?',(identity,))['payload'])
            try:
                response = await self.transport(payload['action'],payload['params'])
            except DeliveryNotSent:
                self.life.delivery.not_sent(identity,token)
                raise SessionFailure('channel_offline','QQ通道未连接，本片段尚未发出；重连后会提交复核事件',delivery_id=identity)
            except BaseException:
                self.life.delivery.settle(identity,token)
                raise
            if not self.life.delivery.settle(identity,token,response):
                raise SessionFailure('outcome_unknown','平台未证实发送成功',delivery_id=identity)

    def require_current(self,scope,row):
        guard = expression_guard.get()
        if guard:
            guard()
        if row['forget_epoch']!=self.repo.forget_epoch(scope.scope_id):
            raise SessionFailure('stale_context','表达来源已遗忘')
        if row['context_revision']!=self.life.delivery.revision(scope):
            raise SessionFailure('input_pending','出现新消息，需要主角色重新确认表达')
