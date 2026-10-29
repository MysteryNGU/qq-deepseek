import { Context } from '@deepseek-ai/cordis'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import { mountAgentLoopTestDependencies } from '@deepseek-ai/dsh-agent-loop-testkit'
import { Session,SessionId } from '@deepseek-ai/dsh-session'
import { AttachmentId } from '@deepseek-ai/dsh-attachment'
import { createAssistantMessage,createToolResultMessage,createUserMessage,ToolCallId } from '@deepseek-ai/dsh-llm'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import TokenMeter from '@deepseek-ai/dsh-token-meter'
import { imageOffloadProjection } from '../../../compaction/compaction-image-offload/src/projection.ts'
import { MockAdapter,textResponse } from '../../../core/agent-loop/tests/mock-adapter.ts'
import { describe,it,expect,vi } from 'vitest'
import { QqLifeContext } from '../src/qq-life-context.ts'
import { BasicCompactionEngine } from '../../../compaction/compaction-basic/src/index.ts'

const policy={ chat_input_target_tokens: 100,task_input_target_tokens: 150,pressure_ratio: 0.8,
  recent_history_target_tokens: 20,handoff_target_tokens: 10,provider_margin_tokens: 8,reference_image_batches: 2 }
const plan={ stamp: 'current',presentation_scope: 'private:1',mode: 'chat',protected_input_ids: ['current'],
  pinned_asset_ids: [] as string[],protect_task_images: false }

async function harness() {
  const ctx=new Context()
  await mountAgentLoopTestDependencies(ctx)
  await ctx.plugin(TokenMeter)
  ctx.sessions.registerMessageProjection(imageOffloadProjection)
  await ctx.plugin(AgentLoop,{ agents: [] })
  ctx.llm.registerAdapter(['mock'],new MockAdapter([textResponse('unused')]))
  const agent=await ctx.agentLoop.create(SessionId('context-tests'),{ provider: 'mock',model: 'mock' })
  const host=vi.fn(async () => ({ ok: true }))
  const control=new QqLifeContext(ctx,()=>true,host)
  return { ctx,agent,host,control,async close() { await ctx.fiber.dispose() } }
}

function image(name: string): ContentBlock {
  return { type: 'image',attachment: { attachmentId: AttachmentId(`sha256:${'a'.repeat(64)}`),
    name,mediaType: 'image/png',bytes: 10,width: 100,height: 100 } }
}

