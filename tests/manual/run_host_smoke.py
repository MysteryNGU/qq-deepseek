"""Actual role/model/OneBot lifecycle with a local fake platform, never QQ."""
import asyncio
import json
import os
import base64
import io
from pathlib import Path
import sys
import time
import uuid

import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
from bot import load_config
from life.role_config import Config
from life.role_host import RoleHost
from tests.manual.run_role_smoke import smoke_directory


async def until(predicate,timeout=180):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


async def main():
    previous = load_config(ROOT/'config.toml')
    with smoke_directory() as folder:
        root = Path(folder)
        (root/'persona.md').write_text('你是准确、简洁的合成测试角色，自行使用真实工具。',encoding='utf-8')
        nonce = uuid.uuid4().hex
        (root/'smoke.txt').write_text(nonce,encoding='utf-8')
        frames = []
        host = None
        health_env = os.environ.get('QQ_WORKER_HEALTH')
        if os.environ.get('QQ_SMOKE_CHANNEL')=='1':
            os.environ['QQ_WORKER_HEALTH'] = str(root/'worker-health.json')
        connected = asyncio.Event()
        socket = None
        async def platform(ws):
            nonlocal socket
            socket = ws
            connected.set()
            async for line in ws:
                request = json.loads(line)
                frames.append(request)
                await ws.send(json.dumps(dict(echo=request['echo'],status='ok',retcode=0,
                                             data=dict(message_id=len(frames)))))
        async with websockets.serve(platform,'127.0.0.1',0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = Config(owner=1,api_key=previous.api_key,base_url=previous.base_url,model=previous.model,
                ws_url=f'ws://127.0.0.1:{port}',life_db='life.db',bili_plugin='',
                dsh_root=str(ROOT.parent/'dsh-bridge-work'),dsh_home=str(root/'dsh-home'),idle_seconds=120)
            host = RoleHost(cfg,root)
            process = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(connected.wait(),30)
                await until(lambda:host.boot_ready)
                await socket.send(json.dumps(dict(post_type='message',message_type='private',message_id=100,
                    user_id=1,self_id=2,time=int(time.time())+1,message=[dict(type='text',data=dict(text=
                    '请真实读取当前工作目录的smoke.txt，并把文件的完整标记告诉我。最后的答复正文只需要标记。'))]),ensure_ascii=False))
                await until(lambda:any(nonce in json.dumps(f['params'].get('message'),ensure_ascii=False) for f in frames))
                await until(lambda:host.life.repo._one("SELECT 1 FROM deliveries WHERE status='confirmed' AND payload LIKE ?",('%'+nonce+'%',)) is not None)
                recovered = False
                channel_recovered = False
                if os.environ.get('QQ_SMOKE_CHANNEL')=='1':
                    old_socket,old_worker = socket,host.bridge._proc.pid
                    await socket.close()
                    server.close()
                    await server.wait_closed()
                    await asyncio.sleep(25)  # Longer than the supervisor's 20s heartbeat expiry.
                    health = json.loads((root/'worker-health.json').read_text('utf-8'))
                    assert time.time()-health['at']<5 and not health['channel_connected'],health
                    assert host.role.running and host.bridge._proc.pid==old_worker
                    async with websockets.serve(platform,'127.0.0.1',port):
                        await until(lambda:socket is not old_socket and host.ws is not None)
                        nonce3 = uuid.uuid4().hex
                        (root/'smoke.txt').write_text(nonce3,encoding='utf-8')
                        await socket.send(json.dumps(dict(post_type='message',message_type='private',message_id=103,
                            user_id=1,self_id=2,time=int(time.time())+1,message=[dict(type='text',data=dict(text=
                            '通道恢复了。实际重读 smoke.txt，只回复当前文件的完整标记。'))]),ensure_ascii=False))
                        await until(lambda:host.life.repo._one("SELECT 1 FROM deliveries WHERE status='confirmed' AND payload LIKE ?",('%'+nonce3+'%',)) is not None)
                        assert host.bridge._proc.pid==old_worker
                        channel_recovered = True
                if os.environ.get('QQ_SMOKE_RESTART')=='1':
                    binding = host.role.sessions.binding(host.life.scope('qq:private:1','1'))
                    await until(lambda:host.life.repo._one("SELECT 1 FROM agent_events WHERE session_id=? "
                        "AND json_extract(payload,'$.type')='turn/end'",(binding['session_id'],)) is not None)
                    await asyncio.to_thread(host.bridge.restart_worker)
                    await host.role.recover()
                    nonce2 = uuid.uuid4().hex
                    (root/'smoke.txt').write_text(nonce2,encoding='utf-8')
                    await socket.send(json.dumps(dict(post_type='message',message_type='private',message_id=101,
                        user_id=1,self_id=2,time=int(time.time())+1,message=[dict(type='text',data=dict(text=
                        '重启后的接续验收：重新实际读取 smoke.txt，正文只回复现在文件里的完整标记。'))]),ensure_ascii=False))
                    await until(lambda:host.life.repo._one("SELECT 1 FROM deliveries WHERE status='confirmed' AND payload LIKE ?",('%'+nonce2+'%',)) is not None)
                    recovered = True
                image_verified = False
                if os.environ.get('QQ_SMOKE_IMAGE')=='1':
                    from PIL import Image,ImageDraw
                    image = Image.new('RGB',(1156,17225),'white')
                    draw = ImageDraw.Draw(image)
                    draw.rectangle((0,0,1155,1500),fill='red')
                    draw.rectangle((0,15700,1155,17224),fill='blue')
                    encoded = io.BytesIO()
                    image.save(encoded,format='JPEG',quality=90)
                    before_count = len(frames)
                    await socket.send(json.dumps(dict(post_type='message',message_type='private',message_id=102,
                        user_id=1,self_id=2,time=int(time.time())+1,message=[
                            dict(type='text',data=dict(text='请实际看这张长图，等图片感知成功后，只说最上方和最下方的大色块各是什么颜色。不要只确认收到，也不用读取文件猜。')),
                            dict(type='image',data=dict(file='base64://'+base64.b64encode(encoded.getvalue()).decode()))]),ensure_ascii=False))
                    await until(lambda:any('红' in json.dumps(f['params'].get('message'),ensure_ascii=False)
                        and '蓝' in json.dumps(f['params'].get('message'),ensure_ascii=False) for f in frames[before_count:]))
                    image_verified = True
                native = [dict(r) for r in host.life.repo._all('SELECT tool,status FROM role_operations')]
                print(json.dumps(dict(greeting_confirmed=host.boot_ready,native_tools=native,
                    reply_confirmed=True,worker_restart_recovered=recovered,long_image_verified=image_verified,
                    channel_disconnect_recovered=channel_recovered,
                    platform_messages=len(frames),real_qq_messages=0),ensure_ascii=False))
            finally:
                host.stop.set()
                await asyncio.wait_for(process,30)
                if health_env is None:
                    os.environ.pop('QQ_WORKER_HEALTH',None)
                else:
                    os.environ['QQ_WORKER_HEALTH'] = health_env


if __name__=='__main__':
    asyncio.run(main())
