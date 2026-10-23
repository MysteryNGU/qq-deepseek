"""Opt-in acceptance through the running role's durable inbox; no forged user message.

The running DSH role generates all outgoing text. This helper only enqueues
explicitly labelled internal maintenance events and reads platform receipts.
"""
import argparse
import asyncio
import json
import re
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.manual.helpers import report_path
import websockets
from life.agent_session import AgentSessions
from life.media import onebot_segments
from life.role_config import load_config
from life.role_host import runtime_version,source_signature
from life.runtime import LifeRuntime


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send',action='store_true',required=True)
    parser.add_argument('--resume',nargs='*',default=[],help='Observe existing acceptance events without enqueueing more tests')
    args = parser.parse_args()
    cfg = load_config(ROOT/'config.toml')
    life = LifeRuntime(ROOT/cfg.life_db,cfg.life_agent)
    scope = life.scope('qq:private:'+str(cfg.owner),str(cfg.owner))
    sessions = AgentSessions(life,SimpleNamespace())
    result_path = report_path('live-qq.json')
    results = json.loads(result_path.read_text('utf-8')) if args.resume and result_path.is_file() else []
    health_path = (ROOT/cfg.life_db).parent/'life-control/worker-health.json'
    try:
        async with asyncio.timeout(180):
            while True:
                health = json.loads(health_path.read_text('utf-8')) if health_path.is_file() else {}
                boot = life.repo._one("SELECT id FROM events WHERE agent_id=? AND conversation_id=? "
                    "AND kind='startup' ORDER BY ingest_seq DESC LIMIT 1",scope.private_key)
                delivered = life.repo._one("SELECT 1 FROM deliveries d,json_each(d.sources) src WHERE d.scope_id=? "
                    "AND d.status='confirmed' AND src.value=?",(scope.scope_id,boot['id'])) if boot else None
                if (health.get('runtime',{}).get('version')==runtime_version() and delivered
                        and health.get('cognition_ready') and health.get('channel_connected')
                        and health['runtime']['source_signature']==source_signature(ROOT)
                        and time.time()-health['at']<15):
                    break
                await asyncio.sleep(1)
        print('verified_running_version='+runtime_version()+' startup_confirmed=true',flush=True)
        cases = args.resume or ('short','long')
        for case_or_event in cases:
            await asyncio.sleep(3)
            existing = life.repo.get_event(case_or_event) if args.resume else None
            if args.resume:
                assert existing and existing.kind=='maintenance_acceptance' and existing.scope==scope
            case = existing.payload['test_case'] if existing else case_or_event
            marker = '节奏验收'+('短' if case=='short' else '长')+uuid.uuid4().hex[:6]
            shape = ('请只发两行自然中文，每行约36～48个非空白字符，合计少于300字符，'
                '不要列表、代码和额外说明，开头自然带上标记“'+marker+'”。' if case=='short' else
                '请发一段330～380字符的自然中文，保留两三次回车，开头自然带上标记“'+marker+'”，'
                '不要列表、代码，也不要额外发一条告知。')
            prompt = ('这是维护者按用户已明确授权投递的真实QQ发送验收内部事件，不是主人新发来的消息，'
                '不代表任何等待事项已获答复，不要改变或取消原有长期打算。只向当前主人私聊发送这次验收文字，'
                '不联系第三方，不调用B站写入，不修改程序。正文可友好说明修复验证进展；由你自己自然表达。'+shape+
                '请先理解并处理本事件，再通过一次life_say提交最终正文；不必建任务或先行告知。')
            if existing:
                event = existing
                marker = re.search(r'节奏验收(?:短|长)[0-9a-f]{6}',event.text).group()
            else:
                event,_ = life.ingest(scope,'maintenance_acceptance','inference',prompt,
                    external_id='maintenance:'+uuid.uuid4().hex,payload=dict(test_case=case,operator_authorized=True))
            binding = sessions.binding(scope)
            if not existing:
                sessions.enqueue(binding,event)
            print(('observing ' if existing else 'enqueued ')+case+' '+event.id,flush=True)
            started = time.time()
            async with asyncio.timeout(180):
                while True:
                    rows = life.repo._all("SELECT DISTINCT e.* FROM expressions e,json_each(e.plan,'$.sources') src "
                        'WHERE e.scope_id=? AND src.value=? ORDER BY e.rowid DESC',(scope.scope_id,event.id))
                    selected = []
                    for row in rows:
                        deliveries = life.repo._all('SELECT * FROM deliveries WHERE turn_id=? ORDER BY segment',(row['id'],))
                        if deliveries and all(d['status']=='confirmed' for d in deliveries):
                            selected = deliveries
                            frozen = json.loads(row['rendered'])
                            break
                    if selected:
                        break
                    if time.time()-started>20 and int(time.time()-started)%20==0:
                        item = life.repo._one('SELECT state,handled FROM agent_inputs WHERE session_id=? AND input_id=?',
                                              (binding['session_id'],event.id))
                        print('waiting '+case+' '+json.dumps(dict(item) if item else {}),flush=True)
                    await asyncio.sleep(1)
            outgoing = []
            for delivery in selected:
                body = json.loads(delivery['payload'])['params']['message']
                text = ''.join(s['data'].get('text','') for s in body if s['type']=='text')
                receipt = json.loads(delivery['receipt'])
                outgoing.append(dict(message_id=receipt['message_id'],text=text,at=delivery['updated']))
            assert any(marker in p['text'] for p in outgoing),'marker not in actual reply'
            if case=='short':
                assert len(outgoing)==2,(case,len(outgoing))
                assert all(2<=d<=5 for d in frozen['delays']),frozen['delays']
            else:
                assert len(outgoing)==1 and len(outgoing[0]['text'].strip())>=300,(case,len(outgoing),len(outgoing[0]['text']))
                assert frozen['delays']==[4],frozen['delays']
            platform_checks = []
            async with websockets.connect(cfg.ws_url,proxy=None,open_timeout=10,
                    additional_headers={'Authorization':'Bearer '+cfg.ws_token} if cfg.ws_token else None) as ws:
                for message in outgoing:
                    await asyncio.sleep(3)
                    echo = uuid.uuid4().hex
                    await ws.send(json.dumps(dict(action='get_msg',params=dict(message_id=message['message_id']),echo=echo)))
                    async with asyncio.timeout(15):
                        async for raw in ws:
                            response = json.loads(raw)
                            if response.get('echo')!=echo:
                                continue
                            assert response.get('retcode')==0,response.get('retcode')
                            text = ''.join(s['data'].get('text','') for s in onebot_segments(response['data']['message']) if s['type']=='text')
                            assert text.strip()==message['text'].strip(),'platform text differs from delivery'
                            platform_checks.append(dict(message_id=message['message_id'],characters=len(text),read_back_verified=True))
                            break
            results = [r for r in results if r['case']!=case]
            results.append(dict(case=case,event_id=event.id,model_generated=True,internal_source_kind='inference',
                delays=frozen['delays'],platform_messages=platform_checks,
                send_gaps_seconds=[round(outgoing[i]['at']-outgoing[i-1]['at'],2) for i in range(1,len(outgoing))]))
            print(json.dumps(results[-1],ensure_ascii=False),flush=True)
    finally:
        result_path.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        life.close()


if __name__=='__main__':
    asyncio.run(main())
