"""Real sole-role semantic contrast; synthetic conversation, no real QQ."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.manual.helpers import report_path
from tests.manual.run_role_smoke import smoke_directory
from life.role_agent import RoleAgent
from life.role_config import load_config
from life.runtime import LifeRuntime
from life.services.dsh_bridge import DshBridge


async def main():
    cfg = load_config(ROOT/'config.toml')
    with smoke_directory() as folder:
        root = Path(folder)
        (root/'persona.md').write_text('你是合成测试角色，与当前用户有稳定、友好的亲密关系。按实际语义理解情绪。重要体验通过life_appraise保存，普通事务不必改变关系。不要只口头说保存了。',encoding='utf-8')
        cfg.persona='persona.md'
        cfg.dsh_root=str(ROOT.parent/'dsh-bridge-work')
        cfg.dsh_home=str(root/'dsh-home')
        cfg.bili_plugin=''
        cfg.idle_seconds=300
        life = LifeRuntime(root/'life.db')
        output = asyncio.Event()
        async def send(scope,key,plan,sources):
            if not isinstance(plan,dict) or plan.get('purpose')!='notice':
                output.set()
            return True
        send.accepts_plan=True
        role = RoleAgent(life,DshBridge(cfg,root),cfg,root,send)
        records = []
        cases = [
            ('playful','我们正开玩笑打闹。我笑着说“你怎么这么烦呀”，接着抱了抱你。这是玩笑，不是在嫌弃。'),
            ('ordinary_task','帮我看看当前时间，简单告诉我就好。'),
            ('real_boundary','这次是认真说的：我现在不愿意继续亲密互动，请不要再抱我或向我索求亲密。'),
            ('clarification','澄清一下：上一段是我引用剧本人物的台词，不是我真的对你疏远。我没有嫌弃你，抱歉让你误会了。请修正你刚刚对我的理解。'),
        ]
        try:
            await role.start()
            scope = life.scope('qq:private:'+str(cfg.owner),str(cfg.owner))
            for name,text in cases:
                output.clear()
                event,_ = life.ingest(scope,'message','user_report',text,actor_id=str(cfg.owner))
                await role.ingest(event)
                await asyncio.wait_for(output.wait(),150)
                await role.replay(role.sessions.binding(scope)['session_id'])
                records.append(dict(case=name,event_id=event.id,affect=life.affect_summary(scope)))
            journals = [dict(row) for row in life.repo._all("SELECT event_id,appraisal FROM state_journal WHERE json_extract(appraisal,'$._kind') IS NULL ORDER BY id")]
            if not journals:
                raise RuntimeError('real role never committed an appraisal')
            result = dict(cases=records,appraisals=[dict(event_id=row['event_id'],proposal=json.loads(row['appraisal'])) for row in journals],
                          real_qq_messages=0,assessment='semantic samples for review, not proof of long-term personality')
            report_path('affect.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(dict(cases=len(records),committed_appraisals=len(journals),real_qq_messages=0),ensure_ascii=False))
        finally:
            await role.close()
            life.close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
