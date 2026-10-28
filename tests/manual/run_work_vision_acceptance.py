"""One authorized maintenance case: batch acknowledgement, reads and real vision."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
from types import SimpleNamespace
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from PIL import Image,ImageDraw,ImageFont
import websockets
from life.agent_session import AgentSessions
from life.media import onebot_segments
from life.role_config import load_config
from life.role_host import source_signature
from life.runtime import LifeRuntime


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--send',action='store_true',required=True);parser.parse_args()
    cfg=load_config(ROOT/'config.toml');base=ROOT/'data/test-results'
    base.mkdir(parents=True,exist_ok=True)
    health=json.loads((ROOT/'data/life-control/worker-health.json').read_text('utf-8'))
    assert time.time()-health['at']<15 and health['channel_connected'] and health['cognition_ready']
    assert health['runtime']['source_signature']==source_signature(ROOT)
    adapter=ROOT/'vendor/dsh-bridge-work/packages/sdk/server/src'
    expected=hashlib.sha256((adapter/'qq-life-sessions.ts').read_bytes()+
        hashlib.sha256((adapter/'qq-life-context.ts').read_bytes()).hexdigest().encode()+
        hashlib.sha256((adapter/'qq-life-consult.ts').read_bytes()).hexdigest().encode()).hexdigest()
    assert health['continuity']['dsh_adapter_version']==expected
    marker='批次视觉验证'+uuid.uuid4().hex[:6];digits=str(secrets.randbelow(9000)+1000)
    first=base/'work-vision-a.txt';second=base/'work-vision-b.txt';picture=base/'work-vision-probe.png'
    first.write_text(marker+' A',encoding='utf-8');second.write_text(marker+' B',encoding='utf-8')
    image=Image.new('RGB',(600,360),'white');draw=ImageDraw.Draw(image)
    draw.ellipse((35,30,175,170),fill='red');draw.rectangle((230,30,370,170),fill='blue')
    draw.polygon([(495,25),(420,170),(570,170)],fill='green')
    draw.text((170,220),digits,fill='black',font=ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf',90))
    image.save(picture)
    life=LifeRuntime(ROOT/cfg.life_db,cfg.life_agent)
    report=dict(marker=marker,expected_digits=digits,started_at=time.time(),source_signature=source_signature(ROOT),adapter=expected)
    path=base/'work-vision-live-qq.json'
    try:
        scope=life.scope('qq:private:'+str(cfg.owner),str(cfg.owner));sessions=AgentSessions(life,SimpleNamespace())
        original=life.repo._one("SELECT a.id FROM media_assets a JOIN events e ON e.id=a.event_id "
            "WHERE a.scope_id=? AND a.kind='image' AND e.source_kind='user_report' AND e.redacted_at IS NULL "
            'ORDER BY e.ingest_seq DESC LIMIT 1',(scope.scope_id,))
        prompt=('这是维护者按用户明确授权投递的内部维护验证，不是主人新消息，也不解除旧等待。'
            '不要改变长期打算、记忆或程序。先自然发一条工作前notice，理解输入后用当前批次引用一并确认。'
            f'独立检查这两个文本文件内容：{first.as_posix()} 和 {second.as_posix()}。按当前方法组织检查。'
            f'然后用life_read(kind:media,action:import,path:{picture.as_posix()},media_kind:image)实际看测试图。'
            +(f'另外用life_read(kind:media,asset_id:{original["id"]})重读主人刚才那张QQ原图。' if original else '')+
            '收到视觉材料后，向当前主人私聊简短回报：带文本文件中的标记，说明测试图从左到右的颜色、形状和下方四位数字，'
            '并描述重读QQ原图的主体。看不到就准确说明，不根据文件名或元数据猜。最后life_say显式end_turn:true。'
            '只读指定材料，不查看维护验证记录，不做其他测试，不联系第三方。')
        event,_=life.ingest(scope,'maintenance_acceptance','inference',prompt,
            external_id='maintenance:'+uuid.uuid4().hex,payload=dict(operator_authorized=True,test_case='work_vision'))
        binding=sessions.binding(scope);sessions.enqueue(binding,event)
        report.update(event_id=event.id,session_id=binding['session_id'])
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('enqueued '+event.id,flush=True)
        async with asyncio.timeout(240):
            while True:
                rows=life.repo._all("SELECT x.id FROM expressions x JOIN events e ON e.id=json_extract(x.plan,'$.event_id') "
                    'WHERE x.scope_id=? AND e.received_at>=? ORDER BY e.rowid DESC',(scope.scope_id,report['started_at']))
                messages=[]
                for row in rows:
                    parts=life.repo._all('SELECT * FROM deliveries WHERE turn_id=? ORDER BY segment',(row['id'],))
                    if not parts or any(p['status']!='confirmed' for p in parts):continue
                    candidate=[]
                    for part in parts:
                        body=json.loads(part['payload'])['params']['message']
                        text=''.join(s['data'].get('text','') for s in body if s['type']=='text')
                        candidate.append(dict(message_id=json.loads(part['receipt'])['message_id'],text=text))
                    if any(marker[-6:] in m['text'] for m in candidate) and digits in ''.join(m['text'] for m in candidate):
                        messages=candidate;break
                if messages:break
                await asyncio.sleep(1)
        async with websockets.connect(cfg.ws_url,proxy=None,additional_headers={'Authorization':'Bearer '+cfg.ws_token} if cfg.ws_token else None) as ws:
            for message in messages:
                echo=uuid.uuid4().hex
                await ws.send(json.dumps(dict(action='get_msg',params=dict(message_id=message['message_id']),echo=echo)))
                async with asyncio.timeout(15):
                    async for raw in ws:
                        response=json.loads(raw)
                        if response.get('echo')!=echo:continue
                        assert response.get('retcode')==0
                        text=''.join(s['data'].get('text','') for s in onebot_segments(response['data']['message']) if s['type']=='text')
                        assert text.strip()==message['text'].strip();message['read_back_verified']=True;break
        report.update(completed_at=time.time(),platform_messages=messages,vision_digits_verified=True)
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');life.close()


if __name__=='__main__':asyncio.run(main())
