"""Opt-in narrow maintenance check through the live Bot inbox and QQ receipts."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import websockets
from life.agent_session import AgentSessions
from life.media import onebot_segments
from life.role_config import load_config
from life.role_host import source_signature
from life.runtime import LifeRuntime


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--send',action='store_true',required=True)
    parser.parse_args()
    cfg=load_config(ROOT/'config.toml')
    health_path=ROOT/'data/life-control/worker-health.json'
    health=json.loads(health_path.read_text('utf-8'))
    assert time.time()-health['at']<15 and health['channel_connected'] and health['cognition_ready']
    assert health['runtime']['source_signature']==source_signature(ROOT)
    adapter=ROOT/'vendor/dsh-bridge-work/packages/sdk/server/src'
    expected=hashlib.sha256((adapter/'qq-life-sessions.ts').read_bytes()+
        hashlib.sha256((adapter/'qq-life-context.ts').read_bytes()).hexdigest().encode()+
        hashlib.sha256((adapter/'qq-life-consult.ts').read_bytes()).hexdigest().encode()).hexdigest()
    assert health['continuity']['dsh_adapter_version']==expected
    marker='上下文验证'+uuid.uuid4().hex[:8]
    marker_file=ROOT/'data/test-results/context-batches-marker.txt'
    marker_file.parent.mkdir(parents=True,exist_ok=True)
    marker_file.write_text(marker,encoding='utf-8')
    life=LifeRuntime(ROOT/cfg.life_db,cfg.life_agent)
    report=dict(marker=marker,started_at=time.time(),source_signature=source_signature(ROOT),adapter=expected)
    result_path=ROOT/'data/test-results/context-batches-live-qq.json'
    try:
        scope=life.scope('qq:private:'+str(cfg.owner),str(cfg.owner))
        sessions=AgentSessions(life,SimpleNamespace())
        binding=sessions.binding(scope)
        prompt=('这是维护者按当前用户明确授权投递的内部维护验证，不是主人新发来的消息，'
            '也不代表旧等待已获答复。仅测试当前上下文优化，不改变长期打算、记忆、专项场景和程序。'
            '理解本事件后，把可确认编号合并到一次简短life_say notice的handled_input_ids中，避免额外resolve。'
            f'然后只用pwsh读取 {marker_file.as_posix()}，'
            '再用life_read(proposal:{kind:"simulation",view:"input_coverage",limit:2})读取覆盖明细第一页。'
            '最后向当前主人私聊发一条简短结果，带文件中的原样标记、执行是否成功和覆盖页是否有更多，'
            'life_say显式end_turn:true。无需加载其他工具，无需额外测试，不联系第三方。')
        event,_=life.ingest(scope,'maintenance_acceptance','inference',prompt,
            external_id='maintenance:'+uuid.uuid4().hex,payload=dict(operator_authorized=True,test_case='context_batches'))
        sessions.enqueue(binding,event)
        report.update(event_id=event.id,session_id=binding['session_id'])
        result_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('enqueued '+event.id,flush=True)
        async with asyncio.timeout(240):
            while True:
                rows=life.repo._all("SELECT DISTINCT e.* FROM expressions e,json_each(e.plan,'$.sources') src "
                    'WHERE e.scope_id=? AND src.value=? ORDER BY e.rowid DESC',(scope.scope_id,event.id))
                deliveries=[]
                for row in rows:
                    parts=life.repo._all('SELECT * FROM deliveries WHERE turn_id=? ORDER BY segment',(row['id'],))
                    if not parts or any(part['status']!='confirmed' for part in parts):continue
                    candidate=[]
                    for part in parts:
                        body=json.loads(part['payload'])['params']['message']
                        text=''.join(s['data'].get('text','') for s in body if s['type']=='text')
                        candidate.append((part,text))
                    if any(marker in text for _,text in candidate):
                        deliveries=candidate
                        break
                if deliveries:break
                await asyncio.sleep(1)
        checked=[]
        async with websockets.connect(cfg.ws_url,proxy=None,open_timeout=10,
                additional_headers={'Authorization':'Bearer '+cfg.ws_token} if cfg.ws_token else None) as ws:
            for part,text in deliveries:
                message_id=json.loads(part['receipt'])['message_id'];echo=uuid.uuid4().hex
                await ws.send(json.dumps(dict(action='get_msg',params=dict(message_id=message_id),echo=echo)))
                async with asyncio.timeout(15):
                    async for raw in ws:
                        response=json.loads(raw)
                        if response.get('echo')!=echo:continue
                        assert response.get('retcode')==0
                        actual=''.join(s['data'].get('text','') for s in onebot_segments(response['data']['message']) if s['type']=='text')
                        assert actual.strip()==text.strip()
                        checked.append(dict(message_id=message_id,text=text,read_back_verified=True))
                        break
        report.update(completed_at=time.time(),platform_messages=checked)
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        result_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        life.close()


if __name__=='__main__':asyncio.run(main())