describe('durable request projections',() => {
  it('logs the final consumed-input batch and replaces its previous presentation',async () => {
    const f=await harness(),s=f.agent.session
    try {
      s.append('turn/start',{ turn: 1 });s.append('step/start',{ turn: 1,step: 1 })
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: plan })
      f.host.mockImplementationOnce(async () => ({ ok: true,input_batch: { ref: 'ib:first',input_ids: ['a','b'],turn: 1,step: 1 } }))
      await f.control.prepare({ agent: f.agent,turn: 1,step: 1,signal: new AbortController().signal })
      expect(JSON.stringify(s.deriveMessages())).toContain('ib:first')
      s.append('step/end',{ turn: 1,step: 1 });s.append('step/start',{ turn: 1,step: 2 })
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: plan })
      f.host.mockImplementationOnce(async () => ({ ok: true,input_batch: { ref: 'ib:second',input_ids: ['c'],turn: 1,step: 2 } }))
      await f.control.prepare({ agent: f.agent,turn: 1,step: 2,signal: new AbortController().signal })
      const visible=JSON.stringify(s.deriveMessages())
      expect(visible).toContain('ib:second');expect(visible).not.toContain('ib:first')
      expect(JSON.stringify(s.snapshotEvents())).toContain('ib:first')
      const restored=Session.create(s.id,s.snapshotEvents(),undefined,undefined,[imageOffloadProjection])
      expect(JSON.stringify(restored.deriveMessages())).toBe(visible)
    } finally { await f.close() }
  })

  it('deduplicates only versioned read pairs and retains distinct same-text user events and unknown writes',async () => {
    const f=await harness(),s=f.agent.session
    try {
      s.append('turn/start',{ turn: 1 })
      for (let step=1;step<=4;step++) {
        s.append('step/start',{ turn: 1,step })
        if (step<=2) s.append('user/message',createUserMessage({ content: [{ type: 'text',text: 'same user words' }],
          source: { kind: 'qq-life',inputId: `different-event-${step}`,origin: 'user_report' } }),{ surfaceOp: 'append' })
        const name=step===3 ? 'send_external' : 'life_read',id=ToolCallId(`call-${step}`)
        s.append('assistant/message',{ turn: 1,step,stream: [],message: createAssistantMessage({ source: { provider: 'mock',model: 'mock' },
          content: [{ type: 'tool-call',id,name,arguments: '{"proposal":{"kind":"observation","event_id":"e"}}' }] }) },{ surfaceOp: 'append' })
        const data=step===3 ? { status: 'unknown',operation_id: 'never-resend' } : step===4 ? { text: 'unversioned' } : {
          text: 'verified content '.repeat(300),read_projection: { version: 'resource-v1',scope: plan.presentation_scope,result_hash: 'same-bytes',tool_identity: { schema_hash: 'read-v1' } } }
        s.append('tool/call',{ turn: 1,step,callId: id,name,arguments: '{}' })
        s.append('tool/result',{ turn: 1,step,message: createToolResultMessage({ callId: id,isError: false,content: [{ type: 'text',text: JSON.stringify(data) }] }) },{ surfaceOp: 'append' })
        s.append('step/end',{ turn: 1,step })
      }
      s.append('turn/end',{ turn: 1,reason: { kind: 'completed' } })
      s.append('turn/start',{ turn: 2 });s.append('step/start',{ turn: 2,step: 1 })
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: plan })
      const before=f.ctx.tokenMeter.measure(s).totalTokens
      await f.control.prepare({ agent: f.agent,turn: 2,step: 1,signal: new AbortController().signal })
      const messages=s.deriveMessages(),body=JSON.stringify(messages)
      expect(messages.filter(message => message.source?.kind==='qq-life')).toHaveLength(2)
      expect(messages.filter(message => message.role==='tool' && message.toolCallId==='call-1')).toHaveLength(0)
      expect(body).toContain('call-2');expect(body).toContain('never-resend');expect(body).toContain('unversioned')
      expect(f.ctx.tokenMeter.measure(s).totalTokens).toBeLessThan(before)
      expect({ complete_input_before: before,complete_input_after: f.ctx.tokenMeter.measure(s).totalTokens,
        retained_user_events: messages.filter(message => message.source?.kind==='qq-life').length,
        unknown_write_retained: body.includes('never-resend') }).toMatchInlineSnapshot(`
          {
            "complete_input_after": 1501,
            "complete_input_before": 2794,
            "retained_user_events": 2,
            "unknown_write_retained": true,
          }
        `)
      expect(s.snapshotEvents().filter(event => event.type==='tool/result')).toHaveLength(4)
      expect(s.snapshotEvents().filter(event => event.type==='compaction/prune')).toHaveLength(1)
    } finally { await f.close() }
  })

  it('offloads only old unpinned occurrences, preserves task pins and re-reading creates a fresh instance',async () => {
    const f=await harness(),s=f.agent.session
    try {
      const add=(id: string,asset: string) => s.append('user/message',createUserMessage({
        content: [{ type: 'text',text: JSON.stringify({ asset_id: asset }) },image(id)],
        source: { kind: 'qq-life',inputId: id,origin: 'user_report' } }),{ surfaceOp: 'append' })
      const old=add('old','asset-old'),pin=add('pin','asset-pin')
      s.append('turn/start',{ turn: 1 });s.append('step/start',{ turn: 1,step: 1 })
      const fresh=add('current','asset-new')
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: { ...plan,pinned_asset_ids: ['asset-pin'] } })
      await f.control.prepare({ agent: f.agent,turn: 1,step: 1,signal: new AbortController().signal })
      expect(s.deriveEventMessage(old)!.content[1]).toHaveProperty('offloaded',true)
      expect(s.deriveEventMessage(pin)!.content[1]).not.toHaveProperty('offloaded')
      expect(s.deriveEventMessage(fresh)!.content[1]).not.toHaveProperty('offloaded')
      const again=add('reread','asset-old')
      expect(s.deriveEventMessage(again)!.content[1]).not.toHaveProperty('offloaded')
      expect(old.data.content[1]).not.toHaveProperty('offloaded')
      const restored=Session.create(s.id,s.snapshotEvents(),undefined,undefined,[imageOffloadProjection])
      expect(restored.deriveMessages().flatMap(message => message.content).filter(block => block.type==='image' && block.offloaded)).toHaveLength(1)
      expect(s.snapshotEvents().filter(event => event.type==='image/offload')).toHaveLength(1)
    } finally { await f.close() }
  })

  it('checks cancellation before any host validation or paid handoff',async () => {
    const f=await harness()
    try {
      f.agent.session.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: plan })
      const abort=new AbortController();abort.abort(new Error('cancelled'))
      await expect(f.control.prepare({ agent: f.agent,turn: 1,step: 1,signal: abort.signal })).rejects.toThrow('cancelled')
      expect(f.host).not.toHaveBeenCalled()
    } finally { await f.close() }
  })

  it('reduces only the host-approved optional section and records the replacement',async () => {
    const f=await harness(),s=f.agent.session
    try {
      s.append('turn/start',{ turn: 1 });s.append('step/start',{ turn: 1,step: 1 })
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      const source_text='OPTIONAL_RECALL '.repeat(300),replacement_text='BOUNDARY + required pending input; recall source ref retained'
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: { ...plan,optional_context: { source_text,replacement_text } } })
      const sections=[{ name: 'qq-role-life',text: source_text },{ name: 'other-owner',text: 'KEEP_OTHER_PROVIDER' }]
      const original=s.append('user/message',createUserMessage({ content: [{ type: 'text',text: sections.map(section=>section.text).join('\n\n') }],
        source: { kind: 'runtime-context',form: 'snapshot',sections } }),{ surfaceOp: 'append' })
      await f.control.prepare({ agent: f.agent,turn: 1,step: 1,signal: new AbortController().signal })
      const text=JSON.stringify(s.deriveMessages())
      expect(text).not.toContain('OPTIONAL_RECALL')
      expect(text).toContain('BOUNDARY');expect(text).toContain('KEEP_OTHER_PROVIDER')
      expect(s.surface.nodes).not.toContain(original.seq)
      expect(original.data.content[0]).toMatchObject({ text: expect.stringContaining('OPTIONAL_RECALL') })
      expect(s.snapshotEvents().filter(event=>event.type==='compaction/prune')).toHaveLength(1)
    } finally { await f.close() }
  })

  it('uses a single durable handoff and preserves the current raw input even above the target',async () => {
    const f=await harness(),s=f.agent.session
    try {
      await f.ctx.plugin(BasicCompactionEngine,{ auto: false,maxTokens: 40 })
      const compact=vi.spyOn(f.ctx.compaction,'compactRegion')
      s.append('user/message',createUserMessage({ content: [{ type: 'text',text: 'Settled process A '.repeat(400) }],source: { kind: 'user' } }),{ surfaceOp: 'append' })
      s.append('user/message',createUserMessage({ content: [{ type: 'text',text: 'Settled process B '.repeat(400) }],source: { kind: 'user' } }),{ surfaceOp: 'append' })
      s.append('turn/start',{ turn: 1 });s.append('step/start',{ turn: 1,step: 1 })
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      f.control.configure(f.agent,{ resource_policy: policy,context_plan: plan })
      s.append('user/message',createUserMessage({ content: [{ type: 'text',text: 'UNANSWERED_USER_WORDS '.repeat(30) }],
        source: { kind: 'qq-life',inputId: 'current',origin: 'user_report' } }),{ surfaceOp: 'append' })
      await f.control.prepare({ agent: f.agent,turn: 1,step: 1,signal: new AbortController().signal })
      expect(compact).toHaveBeenCalledTimes(1)
      expect(JSON.stringify(s.deriveMessages())).toContain('UNANSWERED_USER_WORDS')
      expect(s.snapshotEvents().filter(event => event.type==='compaction/start')).toHaveLength(1)
      expect(s.snapshotEvents().filter(event => event.type==='compaction/end')).toHaveLength(1)
    } finally { await f.close() }
  })

  it.each(['resolved','pending','unknown'])('compacts only certified resolved shell admission errors (%s)',async (state) => {
    const f=await harness(),s=f.agent.session
    try {
      await f.ctx.plugin(BasicCompactionEngine,{ auto: false,maxTokens: 40 })
      const compact=vi.spyOn(f.ctx.compaction,'compactRegion')
      const add=(text: string) => s.append('user/message',createUserMessage({
        content: [{ type: 'text',text: text.repeat(400) }],source: { kind: 'user' } }),{ surfaceOp: 'append' })
      s.append('turn/start',{ turn: 1 });s.append('step/start',{ turn: 1,step: 1 })
      add('Earlier settled A ');add('Earlier settled B ')
      const id=ToolCallId('old-failed-call')
      s.append('assistant/message',{ turn: 1,step: 1,stream: [],message: createAssistantMessage({
        source: { provider: 'mock',model: 'mock' },content: [{ type: 'tool-call',id,name: 'pwsh',arguments: '{}' }] }) },{ surfaceOp: 'append' })
      const failed=s.append('tool/result',{ turn: 1,step: 1,message: createToolResultMessage({ callId: id,isError: true,
        content: [{ type: 'text',text: JSON.stringify({ ok: false,code: state==='unknown' ? 'outcome_unknown' : 'input_pending',message: 'preserve until resolved' }) }] }) },{ surfaceOp: 'append' })
      s.append('step/end',{ turn: 1,step: 1 });s.append('turn/end',{ turn: 1,reason: { kind: 'completed' } })
      add('Later settled A ');add('Later settled B ')
      s.append('turn/start',{ turn: 2 });s.append('step/start',{ turn: 2,step: 1 })
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      const context_plan={ ...plan,resolved_input_error_turn: state==='pending' ? 0 : 1 }
      f.control.configure(f.agent,{ resource_policy: policy,context_plan })
      s.append('user/message',createUserMessage({ content: [{ type: 'text',text: 'CURRENT_REQUIRED_WORDS '.repeat(400) }],
        source: { kind: 'qq-life',inputId: 'current',origin: 'user_report' } }),{ surfaceOp: 'append' })
      await f.control.prepare({ agent: f.agent,turn: 2,step: 1,signal: new AbortController().signal })
      expect(compact).toHaveBeenCalledTimes(1)
      const [start,end]=compact.mock.calls[0]!
      expect(start<=failed.seq && end>=failed.seq).toBe(state==='resolved')
      expect(s.surface.nodes.includes(failed.seq)).toBe(state!=='resolved')
      f.control.configure(f.agent,{ resource_policy: policy,context_plan })
      await f.control.prepare({ agent: f.agent,turn: 2,step: 2,signal: new AbortController().signal })
      expect(compact.mock.calls.length).toBeLessThanOrEqual(2)
      expect(JSON.stringify(s.deriveMessages())).toContain('CURRENT_REQUIRED_WORDS')
      expect(s.snapshotEvents().some(event=>event.seq===failed.seq)).toBe(true)
    } finally { await f.close() }
  })

  it('compacts newly accumulated history again within the same long turn',async () => {
    const f=await harness(),s=f.agent.session
    try {
      f.ctx.llm.registerAdapter(['mock'],new MockAdapter([textResponse('first summary'),textResponse('second summary')]))
      await f.ctx.plugin(BasicCompactionEngine,{ auto: false,maxTokens: 40 })
      const compact=vi.spyOn(f.ctx.compaction,'compactRegion')
      s.append('turn/start',{ turn: 1 })
      s.append('request/header',{ header: { config: { provider: 'mock',model: 'mock' } },reason: 'initial' })
      for (let step=1;step<=2;step++) {
        for (const label of ['A','B','tail']) s.append('user/message',createUserMessage({
          content: [{ type: 'text',text: `settled-${step}-${label} `.repeat(400) }],source: { kind: 'user' } }),{ surfaceOp: 'append' })
        s.append('step/start',{ turn: 1,step })
        f.control.configure(f.agent,{ resource_policy: policy,context_plan: plan })
        const before=f.ctx.tokenMeter.measure(s).totalTokens
        await f.control.prepare({ agent: f.agent,turn: 1,step,signal: new AbortController().signal })
        expect(compact).toHaveBeenCalledTimes(step)
        expect(f.ctx.tokenMeter.measure(s).totalTokens).toBeLessThan(before)
        s.append('step/end',{ turn: 1,step })
      }
    } finally { await f.close() }
  })
})
