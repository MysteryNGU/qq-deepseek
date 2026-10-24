"""Boot a real isolated SDK composition; optional bounded live choices use synthetic life."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from life.role_config import Config,load_config
from life.services.dsh_bridge import DshBridge
from life.runtime import LifeRuntime
from life.role_agent import RoleAgent
from life.tool_boundary import outcome_ok
from life.agent_session import canonical


def install_fixture(bridge,source,empty_profile=False):
    bridge.ensure_profile()
    profile=bridge.profile_dir();bundle=profile/'node_modules/qq-extension-fixture'
    bundle.mkdir(parents=True)
    package=dict(name='qq-extension-fixture',version='1.0.0',type='module',dsh=dict(bundle=dict(patch='./cordis.patch.yml')))
    (bundle/'package.json').write_text(json.dumps(package),encoding='utf-8')
    tool_module=(source/'packages/core/tools/src/index.ts').as_uri()
    (bundle/'index.mjs').write_text('''import { defineTool } from '''+json.dumps(tool_module)+''';
export const name = 'qq-extension-fixture';
export const inject = ['tools'];
export function apply(ctx) {
  let observations = 0;
  ctx.tools.register(defineTool({ name:'fixture_palette', description:'读取实际固定配色样本及明暗、视觉重心的观察；只读，无网络或账号操作。 Read a synthetic palette.',
    parameters:{}, qqLifeIdentity:{package_id:'qq-extension-fixture',version:'1.0.0',policy:{effect:'read',principal_adoption:false,audience:'owner',account:null,resource:'local'}},
    output:{schema:{type:'json'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    execute:async()=>({observations:++observations,colors:['navy','cream','coral'],finding:'A dark large region and a small warm accent separate focal weight in this fixed sample.'})
  }));
  ctx.on('agent/created', ({agent})=>agent.ctx.tools.restrict({allow:['fixture_palette','read','skill','life_read','life_remember','life_intent','life_rest','life_say','life_appraise','life_change_self','life_media']}));
}
''',encoding='utf-8')
    (bundle/'cordis.patch.yml').write_text('- insert:\n    - id: qq-extension-fixture\n      name: '+json.dumps(str(bundle/'index.mjs'))+'\n',encoding='utf-8')
    manifest=profile/'package.json';value=json.loads(manifest.read_text('utf-8'))
    value['dsh']['profile']['bundles'].append('qq-extension-fixture')
    manifest.write_text(json.dumps(value),encoding='utf-8')
    user_patch='[]\n' if empty_profile else '- id: system-prompt\n  config:\n    personaPrefix: "Synthetic retained profile setting"\n'
    (profile/'cordis.patch.yml').write_text(user_patch,encoding='utf-8')
    bridge.ensure_profile()
    assert (profile/'cordis.patch.yml').read_text('utf-8')==user_patch
    return user_patch


async def run(args):
    with tempfile.TemporaryDirectory(prefix='qq-extension-sdk-') as directory:
        base=Path(directory);(base/'persona.md').write_text('一个谨慎而好奇的合成测试角色。按自己的兴趣选择探索、交流或休息。',encoding='utf-8')
        source=ROOT.parent/'dsh-bridge-work'
        cfg=Config(owner=1,continuity_enabled=True,dsh_root=str(source),dsh_home=str(base/'home'),
            bili_plugin='',api_key='offline-fixture',base_url='http://127.0.0.1:9',model='offline-unrequested',
            dsh_timeout=90)
        if args.live_choices:
            actual=load_config(ROOT/'config.toml')
            cfg.api_key,cfg.base_url,cfg.model,cfg.dsh_provider=actual.api_key,actual.base_url,actual.model,actual.dsh_provider
            cfg.action_per_hour=12
        bridge=DshBridge(cfg,base)
        patch=install_fixture(bridge,source,args.empty_profile)
        life=LifeRuntime(base/'life.db');callbacks=[];expressions=[];choices=[]
        async def send(_scope,_logical,proposal,_sources):
            expressions.append(proposal);return dict(sent_confirmed=True)
        role=RoleAgent(life,bridge,cfg,base,send)
        loop=asyncio.get_running_loop()
        original_callback=role.callback
        async def observed_callback(call):
            if call.get('kind','').startswith('life.'):
                proposal=call.get('proposal') or {}
                choices.append(dict(tool=call['kind'],**{key:proposal[key] for key in ('kind','category','action','query','ref','name','reflection_outcome') if key in proposal}))
            return await original_callback(call)
        role.callback=observed_callback
        def host(call):
            callbacks.append(call.get('kind'))
            return asyncio.run_coroutine_threadsafe(role.callback(call),loop).result(90)
        bridge.register_handler('qq_life/life/read',host);bridge.register_handler('qq_life/life/commit',host)
        try:
            hello=await asyncio.to_thread(bridge.hello)
            assert hello.get('capability_protocol')==1 and hello.get('media_adapter_protocol')==1
            role.capability_protocol=role.media_adapter_protocol=role.continuity_protocol=1
            scope=life.scope('qq:private:1','1');binding=role.sessions.binding(scope)
            sid=binding['session_id']
            await asyncio.to_thread(bridge.session_events,sid,-1,generation=1)
            source_event,_=life.ingest_message(scope,'Synthetic source for deterministic method validation.')
            call=dict(session_id=sid,root_session_id=sid,caller='role',kind='life.change_self',call_id='method-create',
                proposal=dict(target='skill',name='synthetic-study',content='A synthetic method: compare a small fixed palette, keep evidence and uncertainty.',
                    create=True,base_hash=None,metadata=dict(description='Read a palette and keep a bounded observation.')))
            saved=await role.callback(call);assert saved['ok'],saved
            item=role.capabilities.methods.read(scope,name='synthetic-study.md')
            catalog=await asyncio.to_thread(bridge.current_agent_catalog,sid,1)
            assert any(tool['name']=='fixture_palette' for tool in catalog['tools'])
            assert any(method['name']==item['name'] for method in catalog['methods'])
            loaded=await asyncio.to_thread(bridge.execute,'skill',dict(name=item['name']),operation_id='load-method',scope=sid)
            assert outcome_ok(loaded) and item['text'] in canonical(loaded),loaded
            plugin=await asyncio.to_thread(bridge.execute,'fixture_palette',{},operation_id='ordinary-plugin',scope=sid)
            assert outcome_ok(plugin) and 'navy' in canonical(plugin),plugin
            group=life.scope('qq:group:2','2');group_binding=role.sessions.binding(group)
            await asyncio.to_thread(bridge.session_events,group_binding['session_id'],-1,generation=1)
            public_catalog=await asyncio.to_thread(bridge.current_agent_catalog,group_binding['session_id'],1)
            assert not any(method['name']==item['name'] for method in public_catalog['methods'])
            before=await asyncio.to_thread(bridge.resource_events,sid,generation=1)
            assert before.get('records')==[]
            report=dict(schema=1,capability_protocol=1,media_adapter_protocol=1,real_composition=True,
                ordinary_plugin_discovered_and_called=True,native_skill_loaded=True,private_method_not_in_group=True,
                plugin_profile_setting_preserved=True,deterministic_model_requests=0,qq_messages=0,live_choices=[])
            if args.live_choices:
                # External diagnostic turns are not a conversation prefix for a provider.
                # Real choices start on a fresh bound session with only actual model turns.
                role.sessions.retire(scope)
                binding=role.sessions.binding(scope);sid=binding['session_id']
                started=loop.time()
                await role.start()
                with life.repo.tx():
                    role.intent(scope,dict(kind='ongoing',goal='想弄明白一种有层次的配色是怎么形成的，找一点真实观察。',
                        origin='self',bind=False,interest=dict(cues=['配色','palette'],open_question='深浅与暖色点缀怎样分配视觉重心？'),
                        sources=[source_event.id]),binding)
                for index in range(args.live_choices):
                    prior_ops={row['id'] for row in life.repo._all('SELECT id FROM role_operations WHERE session_id=?',(sid,))}
                    prior_choices=len(choices)
                    event,_=life.ingest(scope,'self_opportunity','inference','这是一次已有兴趣的自主机会，可以按当前想法行动、交流或休息；没有新的用户任务。',
                        external_id='synthetic-opportunity-'+str(index),source_event_ids=[source_event.id])
                    await role.ingest(event)
                    await asyncio.sleep(.5)
                    deadline=loop.time()+150
                    while loop.time()<deadline:
                        status=await asyncio.to_thread(bridge.input_status,sid,event.id)
                        if status.get('state')=='settled':
                            break
                        await asyncio.sleep(.5)
                    else:
                        raise TimeoutError('bounded_live_choice_did_not_settle')
                    ops=life.repo._all('SELECT id,tool,status FROM role_operations WHERE session_id=? ORDER BY created',(sid,))
                    report['live_choices'].append(dict(opportunity=index+1,input_state=status.get('state'),
                        actual_tools=[dict(row) for row in ops if row['id'] not in prior_ops],
                        role_choices=choices[prior_choices:],
                        remaining_user_tasks=life.repo._one("SELECT COUNT(*) AS n FROM agent_inputs i JOIN events e ON e.id=i.input_id "
                            "WHERE i.session_id=? AND e.source_kind='user_report' AND i.handled=0",(sid,))['n']))
                usage=await asyncio.to_thread(bridge.resource_events,sid,generation=binding['generation'])
                report['actual_call_records']=len(usage.get('records',[]))
                report['reported_usage']=[row.get('usage') for row in usage.get('records',[]) if row.get('usage')]
                report['call_statuses']=[dict(status=row.get('status'),provider=row.get('provider'),model=row.get('model')) for row in usage.get('records',[]) if row.get('revision')==2]
                events=await asyncio.to_thread(bridge.session_events,sid,-1,generation=binding['generation'])
                report['choice_errors']=[canonical(row.get('data',{})).replace(cfg.api_key,'[redacted]')[:700]
                    for row in events.get('events',[]) if row.get('type') in {'agent/error','turn/error','assistant/attempt'}]
                technical=base/'data/logs/dsh-bridge.log'
                if technical.is_file():
                    report['sdk_error_lines']=[line.replace(cfg.api_key,'[redacted]')[-700:] for line in technical.read_text('utf-8',errors='replace').splitlines()
                        if any(marker in line for marker in ('Error:','error','failed'))][-8:]
                report['live_role_expressions']=len(expressions)
                report['elapsed_live_seconds']=round(loop.time()-started,3)
            report['adapter_sha256']=hashlib.sha256((source/'packages/sdk/server/src/qq-life-sessions.ts').read_bytes()).hexdigest()
            args.out.parent.mkdir(parents=True,exist_ok=True)
            args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            if role.running:
                await role.close()
            else:
                await asyncio.to_thread(bridge.close)
            life.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--live-choices',type=int,default=0,choices=range(4))
    parser.add_argument('--empty-profile',action='store_true')
    args=parser.parse_args();asyncio.run(run(args))


if __name__=='__main__':
    main()
