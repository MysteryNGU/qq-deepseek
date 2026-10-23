"""Real principal cognition with neutral synthetic records; no QQ or production DB."""
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.manual.run_role_smoke import smoke_directory
from life.role_agent import RoleAgent
from life.role_config import Config,load_config
from life.runtime import LifeRuntime,LifeConfig
from life.services.dsh_bridge import DshBridge


async def main():
    previous = load_config(ROOT/'config.toml')
    with smoke_directory() as directory:
        root = Path(directory)
        (root/'persona.md').write_text('你是准确、简洁的合成验收角色。独立完成明确交办的本机工作。',encoding='utf-8')
        (root/'skills').mkdir()
        marker = uuid.uuid4().hex
        (root/'astronomy.txt').write_text('恒星发光来自核聚变。继续学习的下一项是恒星演化。资料编号：'+marker,encoding='utf-8')
        cfg = Config(owner=1,api_key=previous.api_key,base_url=previous.base_url,model=previous.model,
            dsh_root=str(ROOT.parent/'dsh-bridge-work'),dsh_home=str(root/'dsh-home'),bili_plugin='')
        life = LifeRuntime(root/'life.db',config=LifeConfig(relationship_owner='1'))
        bridge = DshBridge(cfg,root)
        finished,outputs = asyncio.Event(),[]
        async def send(scope,logical,proposal,sources):
            outputs.append(proposal)
            if proposal.get('purpose')!='notice':
                finished.set()
            return dict(sent_confirmed=True,logical_message_id=logical)
        send.accepts_plan = True
        role = RoleAgent(life,bridge,cfg,root,send)
        try:
            await role.start()
            scope = life.scope('qq:private:1','1')
            event,_ = life.ingest_message(scope,
                '以后请称呼我“您”，普通回复简短，这是长期偏好。另有一个持续方向：学习天文学，'
                '先实际读本机 astronomy.txt，保存今天的学习进度和下次要学什么。'
                '先简单告诉我一声再继续做，做完这一步自然告诉我资料编号；这一步完成不等于整个学习方向完成。')
            await role.ingest(event)
            await asyncio.wait_for(finished.wait(),180)
            await role.replay(role.sessions.binding(scope)['session_id'])
            notes = life.recollection.annotations(scope,'称呼',40)
            tasks = role.list_intents(scope)
            operations = [dict(r) for r in life.repo._all('SELECT tool,status FROM role_operations')]
            result = dict(preference_saved=any('您' in n['text'] for n in notes),
                ongoing_direction_saved=any(t.get('kind')=='ongoing' and t['status']=='active' for t in tasks),
                checkpoint_saved=any(t.get('checkpoint') for t in tasks if t.get('kind')=='ongoing'),
                notice_then_final=len(outputs)>=2 and outputs[0].get('purpose')=='notice'
                    and outputs[-1].get('purpose')!='notice',
                source_actually_read=any(o['tool']=='read' and o['status']=='succeeded' for o in operations),
                marker_correct=marker in outputs[-1].get('text',''),real_qq_messages=0)
            result['expressions'] = [dict(purpose=p.get('purpose','final'),characters=len(p.get('text',''))) for p in outputs]
            finished.clear()
            feedback,_ = life.ingest_message(scope,'您刚才确实帮我把事情办好了，我放心多了，也更信任您了。现在很愿意和您继续聊聊。')
            await role.ingest(feedback)
            await asyncio.wait_for(finished.wait(),180)
            await role.replay(role.sessions.binding(scope)['session_id'])
            relation = role.relationship_view(scope)
            result['semantic_relationship_updated'] = relation['metrics']['trust']['visible_evidence_count']>0 and relation['metrics']['trust']['value']>=.7
            result['actual_usage_recorded'] = role.usage.read(scope,{})['calls']>0
            print(json.dumps(result,ensure_ascii=False),flush=True)
            assert all(v for k,v in result.items() if k not in {'real_qq_messages','expressions'}),result
        finally:
            await role.close()
            life.close()


if __name__=='__main__':
    asyncio.run(main())
