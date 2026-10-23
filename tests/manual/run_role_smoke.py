"""Real DSH composition/model smoke; synthetic persona and test delivery only."""
import asyncio
import json
import sys
import os
import uuid
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from bot import load_config
from life.runtime import LifeRuntime
from life.role_agent import RoleAgent
from life.services.dsh_bridge import DshBridge


@contextmanager
def smoke_directory():
    # Python's restricted mkdtemp ACL on this Windows host prevents Node writes.
    # An ordinary workspace directory inherits the same ACL as the application.
    parent = Path('D:/projects/tools/tmp')
    path = parent / ('role-smoke-' + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield str(path)
    finally:
        if path.resolve().parent != parent.resolve():
            raise RuntimeError('unexpected smoke cleanup target')
        def remove_readonly(operation,target,error):
            item = Path(target)
            if (not isinstance(error,PermissionError) or not item.is_file() or item.is_symlink()
                    or not item.resolve().is_relative_to(path.resolve()) or item.stat().st_mode & stat.S_IWRITE):
                raise error
            item.chmod(item.stat().st_mode | stat.S_IWRITE)
            operation(target)
        shutil.rmtree(path,onexc=remove_readonly)


async def main():
    cfg = load_config(ROOT / 'config.toml')
    with smoke_directory() as folder:
        root = Path(folder)
        (root/'skills').mkdir()
        (root/'persona.md').write_text('# 测试角色\n这是合成测试人格。准确、简洁，用真实结果回答。', encoding='utf-8')
        nonce = uuid.uuid4().hex
        (root/'smoke.txt').write_text(nonce, encoding='utf-8')
        cfg.persona = 'persona.md'
        cfg.dsh_root = str((ROOT/'../dsh-bridge-work').resolve())
        cfg.dsh_home = str(root/'dsh-home')
        cfg.dsh_node = 'D:/projects/tools/node-v22.19.0-win-x64/node.exe'
        cfg.dsh_timeout = 120
        bili_mode = os.environ.get('QQ_SMOKE_BILI')=='1'
        plugins_mode = os.environ.get('QQ_SMOKE_PLUGINS')=='1'
        if bili_mode:
            cfg.bili_plugin = str(Path.home()/'.dsh/plugins/dsh-bili-subagent')
        life = LifeRuntime(root/'life.db')
        bridge = DshBridge(cfg, root)
        delivered = asyncio.Event()
        outputs = []
        async def send(scope, key, text, sources):
            proposal = text if isinstance(text,dict) else dict(text=text)
            outputs.append(dict(key=key,text=proposal.get('text',''),purpose=proposal.get('purpose')))
            if proposal.get('purpose')!='notice':
                delivered.set()
            return True
        send.accepts_plan = True
        role = RoleAgent(life,bridge,cfg,root,send)
        try:
            await role.start()
            scope = life.scope('console:smoke',str(cfg.owner))
            prompt = ('这是工具链路测试。先用life_intent的action=resolve和handled_input_ids处理输入，'
                '然后自行选择真实文件读取工具，读取当前工作目录的smoke.txt。'
                '最后调用life_say告诉我文件里的完整标记，正文只需要标记。不要通知或建立任务。')
            if bili_mode:
                prompt = ('这是 B站工具接入实测。先处理输入，然后使用当前实际工具查一下你现在是否已经登录 B站。'
                    '只查询身份，不发评论、私信，不启动登录；最后通过life_say自然告诉我实际结果。不要只查看源代码推断。')
            if plugins_mode:
                prompt = ('请真实查询当前 SDK profile 的插件目录和可管理的插件，选用实际插件管理工具的只读能力。'
                    '不用安装、更新或删除。这是能力发现测试，请处理输入后直接查工具，不只读源码猜测，最后自然说清查到的情况。')
            event,_ = life.ingest(scope,'message','user_report',prompt)
            receipt = await role.ingest(event)
            await asyncio.wait_for(delivered.wait(),120)
            await role.replay(role.sessions.binding(scope)['session_id'])
            native = life.repo._all('SELECT tool,status FROM role_operations')
            result = dict(input_accepted=bool(receipt.get('message_id')),outputs=len(outputs),
                final_outputs=sum(o['purpose']!='notice' for o in outputs),
                marker_correct=nonce in outputs[-1]['text'],native_tools=[dict(r) for r in native])
            if bili_mode:
                result['bili_native_called'] = any(str(row['tool']).startswith('mcp__bili__') for row in native)
            if plugins_mode:
                result['plugin_manager_called'] = any('plugin' in str(row['tool']) for row in native)
            print(json.dumps(result,ensure_ascii=False))
            if not (result.get('plugin_manager_called') if plugins_mode else result.get('bili_native_called') if bili_mode else result['marker_correct']) or not native or result['final_outputs']!=1:
                raise RuntimeError('real Agent/tool/delivery smoke failed')
        except Exception:
            tail = bridge._log_tail(4000)
            if cfg.api_key:
                tail = tail.replace(cfg.api_key, '<redacted>')
            print(tail, flush=True)
            rows = life.repo._all('SELECT payload FROM agent_events ORDER BY rowid DESC LIMIT 8')
            diagnostics = json.dumps([dict(seq=e['seq'],type=e['type'],tool=e.get('data',{}).get('name'),
                reason=e.get('data',{}).get('reason') if e['type']=='turn/end' else None)
                for row in reversed(rows) for e in [json.loads(row['payload'])]],ensure_ascii=False)
            if cfg.api_key:
                diagnostics = diagnostics.replace(cfg.api_key,'<redacted>')
            print(diagnostics[-9000:],flush=True)
            raise
        finally:
            await role.close()
            life.close()


if __name__ == '__main__':
    asyncio.run(main())
