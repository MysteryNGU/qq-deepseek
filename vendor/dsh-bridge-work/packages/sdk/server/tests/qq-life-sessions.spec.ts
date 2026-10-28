import { Context } from '@deepseek-ai/cordis'
import Loader from '@deepseek-ai/cordis-plugin-loader'
import AgentRegistry from '@deepseek-ai/dsh-agent'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import type {} from '../../../core/agent-loop/src/runtime-context.ts'
import { mountAgentLoopTestDependencies } from '@deepseek-ai/dsh-agent-loop-testkit'
import SessionStore, { Session, SessionId } from '@deepseek-ai/dsh-session'
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import * as JsonlPersistence from '@deepseek-ai/dsh-session-persistence-jsonl'
import SessionQuery from '@deepseek-ai/dsh-session-query-sqlite'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { MockAdapter, textResponse, toolCallResponse } from '../../../core/agent-loop/tests/mock-adapter.ts'
import { BasicCompactionEngine } from '../../../compaction/compaction-basic/src/index.ts'
import { QqLifeSessions } from '../src/qq-life-sessions.ts'
import { SpillStore, SpillLocator, type SaveTextSpill } from '@deepseek-ai/dsh-spill'
import ToolRuntime, { defineTool } from '@deepseek-ai/dsh-tools'
import type { ToolExecutionToken, ToolExecution } from '@deepseek-ai/dsh-tools'
import LlmRuntime, { createUserMessage, ToolCallId } from '@deepseek-ai/dsh-llm'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { AttachmentId } from '@deepseek-ai/dsh-attachment'
import { QqLifeResources } from '../src/qq-life-resources.ts'
import SkillRegistry from '@deepseek-ai/dsh-skill'
import TokenMeter from '@deepseek-ai/dsh-token-meter'
import Jobs from '@deepseek-ai/dsh-jobs-local'
import Subprocess from '@deepseek-ai/dsh-subprocess-local'
import * as ToolJobs from '@deepseek-ai/dsh-tool-jobs'
import { writeFile, readFile, mkdir } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

const hostContracts = JSON.parse(readFileSync(new URL('./fixtures/qq-life-contracts.json', import.meta.url),'utf8'))
const contextPolicy = { text_preview_target_tokens: 3000,text_result_max_tokens: 4000,text_result_max_utf8_bytes: 24000,
  chat_input_target_tokens: 32000,task_input_target_tokens: 48000,pressure_ratio: 0.8,recent_history_target_tokens: 8000,
  handoff_target_tokens: 4000,provider_margin_tokens: 1024,reference_image_batches: 2 }
const contextPlan = { stamp: 'facts-v1',presentation_scope: 'scope-v1',mode: 'chat',protected_input_ids: ['input-1'],
  pinned_asset_ids: [],protect_task_images: false }

async function fixture(beforeAdmission?: () => Promise<void>, script?: ConstructorParameters<typeof MockAdapter>[0], continuity = false, extensions = false, assembled = false, consult = false, experience = false, compact = false) {
  const root = await mkdtemp(join(tmpdir(), 'qq-role-session-'))
  const ctx = new Context()
  if (assembled) {
    await ctx.plugin(Loader)
    Object.assign(ctx.loader.builtins, { llm: LlmRuntime, sessions: SessionStore, projections: SessionProjectionRegistry,
      prompt: SystemPrompt, tools: ToolRuntime, agents: AgentRegistry, meter: TokenMeter, skills: SkillRegistry,
      loop: AgentLoop, persistence: JsonlPersistence, query: SessionQuery, jobs: Jobs, subprocess: Subprocess, toolJobs: ToolJobs })
    const entries: { name: string; config?: object }[] = [
      ...['llm', 'sessions', 'projections', 'prompt', 'tools', 'agents', 'meter', 'skills'].map(name => ({ name: `cordis:${name}` })),
      { name: 'cordis:loop', config: { agents: [] } },
      { name: 'cordis:persistence', config: { root: join(root, 'sessions') } },
      { name: 'cordis:query', config: { path: ':memory:', openAt: 'never' } },
    ]
    const config = join(root, 'cordis.yml')
    if (consult) entries.push(...['jobs','subprocess','toolJobs'].map(name => ({ name: `cordis:${name}` })))
    await writeFile(config, JSON.stringify(entries))
    const loaded: typeof entries = JSON.parse(await readFile(config, 'utf8'))
    for (const entry of loaded) await ctx.loader.create(entry)
    await ctx.loader.await()
  } else {
    await mountAgentLoopTestDependencies(ctx)
    await ctx.plugin(TokenMeter)
    await ctx.plugin(SkillRegistry)
    await ctx.plugin(AgentLoop, { agents: [] })
    await ctx.plugin(JsonlPersistence, { root: join(root, 'sessions') })
    await ctx.plugin(SessionQuery, { path: ':memory:', openAt: 'never' })
  }
  const adapter = new MockAdapter(script ?? [toolCallResponse('say-1', 'life_say', { proposal: { text: 'hello', end_turn: true, handled_input_ids: ['input-1'] } })])
  ctx.llm.registerAdapter(['mock'], adapter)
  const request = vi.fn(async (method: string, params: object): Promise<Record<string, unknown>> => {
    if (compact && 'kind' in params && params.kind === 'static_context') return { ok: true,stable_text: 'Current test persona.',
      tool_contracts: hostContracts,presentation_scope: 'scope-v1',default_tools: ['pwsh','job_output','job_kill'] }
    if (compact && 'kind' in params && params.kind === 'context') return { ok: true,stable_text: 'Current test persona.',text: 'Current facts.',
      resource_policy: contextPolicy,context_plan: contextPlan }
    return method === 'qq_life/life/read' ? { ok: true, text: 'You are the test role.', continuity_protocol: 1, continuity_enabled: continuity }
      : { ok: true, params, ...'kind' in params && params.kind === 'life.say' ? { turn_complete: true } : {} }
  })
  const sessions = new QqLifeSessions(ctx, { request, notify() {} }, () => root, async id => {
    const existing = ctx.agents.get(SessionId(id))
    if (existing) return existing
    return (await ctx.agents.create({ sessionId: SessionId(id), agentOptions: { provider: 'mock', model: 'mock' } })).agent
  }, async blocks => {
    await beforeAdmission?.()
    return blocks.flatMap(block => block.type === 'text' ? [block] : [])
  }, continuity, { capability: extensions, media: extensions, consult, experience,compact })
  return { ctx, root, adapter, sessions, request, async close() {
    await ctx.fiber.dispose(); sessions.dispose(); await rm(root, { recursive: true, force: true })
  } }
}

const input = { session_id: 'main', generation: 1, input_id: 'input-1',
  source: 'user_report', content: 'hello', source_event_ids: ['input-1'], mode: 'steer' }

