"""Real Bot profile/model: local spill, bounded expansion and provider usage. No QQ."""
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.manual.helpers import report_path
from tests.manual.run_role_smoke import smoke_directory
from life.role_agent import RoleAgent
from life.role_config import Config,load_config
from life.runtime import LifeRuntime,LifeConfig
from life.services.dsh_bridge import DshBridge


async def main():
    actual = load_config(ROOT/'config.toml')
    with smoke_directory() as folder:
        root = Path(folder)
        (root/'skills').mkdir()
        (root/'persona.md').write_text('你是准确简洁的合成验收角色。按指定的真实协议检查，不伪造结果。',encoding='utf-8')
        marker = uuid.uuid4().hex
        (root/'logs.txt').write_text('\n'.join(f'row {i:03d}: '+('MARKER='+marker if i==300 else '普通日志记录 '*18) for i in range(600)),encoding='utf-8')
        cfg = Config(owner=1,api_key=actual.api_key,base_url=actual.base_url,model=actual.model,
            dsh_root=str(ROOT.parent/'dsh-bridge-work'),dsh_home=str(root/'home'),bili_plugin='')
        life = LifeRuntime(root/'life.db',config=LifeConfig(relationship_owner='1'))
        bridge = DshBridge(cfg,root)
        finished = asyncio.Event();outputs=[]
        async def send(scope,key,proposal,sources):
            outputs.append(proposal)
            if proposal.get('purpose')!='notice':finished.set()
            return dict(sent_confirmed=True,logical_message_id=key)
        send.accepts_plan=True
        role = RoleAgent(life,bridge,cfg,root,send)
        try:
            await role.start()
            scope = life.scope('qq:private:1','1')
            event,_ = life.ingest_message(scope,'这是资源接口验收。先处理输入，然后用当前原生 read 工具一次读取 logs.txt 的前 600 行。'
                '大结果会有真实 result_ref；必须用 life_read(kind=observation,result_ref=该引用,mode=search,query=MARKER) 查中间标记，'
                '不要通过另跑 grep 绕过本轮分页接口验收。再用 life_read(kind=usage) 查询本轮已报告用量，最后自然告诉我标记。不要建立长期任务，不要对外发其他消息。')
            await role.ingest(event)
            await asyncio.wait_for(finished.wait(),240)
            await role.replay(role.sessions.binding(scope)['session_id'])
            envelopes=[]
            for row in life.repo._all("SELECT payload FROM agent_events WHERE json_extract(payload,'$.type')='tool/result'"):
                data=json.loads(row['payload'])['data']
                for block in data.get('message',{}).get('content',[]):
                    if block.get('type')=='text':
                        try:
                            value=json.loads(block['text'])
                            if isinstance(value,dict) and value.get('result_ref'):envelopes.append((value,len(block['text'].encode())))
                        except ValueError:pass
            receipts=[json.loads(r['receipt']) for r in life.repo._all('SELECT receipt FROM life_calls')]
            usage=role.usage.read(scope,{})
            checks=dict(real_qq_messages=0,marker_correct=marker in outputs[-1].get('text',''),
                actual_spill_preview=bool(envelopes),preview_bytes=[b for _,b in envelopes],
                bounded_search_recovered_marker=any(r.get('mode')=='search' and marker in json.dumps(r) for r in receipts),
                provider_usage_recorded=usage['calls']>0,usage_read_called=any(r.get('known_totals') for r in receipts),
                calls=usage['calls'],known_totals=usage['known_totals'],coverage=usage['coverage'])
            print(json.dumps(checks,ensure_ascii=False),flush=True)
            assert checks['marker_correct'] and checks['actual_spill_preview'] and checks['bounded_search_recovered_marker'] and checks['usage_read_called']
            assert all(b<=4000 for _,b in envelopes)
            report_path('resources.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        finally:
            await role.close();life.close()


if __name__=='__main__':asyncio.run(main())
