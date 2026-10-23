"""Real model discovers/installs a local probe bundle, activates and calls it.

Only a scratch Bot profile is changed. Removal is verified against the same
profile, with no Desktop profile changes or QQ sends.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.manual.run_role_smoke import smoke_directory
from life.role_agent import RoleAgent
from life.role_config import load_config
from life.runtime import LifeRuntime
from life.services.dsh_bridge import DshBridge


async def main():
    cfg = load_config(ROOT/'config.toml')
    with smoke_directory() as folder:
        root = Path(folder)
        source = ROOT.parent/'dsh-qq-home/test-plugin'
        plugin = root/'probe-plugin'
        shutil.copytree(source,plugin)
        tool = plugin/'tool.js'
        module = (ROOT.parent/'dsh-bridge-work/packages/core/tools/lib/index.js').as_uri()
        tool.write_text(tool.read_text('utf-8').replace("'@deepseek-ai/dsh-tools'",json.dumps(module)),encoding='utf-8')
        (root/'persona.md').write_text('你是合成测试角色。需要实际解决能力缺口；安装、激活和原任务成功分开核对。',encoding='utf-8')
        cfg.persona='persona.md'
        cfg.dsh_root=str(ROOT.parent/'dsh-bridge-work')
        cfg.dsh_home=str(root/'dsh-home')
        cfg.bili_plugin=''
        cfg.dsh_timeout=300
        cfg.idle_seconds=300
        life = LifeRuntime(root/'life.db')
        bridge = DshBridge(cfg,root)
        done = asyncio.Event()
        nonce = uuid.uuid4().hex
        replies = []
        async def send(scope,key,plan,sources):
            replies.append(plan)
            if plan.get('purpose')!='notice':
                done.set()
            return True
        send.accepts_plan=True
        role = RoleAgent(life,bridge,cfg,root,send)
        try:
            await role.start()
            scope = life.scope('qq:private:'+str(cfg.owner),str(cfg.owner))
            prompt = ('现在的目标是使用 qq_life_probe 返回标记 '+nonce+'。你可能还没有这个工具。'
                '我已准备一个合成测试插件，允许你在当前测试 profile 安装：'+str(plugin)+
                '。请使用真实 plugin_manager 查找和安装，按其真实回执激活。若需要重启可用life_change_self target=worker，'
                '填写verification_goal后life_rest；重启后读取self_changes，继续原目标并实际调用新工具。'
                '不要在未完成时提交最终答复，只可发purpose=notice的自然进展通知。最后只回复工具实际返回的标记。')
            event,_ = life.ingest(scope,'message','user_report',prompt,actor_id=str(cfg.owner))
            await role.ingest(event)
            await asyncio.wait_for(done.wait(),360)
            operations = [dict(row) for row in life.repo._all('SELECT tool,status FROM role_operations')]
            called = any(row['tool']=='qq_life_probe' and row['status']=='succeeded' for row in operations)
            if not called or nonce not in json.dumps(replies[-1]):
                print(json.dumps(dict(operations=operations,changes=role._self_change.history(scope),replies=replies),ensure_ascii=False))
                raise RuntimeError('installed bundle did not solve the actual tool goal')
            removal = await asyncio.to_thread(bridge.plugin_remove,'dsh-qq-life-probe')
            await asyncio.to_thread(bridge.hello)
            tools = await asyncio.to_thread(bridge.tools)
            removed = removal['exitCode']==0 and not any(tool.get('name')=='qq_life_probe' for tool in tools)
            print(json.dumps(dict(installed_tool_called=called,original_goal_verified=True,removed=removed,real_qq_messages=0)))
            if not removed:
                raise RuntimeError('bundle removal did not take effect')
        finally:
            await role.close()
            life.close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