describe('complete bot request preparation', () => {
  it('presents a logged input batch after request assembly through the Loader composition',async () => {
    const f=await fixture(undefined,[(options) => {
      expect(JSON.stringify(options.messages)).toContain('ib:complete-input')
      return toolCallResponse('say-batch','life_say',{ proposal: {
        text: 'I understood this batch.',handled_batch_ref: 'ib:complete-input',end_turn: true,
      } })
    }],false,true,true,false,false,true)
    const fallback=f.request.getMockImplementation()!
    f.request.mockImplementation(async (method,params) => {
      if ('kind' in params && params.kind==='context_validate') return { ok: true,
        input_batch: { ref: 'ib:complete-input',input_ids: ['input-1'],turn: 1,step: 1 } }
      return fallback(method,params)
    })
    try {
      await f.sessions.handle('input',input)
      const agent=f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(1)
      expect(agent.session.snapshotEvents().filter(event=>event.type==='turn/end').map(event=>event.data.reason)).toEqual([{ kind: 'completed' }])
      expect(JSON.stringify(agent.session.snapshotEvents())).toContain('ib:complete-input')
      expect(agent.session.snapshotEvents().filter(event=>event.type==='tool/result')
        .map(event=>event.data.message)).toEqual([expect.objectContaining({ isError: false })])
      expect(f.request.mock.calls.some(([,p]) => 'kind' in p && p.kind==='life.say'
        && 'proposal' in p && (p.proposal as Record<string,unknown>).handled_batch_ref==='ib:complete-input')).toBe(true)
    } finally { await f.close() }
  })

  it('shows host-selected native defaults immediately and still checks execution with the host', async () => {
    for (const allowed of [false,true]) {
      let executions=0
      const f=await fixture(undefined,[(options) => {
        expect(options.tools!.map(tool => tool.name)).toEqual(expect.arrayContaining(['pwsh','job_output','job_kill']))
        expect(options.tools!.some(tool => tool.name==='life_media')).toBe(false)
        return toolCallResponse('shell','pwsh',{})
      },(options) => {
        expect(options.tools).toEqual(f.adapter.requests[0]!.tools)
        return toolCallResponse('rest','life_rest',{ proposal: {} })
      }],false,true,true,false,false,true)
      for (const name of ['pwsh','job_output','job_kill']) f.ctx.effect(() => f.ctx.tools.register(defineTool({
        name,description: 'Local execution probe.',parameters: {},
        output: { schema: { type: 'string' },render: (_args,value) => [{ type: 'text',text: value }] },
        execute: async () => { executions++;return 'probe' },
      })))
      const fallback=f.request.getMockImplementation()!
      f.request.mockImplementation(async (method,params) => {
        if ('kind' in params && params.kind==='before_tool' && 'tool' in params && params.tool==='pwsh') return { ok: allowed,code: 'scope_mismatch' }
        return fallback(method,params)
      })
      try {
        await f.sessions.handle('input',input)
        await f.ctx.agents.get(SessionId('main'))!.whenIdle()
        expect(executions).toBe(allowed ? 1 : 0)
        expect(f.adapter.requests).toHaveLength(2)
        expect(f.request.mock.calls.some(([,params]) => 'kind' in params && params.kind==='before_tool')).toBe(true)
        expect(f.request.mock.calls.some(([,params]) => 'kind' in params && params.kind==='life.read')).toBe(false)
      } finally { await f.close() }
    }
  })

  it('sends structured base tools and ends a final say without an extra rest request', async () => {
    const f = await fixture(undefined,undefined,false,true,true,false,false,true)
    try {
      await f.sessions.handle('input',input)
      const agent=f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(1)
      const tools=f.adapter.requests[0]!.tools!
      expect(tools.map(tool => tool.name)).toEqual(['life_read','life_remember','life_intent','life_appraise','life_say','life_rest'])
      const say=tools.find(tool => tool.name==='life_say')!
      expect(say.parameters).toMatchObject({ properties: { proposal: { type: 'object',additionalProperties: false,
        properties: { end_turn: { type: 'boolean' } } } } })
      expect(agent.session.requestHeader()!.tools).toEqual(tools)
      const usage=f.sessions.handle('usage',{ session_id: 'main',generation: 1,after_seq: -1 })
      expect(JSON.stringify(await usage)).toContain('dsh-token-meter-route-priced')
      expect(JSON.stringify(await usage)).toContain('mandatory_overflow')
    } finally { await f.close() }
  })

  it('loads an exact current schema once and persists its presentation separately from permission', async () => {
    let f: Awaited<ReturnType<typeof fixture>>
    f=await fixture(undefined,[options => {
      const agent=f.ctx.agents.get(SessionId('main'))!
      const schema=f.ctx.tools.schemas(agent).find(tool => tool.name==='life_media')!
      const schema_hash=createHash('sha256').update(JSON.stringify(schema)).digest('hex')
      expect(options.tools?.some(tool => tool.name==='life_media')).toBe(false)
      return toolCallResponse('load','life_read',{ proposal: { kind: 'capabilities',ref: 'tool:dsh:life_media',schema_hash,load: true } })
    },options => {
      expect(options.tools?.some(tool => tool.name==='life_media')).toBe(true)
      expect(JSON.stringify(options.messages)).not.toContain('"parameters"')
      return toolCallResponse('rest','life_rest',{ proposal: {} })
    }],false,true,true,false,false,true)
    const fallback=f.request.getMockImplementation()!
    f.request.mockImplementation(async (method,params) => {
      if ('kind' in params && params.kind==='life.read') {
        const tool=f.ctx.tools.schemas(f.ctx.agents.get(SessionId('main'))!).find(tool => tool.name==='life_media')!
        return { ok: true,summary: 'Loaded media fields.',tool_load: { name: 'life_media',status: 'ready',
          presentation_scope: 'scope-v1',schema_hash: createHash('sha256').update(JSON.stringify(tool)).digest('hex') } }
      }
      return fallback(method,params)
    })
    try {
      await f.sessions.handle('input',input)
      const agent=f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(2)
      expect(agent.session.snapshotEvents().filter(event => event.type==='developer/message'
        && event.data.message.source.kind==='qq-life-tools')).toHaveLength(1)
      expect(f.request.mock.calls.filter(([,params]) => 'kind' in params && params.kind==='before_tool')).toHaveLength(2)
    } finally { await f.close() }
  })

  it('never activates failed or stale load receipts and rejects malformed fields before the host commit', async () => {
    const f=await fixture(undefined,[
      toolCallResponse('bad-load','life_read',{ proposal: { kind: 'capabilities',ref: 'tool:dsh:life_media',schema_hash: 'stale',load: true } }),
      toolCallResponse('bad-shape','life_say',{ proposal: { text: 'do not send',end_turn: 'true' } }),
      options => {
        expect(options.tools?.some(tool => tool.name==='life_media')).toBe(false)
        return toolCallResponse('rest','life_rest',{ proposal: {} })
      },
    ],false,true,true,false,false,true)
    const fallback=f.request.getMockImplementation()!
    f.request.mockImplementation(async (method,params) => {
      if ('kind' in params && params.kind==='life.read') return { ok: true,tool_load: { name: 'life_media',status: 'ready',presentation_scope: 'scope-v1',schema_hash: 'stale' } }
      return fallback(method,params)
    })
    try {
      await f.sessions.handle('input',input)
      const agent=f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(3)
      expect(f.request.mock.calls.some(([,params]) => 'kind' in params && params.kind==='life.say')).toBe(false)
      expect(agent.session.snapshotEvents().filter(event => event.type==='developer/message'
        && event.data.message.source.kind==='qq-life-tools')).toHaveLength(0)
    } finally { await f.close() }
  })

  it('restores only current loaded definitions after adapter restart and drops changed versions', async () => {
    for (const changed of [false,true]) {
      let f: Awaited<ReturnType<typeof fixture>>
      f=await fixture(undefined,[() => {
        const schema=f.ctx.tools.schemas(f.ctx.agents.get(SessionId('main'))!).find(tool => tool.name==='life_media')!
        return toolCallResponse('load','life_read',{ proposal: { kind: 'capabilities',ref: 'tool:dsh:life_media',load: true,
          schema_hash: createHash('sha256').update(JSON.stringify(schema)).digest('hex') } })
      },toolCallResponse('rest','life_rest',{ proposal: {} }),options => {
        expect(options.tools?.some(tool => tool.name==='life_media')).toBe(!changed)
        return toolCallResponse('rest-after-restart','life_rest',{ proposal: {} })
      }],false,true,true,false,false,true)
      const fallback=f.request.getMockImplementation()!
      let restarted=false
      f.request.mockImplementation(async (method,params) => {
        if ('kind' in params && params.kind==='life.read') {
          const schema=f.ctx.tools.schemas(f.ctx.agents.get(SessionId('main'))!).find(tool => tool.name==='life_media')!
          return { ok: true,tool_load: { name: 'life_media',status: 'ready',presentation_scope: 'scope-v1',
            schema_hash: createHash('sha256').update(JSON.stringify(schema)).digest('hex') } }
        }
        const result=await fallback(method,params)
        if (changed && restarted && 'kind' in params && params.kind==='static_context') {
          const tool_contracts=structuredClone(hostContracts)
          tool_contracts.version='registry-v2'
          tool_contracts.items.find((item: { name: string }) => item.name==='life_media').description+=' v2'
          return { ...result,tool_contracts }
        }
        return result
      })
      let resumed: QqLifeSessions | undefined
      try {
        await f.sessions.handle('input',input)
        const agent=f.ctx.agents.get(SessionId('main'))!
        await agent.whenIdle()
        f.sessions.dispose();restarted=true
        resumed=new QqLifeSessions(f.ctx,{ request: f.request,notify() {} },()=>f.root,async () => agent,
          undefined,false,{ capability: true,compact: true })
        await resumed.handle('input',{ ...input,input_id: 'after-restart' })
        await agent.whenIdle()
        expect(f.adapter.requests).toHaveLength(3)
      } finally { resumed?.dispose();await f.close() }
    }
  })

  it('permits mandatory soft overflow and records that unknown physical capacity remains unknown', async () => {
    const f=await fixture(undefined,undefined,false,true,true,false,false,true)
    const fallback=f.request.getMockImplementation()!
    f.request.mockImplementation(async (method,params) => {
      const reply=await fallback(method,params)
      if ('kind' in params && params.kind==='context') return { ...reply,
        resource_policy: { ...contextPolicy,chat_input_target_tokens: 8 } }
      return reply
    })
    try {
      await f.sessions.handle('input',input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(f.adapter.requests).toHaveLength(1)
      const result=await f.sessions.handle('usage',{ session_id: 'main',generation: 1,after_seq: -1 })
      expect(JSON.stringify(result)).toContain('"mandatory_overflow":true')
      expect(JSON.stringify(result)).toContain('"physical_input_capacity":null')
    } finally { await f.close() }
  })

  it('blocks provider execution when source versions or the physical capacity reject preparation', async () => {
    for (const failure of ['changed','capacity']) {
      const f=await fixture(undefined,undefined,false,true,true,false,false,true)
      const fallback=f.request.getMockImplementation()!
      if (failure==='capacity') vi.spyOn(f.adapter,'resolveModel').mockResolvedValue({ provider: 'mock',id: 'mock',name: 'mock',
        context: { contextWindow: 16 } })
      f.request.mockImplementation(async (method,params) => {
        if (failure==='changed' && 'kind' in params && params.kind==='context_validate') return { ok: false,code: 'stale_context' }
        return fallback(method,params)
      })
      try {
        await f.sessions.handle('input',input)
        await f.ctx.agents.get(SessionId('main'))!.whenIdle()
        expect(f.adapter.requests).toHaveLength(0)
      } finally { await f.close() }
    }
  })
})

describe('identified role Agent inputs', () => {
  it('forwards the negotiated experience tool and logs its receipt before the next request', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('effect', 'life_simulation', { proposal: { experience_protocol: 2, experience_domain: 'adult_intimate', action: 'appraise_input', base_revision: 2, sources: ['input-1'], subject_actor_id: '1', direction: 'increase', effects: { arousal: 0.1 }, confidence: 1, reason: 'current language meaning' } }),
      toolCallResponse('read-state', 'life_read', { proposal: { kind: 'simulation' } }),
      toolCallResponse('rest', 'life_rest', { proposal: {} }),
    ], false, false, true, false, true)
    f.request.mockImplementation(async (_method, params): Promise<Record<string, unknown>> => {
      if ('kind' in params && params.kind === 'life.simulation') return { ok: true, evaluated: true, applied: true, transition_refs: ['transition-one'], state_revision: 3 }
      return { ok: true, text: 'The same role, with current state.', transition_refs: ['transition-one'] }
    })
    try {
      await f.sessions.handle('input', input)
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('life_simulation')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('stop immediately resets the episode')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('experience_protocol:2')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('Silence, imagined answers')
      expect(JSON.stringify(f.adapter.requests[1])).toContain('transition-one')
      expect(f.request.mock.calls.filter(([, p]) => 'kind' in p && p.kind === 'life.simulation')).toHaveLength(1)
      f.sessions.dispose()
      expect(f.ctx.tools.schemas(agent).some(tool => tool.name === 'life_simulation')).toBe(false)
    } finally { await f.close() }
  })

  it('does not expose experience without negotiation or to internal memory sessions', async () => {
    const normal = await fixture(undefined, [toolCallResponse('rest', 'life_rest', { proposal: {} })], false, false, true)
    try {
      await normal.sessions.handle('input', input)
      await normal.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(JSON.stringify(normal.adapter.requests[0]!.tools)).not.toContain('life_simulation')
    } finally { await normal.close() }
    const internal = await fixture(undefined, [
      toolCallResponse('forbidden', 'life_simulation', { proposal: { action: 'start' } }),
      toolCallResponse('rest', 'life_rest', { proposal: { reason: 'internal work only' } }),
    ], false, false, true, false, true)
    internal.request.mockImplementation(async (_method, _params): Promise<Record<string, unknown>> => ({ ok: true, processing_mode: 'internal', text: 'Internal sources.', stable_text: 'Internal sources.' }))
    try {
      await internal.sessions.handle('input', { ...input, processing_mode: 'internal' })
      await internal.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(JSON.stringify(internal.adapter.requests[0]!.tools)).not.toContain('life_simulation')
      expect(internal.request.mock.calls.some(([, p]) => 'kind' in p && p.kind === 'life.simulation')).toBe(false)
    } finally { await internal.close() }
  })

  it('isolates internal memory tools and concludes the committed summary without another model request', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('forbidden', 'send_leak', { text: 'private source' }),
      toolCallResponse('read', 'life_read', { proposal: { kind: 'memory_batch' } }),
      toolCallResponse('commit', 'life_remember', { proposal: { kind: 'organized_memory', claims: [] } }),
    ], false, true, true)
    let sent = 0
    let parent: ToolExecutionToken | undefined
    f.ctx.on('tools/result', exec => { if (exec.name === 'life_read') parent = exec.token })
    const unregister = f.ctx.tools.register(defineTool({ name: 'send_leak', description: 'An external sender.',
      parameters: { text: { type: 'string', required: true } },
      output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
      execute: async () => { sent++; return 'sent' } }))
    f.request.mockImplementation(async (_method, params) => {
      if ('kind' in params && (params.kind === 'context' || params.kind === 'static_context')) {
        return { ok: true, processing_mode: 'internal', stable_text: 'Same role, internal evidence only.', text: 'batch:one', continuity_enabled: false }
      }
      if ('kind' in params && params.kind === 'life.remember') return { ok: true, memory_id: 'summary-one', turn_complete: true }
      return { ok: true, items: [] }
    })
    try {
      await f.sessions.handle('input', { ...input, source: 'inference' })
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(sent).toBe(0)
      expect(f.adapter.requests).toHaveLength(3)
      expect(f.ctx.tools.schemas(agent).map(tool => tool.name).sort()).toMatchInlineSnapshot(`
        [
          "life_read",
          "life_remember",
          "life_rest",
        ]
      `)
      expect(f.request.mock.calls.some(([, params]) => 'kind' in params && params.kind === 'life.say')).toBe(false)
      expect(f.ctx.tools.schemas().some(tool => tool.name === 'send_leak')).toBe(true)
      const removeScoped = agent.ctx.tools.register(defineTool({ name: 'scoped_leak', description: 'Scoped sender.',
        parameters: {}, output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
        execute: async () => { sent++; return 'sent' } }))
      try {
        if (!parent) throw new Error('missing actual parent token')
        const blocked = await f.ctx.tools.execute({ callId: ToolCallId('scoped-attempt'), parent,
          name: 'scoped_leak', arguments: {}, signal: new AbortController().signal, agent })
        expect(blocked.isError).toBe(true)
        expect(sent).toBe(0)
      } finally { removeScoped() }
      f.sessions.dispose()
      expect(f.ctx.tools.schemas(agent).some(tool => tool.name === 'life_read')).toBe(false)
    } finally { unregister(); await f.close() }
  })

  it('returns consultation admission before completion and collects one native job without a second notice', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('consult-1', 'life_consult', { proposal: { message: 'A synthetic consultation.' } }),
      toolCallResponse('say-1', 'life_say', { proposal: { text: 'Still available while consulting.' } }),
      toolCallResponse('rest-1', 'life_rest', { proposal: {} }),
    ], false, false, true, true)
    const normal = f.request.getMockImplementation()!
    const worker = join(f.root, 'consult-worker.mjs')
    await writeFile(worker, `import { createInterface } from 'node:readline';
      const input=createInterface({input:process.stdin});
      input.once('line',line=>{const value=JSON.parse(line);
        if(value.action!=='start')process.exit(2);
        import('node:fs').then(fs=>{const gate=setInterval(()=>{
          if(fs.existsSync(${JSON.stringify(join(f.root, 'release'))})){
            clearInterval(gate);input.close();process.stdin.destroy();process.exit(0)}},10)})});`)
    let settled = false
    f.request.mockImplementation(async (method, params) => {
      if ('kind' in params && params.kind === 'life.consult') return { ok: true, accepted: true, launch: true,
        request_id: 'request-fixture', topic_id: 'topic-fixture', status: 'queued', argv: [process.execPath, worker],
        cwd: f.root, grace_ms: 1000, timeout_ms: 10000 }
      if ('kind' in params && params.kind === 'consult_bind') return { ok: true }
      if ('kind' in params && params.kind === 'consult_settle') { settled = true; return { ok: true } }
      if ('kind' in params && params.kind === 'life.say') {
        expect(settled).toBe(false)
        return { ok: true, sent_confirmed: true, turn_complete: false }
      }
      return normal(method, params)
    })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      await writeFile(join(f.root, 'release'), 'release after role remained responsive')
      await vi.waitFor(() => expect(settled, JSON.stringify({ jobs: f.ctx.jobs.list(SessionId('main')),
        callbacks: f.request.mock.calls, messages: f.adapter.requests[1]?.messages })).toBe(true), { timeout: 8000 })
      expect(f.adapter.requests).toHaveLength(3)
      expect(f.ctx.jobs.list(SessionId('main')).map(job => ({ kind: job.kind, status: job.status })))
        .toMatchInlineSnapshot(`
          [
            {
              "kind": "codex_consult",
              "status": "completed",
            },
          ]
        `)
      expect(f.request.mock.calls.filter(([, params]) => 'kind' in params && params.kind === 'consult_bind')).toHaveLength(1)
      const agent = f.ctx.agents.get(SessionId('main'))!
      expect(f.ctx.tools.schemas(agent).some(tool => tool.name === 'life_consult')).toBe(true)
      f.sessions.dispose()
      expect(f.ctx.tools.schemas(agent).some(tool => tool.name === 'life_consult')).toBe(false)
    } finally { await f.close() }
  })

  it('continues one idle activity through messages and work until the host confirms its ending', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('say-a', 'life_say', { proposal: { text: 'An idea to share.' } }),
      toolCallResponse('work-a', 'activity_read', {}),
      toolCallResponse('say-b', 'life_say', { proposal: { text: 'Another observation.' } }),
      toolCallResponse('rest-a', 'life_rest', { proposal: {} }),
    ], false, false, true)
    const normal = f.request.getMockImplementation()!
    f.request.mockImplementation(async (method, params) => {
      if ('kind' in params && params.kind === 'life.say') return { ok: true, sent_confirmed: true, turn_complete: false }
      return normal(method, params)
    })
    f.ctx.effect(() => f.ctx.tools.register(defineTool({ name: 'activity_read', description: 'Read the next observation.',
      parameters: {}, output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: async () => ({ observation: 'actual local result' }) })))
    try {
      await f.sessions.handle('input', { ...input, source: 'inference', content: 'An idle opportunity.' })
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(f.adapter.requests).toHaveLength(4)
      expect(f.request.mock.calls.filter(([, params]) => 'kind' in params && params.kind === 'before_tool')
        .map(([, params]) => 'tool' in params ? params.tool : null)).toMatchInlineSnapshot(`
        [
          "life_say",
          "activity_read",
          "life_say",
          "life_rest",
        ]
      `)
      expect(JSON.stringify(f.adapter.requests[2]!.messages)).toContain('actual local result')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('Sending normally continues the current activity')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('updates the independent private persona source')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('persona_audience:private|public')
      const page = await f.sessions.handle('events', { session_id: 'main', after_seq: -1 })
      if (!Array.isArray(page.events)) throw new Error('missing activity events')
      expect(page.events.filter(event => event.type === 'turn/start')).toHaveLength(1)
      expect(page.events.filter(event => event.type === 'turn/end')).toHaveLength(1)
    } finally { await f.close() }
  })

  it('ends on a confirmed final expression and keeps a failed ending available for correction', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('say-pending', 'life_say', { proposal: { text: 'Not confirmed.', end_turn: true } }),
      toolCallResponse('say-final', 'life_say', { proposal: { text: 'The confirmed final.', end_turn: true } }),
    ])
    const normal = f.request.getMockImplementation()!
    f.request.mockImplementation(async (method, params) => {
      if ('kind' in params && params.kind === 'life.say') return { ok: true, turn_complete: 'call_id' in params && params.call_id === 'say-final' }
      return normal(method, params)
    })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(f.adapter.requests).toHaveLength(2)
    } finally { await f.close() }
  })

  it('requires durable host admission before ordinary and compaction provider requests', async () => {
    const f = await fixture()
    const normalReply = f.request.getMockImplementation()!
    f.request.mockImplementation(async (method, params) => {
      if ('kind' in params && params.kind === 'budget_reserve') return { ok: false, code: 'budget_limit', message: 'Daily allowance exhausted' }
      return normalReply(method, params)
    })
    try {
      await f.sessions.handle('input', input)
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(0)
      const summary = async () => {
        for await (const chunk of f.ctx.llm.stream({ provider: 'mock', model: 'mock', sessionId: agent.session.id,
          purpose: 'compaction', maxTokens: 8192, messages: [] })) void chunk
      }
      await expect(summary()).rejects.toThrow('Daily allowance exhausted')
      const child = (await f.ctx.agents.create({ sessionId: SessionId('budget-child'), parentAgent: agent,
        meta: { parentSession: agent.session.id }, agentOptions: { provider: 'mock', model: 'mock' } })).agent
      const childCall = async () => {
        for await (const chunk of f.ctx.llm.stream({ provider: 'mock', model: 'mock', sessionId: child.session.id,
          maxTokens: 8192, messages: [] })) void chunk
      }
      await expect(childCall()).rejects.toThrow('Daily allowance exhausted')
      expect(f.adapter.requests).toHaveLength(0)
      expect(f.request.mock.calls.flatMap(([, params]) => 'kind' in params && params.kind === 'budget_reserve'
        && 'record' in params && params.record && typeof params.record === 'object' && 'purpose' in params.record
        ? [params.record.purpose] : [])).toMatchInlineSnapshot(`
        [
          "ordinary",
          "compaction",
          "ordinary",
        ]
      `)
    } finally { await f.close() }
  })

  it('discovers a newly registered ordinary tool and keeps its real hook/result path', async () => {
    const f = await fixture(undefined, [toolCallResponse('new-capability', 'synthetic_plugin_read', { topic: 'watercolor' }),
      toolCallResponse('done', 'life_rest', { proposal: {} })], false, true)
    let calls = 0
    const undo = f.ctx.tools.register(defineTool({ name: 'synthetic_plugin_read', description: 'Read a synthetic public sample.',
      parameters: { topic: { type: 'string', required: true } },
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: async args => ({ topic: args.topic, observations: ++calls }),
    }))
    try {
      await f.sessions.handle('input', { ...input, source: 'self_output', content: 'An existing interest offers a small question.' })
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      const catalog = await f.sessions.handle('catalog', { session_id: 'main', generation: 1 })
      expect(JSON.stringify(catalog)).toContain('synthetic_plugin_read')
      expect(calls).toBe(1)
      expect(f.request.mock.calls.filter(([, params]) => 'kind' in params && params.kind === 'before_tool')
        .map(([, params]) => 'tool' in params ? params.tool : null)).toMatchInlineSnapshot(`
        [
          "synthetic_plugin_read",
          "life_rest",
        ]
      `)
      expect(JSON.stringify(f.adapter.requests[1]!.messages)).toContain('watercolor')
      undo()
      expect(JSON.stringify(await f.sessions.handle('catalog', { session_id: 'main', generation: 1 }))).not.toContain('synthetic_plugin_read')
      const count = f.ctx.agents.list().length
      expect(await f.sessions.handle('catalog', { session_id: 'absent', generation: 1 })).toMatchObject({ ok: false, code: 'stale_context' })
      expect(f.ctx.agents.list()).toHaveLength(count)
    } finally { undo(); await f.close() }
  })

  it('loads hash-bound methods only through their Agent provider and removes scoped registrations', async () => {
    const f = await fixture(undefined, [toolCallResponse('rest', 'life_rest', { proposal: {} })], false, true)
    let hash = 'hash-v1'
    const method = (): Record<string, unknown> => ({ ref: 'method:bot:qq-method-sample', name: 'qq-method-sample',
      summary: 'A scoped sample method.', hash, revision: 1 })
    f.request.mockImplementation(async (methodName, params) => {
      if ('kind' in params && params.kind === 'method_catalog') {
        return { ok: true, items: 'session_id' in params && params.session_id === 'main' ? [method()] : [] }
      }
      if ('kind' in params && params.kind === 'method_get') {
        if ('hash' in params && params.hash !== hash) return { ok: false, code: 'stale_context', message: 'Method changed.' }
        return { ok: true, hash, text: `Method body ${hash}` }
      }
      return methodName === 'qq_life/life/read' ? { ok: true, text: 'Synthetic role.' } : { ok: true }
    })
    try {
      await f.sessions.handle('input', input)
      const main = f.ctx.agents.get(SessionId('main'))!
      await main.whenIdle()
      expect((await f.ctx.skills.list({ scope: main })).map(skill => skill.name)).toEqual(['qq-method-sample'])
      expect(await f.ctx.skills.list()).toEqual([])
      expect((await f.ctx.skills.get('qq-method-sample', { scope: main }))?.content).toBe('Method body hash-v1')
      hash = 'hash-v2'
      await expect(f.ctx.skills.get('qq-method-sample', { scope: main })).rejects.toThrow('Method changed')
      await f.sessions.handle('methods_changed', { session_id: 'main', generation: 1 })
      expect((await f.ctx.skills.get('qq-method-sample', { scope: main }))?.content).toBe('Method body hash-v2')
      const child = (await f.ctx.agents.create({ sessionId: SessionId('child'), parentAgent: main,
        meta: { parentSession: main.session.id }, agentOptions: { provider: 'mock', model: 'mock' } })).agent
      expect(await f.ctx.skills.list({ scope: child })).toEqual([])
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('capability_protocol:1')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('media_adapter_protocol:1')
      f.sessions.dispose()
      expect(await f.ctx.skills.list({ scope: main })).toEqual([])
    } finally { await f.close() }
  })

  it('cancels the matched in-flight call while retaining a newer inbox input', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('old-call', 'wait_cancel', {}),
      toolCallResponse('rest', 'life_rest', { proposal: {} }),
    ], false, false, true)
    let reached: () => void = () => undefined
    const entered = new Promise<void>(resolve => { reached = resolve })
    let aborted = false
    f.ctx.tools.register(defineTool({ name: 'wait_cancel', description: 'Controlled cancellable work.', parameters: {},
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: async (_args, exec) => {
        reached()
        await new Promise<void>(resolve => {
          if (exec.signal.aborted) resolve()
          else exec.signal.addEventListener('abort', () => { aborted = true; resolve() }, { once: true })
        })
        return { stopped: true }
      },
    }))
    try {
      await f.sessions.handle('input', input)
      await entered
      await f.sessions.handle('control', { session_id: 'main', target_session_ids: [],
        parent_calls: [{ session_id: 'main', call_id: 'unrelated-call' }] })
      expect(aborted).toBe(false)
      await f.sessions.handle('input', { ...input, input_id: 'newer', content: 'A new interruption.' })
      const result = await f.sessions.handle('control', { session_id: 'main', target_session_ids: [],
        parent_calls: [{ session_id: 'main', call_id: 'old-call' }] })
      expect(result.sessions).toEqual(['main'])
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(aborted).toBe(true)
      const newer = await f.sessions.handle('input_status', { session_id: 'main', input_id: 'newer' })
      expect(['accepted', 'consumed', 'settled']).toContain(newer.state)
    } finally { await f.close() }
  })

  it('negotiates continuity descriptions and preserves a recorded thought proposal', async () => {
    const f = await fixture(undefined, [toolCallResponse('thought-1', 'life_rest', { proposal: {
      thought: 'Continue reading the original source later.', keep_thought: true, thought_cues: ['source'], handled_input_ids: ['input-1'],
    } })], true)
    const errors: string[] = []
    f.ctx.on('agent/error', ({ error }) => { errors.push(String(error)) })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(errors).toEqual([])
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('continuity_protocol:1')
      const call = f.request.mock.calls.find(([, params]) => 'kind' in params && params.kind === 'life.rest')
      expect(call?.[1]).toMatchObject({ proposal: { keep_thought: true, thought_cues: ['source'] } })
      expect(f.request.mock.calls.filter(([, params]) => 'kind' in params && params.kind === 'life.rest')
        .map(([, params]) => 'proposal' in params ? params.proposal : null)).toMatchInlineSnapshot(`
        [
          {
            "handled_input_ids": [
              "input-1",
            ],
            "keep_thought": true,
            "thought": "Continue reading the original source later.",
            "thought_cues": [
              "source",
            ],
          },
        ]
      `)
    } finally { await f.close() }
  })

  it('keeps base tools available when the host has not enabled continuity', async () => {
    const f = await fixture()
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).not.toContain('continuity_protocol:1')
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('life_rest')
    } finally { await f.close() }
  })

  it('captures child creation against the actual executing call', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('spawn-1', 'probe_child', {}),
      toolCallResponse('spawn-2', 'probe_child', {}),
      toolCallResponse('rest', 'life_rest', { proposal: {} }),
    ])
    const captured: string[] = []
    f.ctx.tools.register(defineTool({ name: 'probe_child', description: 'Controlled child creation fixture.', parameters: {},
      output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: async (_args, exec) => {
        await Promise.resolve()
        const call = f.sessions.captureChildCall('main', `child-${String(exec.callId)}`)
        expect(f.sessions.captureChildCall('unrelated', 'invalid-child')).toBeUndefined()
        captured.push(String(call))
        return { parentCallId: call ?? null }
      },
    }))
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(captured).toMatchInlineSnapshot(`
        [
          "spawn-1",
          "spawn-2",
        ]
      `)
    } finally { await f.close() }
  })

  it('submits local media and rests without waiting for model inference', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('media-1', 'life_media', { proposal: { action: 'submit', kind: 'speech', text: 'hello' } }),
      toolCallResponse('rest-1', 'life_rest', { proposal: { handled_input_ids: ['input-1'] } }),
    ])
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(JSON.stringify(f.adapter.requests[0]!.tools)).toContain('life_media')
      const calls = f.request.mock.calls
        .filter(([, params]) => 'kind' in params && (params.kind === 'life.media' || params.kind === 'life.rest'))
        .map(([method, params]) => ({ method, ...('kind' in params ? { kind: params.kind } : {}),
          ...('proposal' in params ? { proposal: params.proposal } : {}) }))
      expect(calls).toMatchInlineSnapshot(`
        [
          {
            "kind": "life.media",
            "method": "qq_life/life/commit",
            "proposal": {
              "action": "submit",
              "kind": "speech",
              "text": "hello",
            },
          },
          {
            "kind": "life.rest",
            "method": "qq_life/life/commit",
            "proposal": {
              "handled_input_ids": [
                "input-1",
              ],
            },
          },
        ]
      `)
    } finally { await f.close() }
  })
  it('leaves multimodal content intact under the text-only policy', async () => {
    const f = await fixture()
    let execution: ToolExecution | undefined
    f.ctx.on('tools/result', (exec) => { execution = exec })
    try {
      await f.sessions.handle('input', input)
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      if (!execution) throw new Error('missing execution')
      const resources = new QqLifeResources(f.ctx, () => f.root, () => ({ root: 'main', child: false }))
      resources.configure(agent, { text_preview_target_tokens: 1, text_result_max_tokens: 2, text_result_max_utf8_bytes: 4 }, { turn: 1, step: 1 })
      const content: ContentBlock[] = [{ type: 'text', text: 'large text'.repeat(1000) }, { type: 'image', attachment: {
        attachmentId: AttachmentId(`sha256:${'a'.repeat(64)}`), mediaType: 'image/png', width: 1, height: 1, bytes: 1 } }]
      expect(await resources.present(execution, content, false)).toBe(content)
    } finally { await f.close() }
  })
  it('keeps the role running when usage history is damaged and reports the accounting gap', async () => {
    const f = await fixture()
    await mkdir(join(f.root, 'qq-life'), { recursive: true })
    await writeFile(join(f.root, 'qq-life', 'usage.jsonl'), 'invalid old record\n')
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(f.request.mock.calls.some(([, params]) => 'kind' in params && params.kind === 'life.say')).toBe(true)
      const page = await f.sessions.handle('usage', { session_id: 'main', after_seq: -1 })
      expect(page.accounting_warning).toBe(true)
      expect(page.records).toHaveLength(2)
    } finally { await f.close() }
  })
  it('keeps unsaved full results visible and executes repeated commands again', async () => {
    const f = await fixture(undefined, [toolCallResponse('log-a', 'long_log', {}),toolCallResponse('log-b', 'long_log', {}),
      toolCallResponse('rest-1', 'life_rest', { proposal: { handled_input_ids: ['input-1'] } })])
    const original = 'unsaved diagnostic\n'.repeat(1000)
    let executions = 0
    f.ctx.effect(() => f.ctx.tools.register(defineTool({ name: 'long_log', description: 'Fixture log', parameters: {},
      output: { schema: { type: 'json' }, render: () => [{ type: 'text', text: original }] },
      execute: async () => ({ invocation: ++executions }) })))
    f.request.mockImplementation(async (method, params) => method === 'qq_life/life/read'
      ? { ok: true, text: 'test', resource_policy: { text_preview_target_tokens: 1500, text_result_max_tokens: 2000, text_result_max_utf8_bytes: 12000 } }
      : { ok: true, params })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(executions).toBe(2)
      expect(JSON.stringify(f.adapter.requests[1]!.messages)).toContain('unsaved diagnostic')
      const page = await f.sessions.handle('events', { session_id: 'main', after_seq: -1 })
      if (!Array.isArray(page.events)) throw new Error('missing events')
      expect(JSON.stringify(page.events.filter(event => event.type === 'tool/result'))).not.toContain('result_ref')
    } finally { await f.close() }
  })
  it('retains a real full-result reference while bounding text and preserving canonical values', async () => {
    const f = await fixture(undefined, [toolCallResponse('log-1', 'long_log', {}),
      toolCallResponse('rest-1', 'life_rest', { proposal: { handled_input_ids: ['input-1'] } })])
    const original = Array.from({ length: 600 }, (_, i) => `${i}: ${'诊断🙂'.repeat(12)}`).join('\n')
    class Store extends SpillStore {
      async saveText(input: SaveTextSpill) {
        const path = join(f.root, 'full-result.txt')
        await writeFile(path, input.content)
        return { locator: SpillLocator(path), bytes: Buffer.byteLength(input.content), retrievalHint: 'read this local result' }
      }
    }
    await f.ctx.plugin(Store)
    f.ctx.effect(() => f.ctx.tools.register(defineTool({ name: 'long_log', description: 'Fixture log', parameters: {},
      output: { schema: { type: 'json' }, render: () => [{ type: 'text', text: original }] },
      execute: async () => ({ exit_code: 1, canonical_marker: 'preserved' }) })))
    let parent: ToolExecutionToken | undefined
    f.ctx.on('tools/result', (exec, result) => {
      if (exec.name === 'long_log' && !exec.parent) {
        parent = exec.token
        expect(result.isError).toBe(false)
        expect(result.value).toEqual({ exit_code: 1, canonical_marker: 'preserved' })
      }
    })
    f.request.mockImplementation(async (method, params) => method === 'qq_life/life/read'
      ? { ok: true, text: 'bounded result test', resource_policy: {
        text_preview_target_tokens: 1500, text_result_max_tokens: 2000, text_result_max_utf8_bytes: 12000 } }
      : { ok: true, params })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      const messages = f.adapter.requests[1]!.messages
      const tool = messages.find(message => message.role === 'tool')
      if (!tool || tool.role !== 'tool' || tool.content[0]?.type !== 'text') throw new Error('missing preview')
      const envelope = JSON.parse(tool.content[0].text)
      expect(Buffer.byteLength(tool.content[0].text)).toBeLessThanOrEqual(4000)
      expect(envelope.coverage).toBe('partial')
      expect(envelope.exit_code).toBe(1)
      expect(envelope.status).toBe('failed')
      expect(envelope.total_lines).toBe(600)
      const full = await f.sessions.handle('original', { session_id: 'main', result_ref: envelope.result_ref })
      expect(full.text).toBe(original)
      const absent = await f.sessions.handle('original', { session_id: 'main', result_ref: 'not-owned' })
      expect(absent).toMatchObject({ ok: false, code: 'scope_mismatch' })
      if (!parent) throw new Error('missing actual parent token')
      const nested = await f.ctx.tools.execute({ callId: ToolCallId('nested-program'), parent, name: 'long_log',
        arguments: {}, signal: new AbortController().signal, agent: f.ctx.agents.get(SessionId('main'))! })
      expect(nested.value).toEqual({ exit_code: 1, canonical_marker: 'preserved' })
      expect(nested.content).toEqual([{ type: 'text', text: original }])
    } finally { await f.close() }
  })
  it('records failed summary calls and retries with separate identities and no invented usage', async () => {
    const f = await fixture(undefined, [toolCallResponse('rest-1', 'life_rest', { proposal: { handled_input_ids: ['input-1'] } }),
      [{ type: 'finish', reason: { kind: 'error', failure: { message: 'summary failure', code: 'test_failure' } } }],
      [{ type: 'usage', usage: { inputTokens: 3, outputTokens: 4, cacheReadTokens: 7, reasoningTokens: 2 } },
        { type: 'finish', reason: { kind: 'stop' } }]])
    try {
      await f.sessions.handle('input', input)
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      for (let i = 0; i < 2; i++) {
        for await (const chunk of f.ctx.llm.stream({ provider: 'mock', model: 'mock', sessionId: agent.session.id, purpose: 'compaction', messages: [] })) void chunk
      }
      const page = await f.sessions.handle('usage', { session_id: 'main', after_seq: -1 })
      expect(page.records).toMatchObject([
        { revision: 1 }, { revision: 2 }, { revision: 1, purpose: 'compaction' },
        { revision: 2, status: 'failed' }, { revision: 1, purpose: 'compaction' },
        { revision: 2, usage: { inputTokens: 3, cacheReadTokens: 7 } },
      ])
      if (!Array.isArray(page.records)) throw new Error('missing usage')
      expect(new Set(page.records.map(record => record.call_key)).size).toBe(3)
      expect(page.records[3]).not.toHaveProperty('usage')
    } finally { await f.close() }
  })
  it('exposes relationship v2 and records cumulative usage once per actual stream', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('app-1', 'life_appraise', { proposal: { sources: ['input-1'], relation_protocol: 2,
        subject_actor_id: '1', based_on_relationship_version: 1,
        relation_impacts: [{ metric: 'bond', direction: 'increase', magnitude: 'meaningful', confidence: 0.8 }] } }),
      toolCallResponse('rest-1', 'life_rest', { proposal: { handled_input_ids: ['input-1'] } }),
    ])
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      const declarations = JSON.stringify(f.adapter.requests[0]!.tools)
      expect(declarations).toContain('relation_protocol:2')
      expect(declarations).toContain('based_on_relationship_version')
      expect(declarations).toContain('relationship_note')
      const callbacks = f.request.mock.calls.filter(([, params]) => 'kind' in params && params.kind === 'life.appraise')
      expect(callbacks).toHaveLength(1)
      expect(callbacks[0]![1]).toMatchObject({ proposal: { relation_protocol: 2, subject_actor_id: '1' } })
      const page = await f.sessions.handle('usage', { session_id: 'main', generation: 1, after_seq: -1 })
      expect(page.records).toMatchObject([
        { revision: 1, status: 'started' }, { revision: 2, status: 'completed', usage: { inputTokens: 10, outputTokens: 5 } },
        { revision: 1, status: 'started' }, { revision: 2, status: 'completed', usage: { inputTokens: 10, outputTokens: 5 } },
      ])
    } finally { await f.close() }
  })
  it('keeps stable rules in a section and refreshes a complete logged dynamic snapshot', async () => {
    const f = await fixture()
    f.request.mockImplementation(async (method, params) => method === 'qq_life/life/read'
      ? { ok: true, text: 'CURRENT-SNAPSHOT', stable_text: 'STABLE-RELATION-RULES', resource_policy: {
        text_preview_target_tokens: 1500, text_result_max_tokens: 2000, text_result_max_utf8_bytes: 12000 } }
      : { ok: true, params })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      const messages = f.adapter.requests[0]!.messages
      expect(JSON.stringify(messages)).toContain('STABLE-RELATION-RULES')
      expect(messages.some(message => message.role === 'user' && JSON.stringify(message).includes('CURRENT-SNAPSHOT'))).toBe(true)
      const page = await f.sessions.handle('events', { session_id: 'main', after_seq: -1 })
      expect(JSON.stringify(page)).toContain('CURRENT-SNAPSHOT')
      expect(JSON.stringify(page)).toContain('STABLE-RELATION-RULES')
    } finally { await f.close() }
  })
  it('sends one latest snapshot across tool steps and replays the same history without old snapshots', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('read-1', 'life_read', { kind: 'self' }),
      toolCallResponse('read-2', 'life_read', { kind: 'relationship' }),
      toolCallResponse('rest', 'life_rest', { proposal: {} }),
    ], false, false, true)
    let revision = 0
    const ordinary = f.request.getMockImplementation()!
    f.request.mockImplementation(async (method, params) => {
      if ('kind' in params && params.kind === 'context') return {
        ok: true, text: `当前状态与材料：\n${JSON.stringify({ local_time: `time-${++revision}`, reminders: 'KEEP-REMINDER', revision })}`,
        stable_text: 'STABLE-IDENTITY',
      }
      if ('kind' in params && params.kind === 'static_context') return { ok: true, stable_text: 'STABLE-IDENTITY' }
      return ordinary(method, params)
    })
    try {
      await f.sessions.handle('input', input)
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(3)
      const states = f.adapter.requests.map(request => request.messages.filter(message => message.source?.kind === 'runtime-context'))
      expect(states.map(messages => messages.length)).toEqual([1, 1, 1])
      for (const [index, messages] of states.entries()) {
        expect(JSON.stringify(messages)).toContain(`time-${index + 1}`)
        expect(JSON.stringify(messages)).toContain('KEEP-REMINDER')
      }
      expect(f.adapter.requests[2]!.messages.filter(message => message.role === 'tool')).toHaveLength(2)
      expect(f.adapter.requests[2]!.messages.some(message => message.source?.kind === 'qq-life')).toBe(true)
      expect(f.adapter.requests[2]!.messages.filter(message => message.role === 'system')).toHaveLength(1)
      const events = agent.session.snapshotEvents()
      expect(events.filter(event => event.type === 'user/message' && event.data.source.kind === 'runtime-context')).toHaveLength(3)
      expect(events.filter(event => event.type === 'compaction/prune')).toHaveLength(2)
      const restored = Session.fromRestore(agent.session.id, events, agent.session.header,
        agent.session.inheritedEventCount, 'shared-frozen')
      expect(restored.deriveMessages()).toEqual(agent.session.deriveMessages())
      expect(restored.deriveMessages().filter(message => message.source.kind === 'runtime-context')).toHaveLength(1)
      expect(restored.deriveMessages().some(message => message.role === 'developer' && message.content.length === 0)).toBe(false)
      expect(f.adapter.requests.map(request => request.messages.map(message => message.role))).toMatchInlineSnapshot(`
        [
          [
            "system",
            "user",
            "user",
          ],
          [
            "system",
            "user",
            "assistant",
            "tool",
            "user",
          ],
          [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "user",
          ],
        ]
      `)
    } finally { await f.close() }
  })

  it('retires legacy snapshots inside an open step while preserving real inputs and unrelated sessions', async () => {
    const f = await fixture(undefined, [toolCallResponse('rest', 'life_rest', { proposal: {} })])
    const counts: number[] = []
    f.ctx.on('agent/pre-step', async ({ agent }, next) => {
      counts.push(agent.session.deriveMessages().filter(message => message.source.kind === 'runtime-context').length)
      return next()
    })
    try {
      await f.sessions.handle('events', { session_id: 'main', generation: 1, after_seq: -1 })
      const agent = f.ctx.agents.get(SessionId('main'))!
      for (let i = 0; i < 42; ++i) agent.session.append('user/message', createUserMessage({
        content: [{ type: 'text', text: `LEGACY-STATE-${i}: ` + 'large repeated material '.repeat(500) }],
        source: { kind: 'runtime-context' },
      }), { surfaceOp: 'append' })
      agent.session.append('user/message', createUserMessage({
        content: [{ type: 'text', text: 'A real previous promise must survive.' }], source: { kind: 'user' },
      }), { surfaceOp: 'append' })
      const beforeTokens = f.ctx.tokenMeter.measure(agent.session).surfaceTokens
      const unrelated = (await f.ctx.agents.create({ sessionId: SessionId('not-bot'),
        agentOptions: { provider: 'mock', model: 'mock' } })).agent
      const otherSnapshot = unrelated.session.append('user/message', createUserMessage({
        content: [{ type: 'text', text: 'UNRELATED-CONTEXT' }], source: { kind: 'runtime-context' },
      }), { surfaceOp: 'append' })
      await f.sessions.handle('input', input)
      await agent.whenIdle()
      expect(counts).toEqual([42])
      const events = agent.session.snapshotEvents()
      const firstStep = events.find(event => event.type === 'step/start')!
      const replacements = events.filter(event => event.type === 'developer/message')
      expect(replacements).toHaveLength(42)
      expect(replacements.every(event => event.seq > firstStep.seq)).toBe(true)
      expect(JSON.stringify(f.adapter.requests[0]!.messages)).not.toContain('LEGACY-STATE-')
      expect(JSON.stringify(f.adapter.requests[0]!.messages)).toContain('A real previous promise must survive.')
      expect(agent.session.snapshotEvents().filter(event => event.type === 'compaction/prune')).toHaveLength(42)
      expect(f.ctx.tokenMeter.measure(agent.session).surfaceTokens).toBeLessThan(beforeTokens / 10)
      expect(unrelated.session.surface.nodes).toContain(otherSnapshot.seq)
      expect(JSON.stringify(unrelated.session.deriveMessages())).toContain('UNRELATED-CONTEXT')
    } finally { await f.close() }
  })

  it('retains the last complete snapshot when the host cannot supply its replacement', async () => {
    const f = await fixture()
    try {
      await f.sessions.handle('events', { session_id: 'main', generation: 1, after_seq: -1 })
      const agent = f.ctx.agents.get(SessionId('main'))!
      agent.session.append('user/message', createUserMessage({
        content: [{ type: 'text', text: 'LAST-VALID-STATE' }], source: { kind: 'runtime-context' },
      }), { surfaceOp: 'append' })
      const ordinary = f.request.getMockImplementation()!
      f.request.mockImplementation(async (method, params) => 'kind' in params && params.kind === 'context'
        ? { ok: false, code: 'unavailable', message: 'Host unavailable' } : ordinary(method, params))
      await f.sessions.handle('input', input)
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(0)
      expect(JSON.stringify(agent.session.deriveMessages())).toContain('LAST-VALID-STATE')
      expect(agent.session.snapshotEvents().filter(event => event.type === 'compaction/prune')).toHaveLength(0)
    } finally { await f.close() }
  })

  it('compacts only retained context and restores a fresh snapshot on the following turn', async () => {
    const f = await fixture(undefined, [
      toolCallResponse('read', 'life_read', { kind: 'self' }),
      toolCallResponse('rest-before', 'life_rest', { proposal: {} }),
      textResponse('The user asked about their project; keep the resulting verified facts.'),
      toolCallResponse('rest-after', 'life_rest', { proposal: {} }),
    ])
    let revision = 0
    const ordinary = f.request.getMockImplementation()!
    f.request.mockImplementation(async (method, params) => 'kind' in params && params.kind === 'context'
      ? { ok: true, text: `STATE-REVISION-${++revision}\n` + 'current reminder '.repeat(150) }
      : ordinary(method, params))
    try {
      await f.ctx.plugin(BasicCompactionEngine, { auto: false })
      await f.sessions.handle('input', { ...input, content: 'A real long project conversation. '.repeat(300) })
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      const result = await f.ctx.compaction.compactNow(agent, new AbortController().signal)
      expect(result).not.toBeNull()
      const request = f.adapter.requests.find(request => request.purpose === 'compaction')!
      expect(request).toBeDefined()
      expect(JSON.stringify(request.messages)).not.toContain('STATE-REVISION-1')
      expect(request.messages.filter(message => message.source?.kind === 'runtime-context').length).toBeLessThanOrEqual(1)
      await f.sessions.handle('input', { ...input, input_id: 'input-2', content: 'Continue after compaction.' })
      await agent.whenIdle()
      const following = f.adapter.requests.at(-1)!
      expect(following.purpose).not.toBe('compaction')
      expect(following.messages.filter(message => message.source?.kind === 'runtime-context')).toHaveLength(1)
      expect(JSON.stringify(following.messages)).toContain('STATE-REVISION-3')
      expect(JSON.stringify(following.messages)).not.toContain('STATE-REVISION-1')
    } finally { await f.close() }
  })
  it('does not requeue an input removed from the inbox during request preparation', async () => {
    const f = await fixture()
    let release: () => void = () => undefined
    let entered: () => void = () => undefined
    const gate = new Promise<void>(resolve => { release = resolve })
    const reached = new Promise<void>(resolve => { entered = resolve })
    f.request.mockImplementation(async (method,params) => {
      if (method === 'qq_life/life/read' && 'kind' in params && params.kind === 'context') {
        entered(); await gate; return { ok: true, text: 'test role' }
      }
      return { ok: true, params }
    })
    try {
      await f.sessions.handle('input',input)
      await reached
      const agent = f.ctx.agents.get(SessionId('main'))!
      expect(agent.status).toBe('running')
      expect(await f.sessions.handle('input_status',{ session_id:'main',input_id:'input-1' }))
        .toMatchObject({ state:'accepted' })
      expect([...agent.inbox.nextStep,...agent.inbox.nextTurn]).toHaveLength(0)
      const later = await f.sessions.handle('input',{ ...input,input_id:'input-2',content:'new interruption' })
      expect([...agent.inbox.nextStep,...agent.inbox.nextTurn].map(message => String(message.id)))
        .toContain(later.message_id)
      release()
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(2)
      const page = await f.sessions.handle('events',{ session_id:'main',after_seq:-1 })
      if (!Array.isArray(page.events)) throw new Error('missing events')
      const messages = page.events.filter(event => event.type==='user/message')
      expect(messages.filter(event => event.data.source?.inputId==='input-1')).toHaveLength(1)
      expect(messages.filter(event => event.data.source?.inputId==='input-2')).toHaveLength(1)
    } finally { release(); await f.close() }
  })
  it('can replay a bound session before its first input admission is persisted', async () => {
    const f = await fixture(async () => {
      expect(await f.sessions.handle('events', { session_id: 'main', after_seq: -1 }))
        .not.toHaveProperty('code')
    })
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(f.adapter.requests).toHaveLength(1)
      expect(await f.sessions.handle('events', { session_id: 'unknown', after_seq: -1 }))
        .toMatchObject({ ok: false, code: 'stale_context' })
    } finally { await f.close() }
  })
  it('readmits an input lost before consumption while a later turn is running', async () => {
    const f = await fixture()
    let contexts = 0
    let release!: () => void
    const gate = new Promise<void>(resolve => { release = resolve })
    let entered!: () => void
    const running = new Promise<void>(resolve => { entered = resolve })
    const ordinary = f.request.getMockImplementation()!
    f.request.mockImplementation(async (method, params) => {
      if ('kind' in params && params.kind === 'context') {
        contexts++
        if (contexts === 1) f.ctx.agents.get(SessionId('main'))!.cancel({ kind: 'user' }, { keepInbox: true })
        if (contexts === 2) { entered(); await gate }
      }
      return ordinary(method, params)
    })
    try {
      await f.sessions.handle('input', input)
      const agent = f.ctx.agents.get(SessionId('main'))!
      await agent.whenIdle()
      expect(f.adapter.requests).toHaveLength(0)
      const later = f.sessions.handle('input', { ...input, input_id: 'later', content: 'A later wake' })
      await running
      expect(agent.status).toBe('running')
      await f.sessions.handle('input_status', { session_id: 'main', input_id: 'input-1' })
      expect([...agent.inbox.nextStep, ...agent.inbox.nextTurn].some(message =>
        message.source.kind === 'qq-life' && message.source.inputId === 'input-1')).toBe(true)
      release()
      await later
      await agent.whenIdle()
      expect(await f.sessions.handle('input_status', { session_id: 'main', input_id: 'input-1' })).toMatchObject({ state: 'settled' })
      expect(agent.session.snapshotEvents().filter(event => event.type === 'user/message'
        && event.data.source.kind === 'qq-life' && event.data.source.inputId === 'input-1')).toHaveLength(1)
    } finally { release(); await f.close() }
  })

  it('admits concurrent retries once and exposes consumed input independently of replies', async () => {
    const f = await fixture()
    try {
      const receipts = await Promise.all([f.sessions.handle('input', input), f.sessions.handle('input', input)])
      expect(receipts[0]!.message_id).toBe(receipts[1]!.message_id)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      const status = await f.sessions.handle('input_status', { session_id: 'main', input_id: 'input-1' })
      expect(status.state).toBe('settled')
      expect(f.adapter.requests).toHaveLength(1)
      expect(f.request.mock.calls.filter(([, params]) => 'kind' in params && params.kind === 'life.say')).toHaveLength(1)
      const events = await f.sessions.handle('events', { session_id: 'main', after_seq: -1 })
      expect(events.code).toBeUndefined()
      expect(events.has_more).toBe(false)
    } finally { await f.close() }
  })

  it('rejects changed content under the same input identity and reports impossible replay cursors', async () => {
    const f = await fixture()
    try {
      await f.sessions.handle('input', input)
      await f.ctx.agents.get(SessionId('main'))!.whenIdle()
      expect(await f.sessions.handle('input', { ...input, content: 'different' }))
        .toMatchObject({ ok: false, code: 'idempotency_conflict' })
      expect(await f.sessions.handle('events', { session_id: 'main', after_seq: 99999 }))
        .toMatchObject({ ok: false, code: 'history_gap' })
      expect(f.adapter.requests).toHaveLength(1)
    } finally { await f.close() }
  })
})
