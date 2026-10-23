"""Actual native publish and new worker proof, with synthetic data/fake QQ.

The only production source change is a connected runtime version marker.
This proves switching/reloading, not an improvement in personality quality.
"""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.manual.run_role_smoke import smoke_directory
from tests.manual.helpers import toml_value
from life.release import NativeWorker,Probe,ReleaseController
from life.role_config import Config,load_config
from life.supervisor import HostSupervisor


async def main():
    previous = load_config(ROOT/'config.toml')
    with smoke_directory() as folder:
        root = Path(folder)
        (root/'persona.md').write_text('你是合成测试角色。启动时自然说一句简短招呼，随后休息。',encoding='utf-8')
        frames = []
        async def platform(ws):
            async for line in ws:
                request = json.loads(line)
                frames.append(request)
                await ws.send(json.dumps(dict(echo=request['echo'],status='ok',retcode=0,data=dict(message_id=len(frames)))))
        async with websockets.serve(platform,'127.0.0.1',0) as server:
            cfg = Config(owner=1,api_key=previous.api_key,base_url=previous.base_url,model=previous.model,
                ws_url='ws://127.0.0.1:'+str(server.sockets[0].getsockname()[1]),
                persona=str(root/'persona.md'),life_db=str(root/'life.db'),bili_plugin='',idle_seconds=300,
                dsh_root=str(ROOT.parent/'dsh-bridge-work'),dsh_home=str(root/'dsh-home'))
            config = root/'config.toml'
            config.write_text('\n'.join(k+' = '+toml_value(v) for k,v in asdict(cfg).items()),encoding='utf-8')
            control = ReleaseController(ROOT,root/'control',NativeWorker())
            patch = dict(file='life/role_host.py',old="ROLE_RUNTIME_VERSION = '2026.10.23.1'",new="ROLE_RUNTIME_VERSION = '2026.10.23.2'")
            identity = control.prepare('verify-native-role-publish',patch)
            report = await asyncio.to_thread(control.evaluate,identity,[Probe('life.role_host','runtime_version',{},'2026.10.23.2')])
            print(json.dumps(dict(candidate=identity,verdict=report['verdict'],
                baseline_suite=report.get('baseline_suite',{}).get('ok'),candidate_suite=report.get('candidate_suite',{}).get('ok'))),flush=True)
            if report['verdict']!='publishable':
                print(json.dumps(report,ensure_ascii=False),flush=True)
                raise RuntimeError('native publish candidate did not pass')
            control.request_publish(identity)
            supervisor = HostSupervisor(control,config,startup_timeout=120)
            try:
                async with asyncio.timeout(180):
                    while True:
                        await asyncio.to_thread(supervisor.tick)
                        try:
                            health = json.loads(supervisor.health_path.read_text('utf-8'))
                        except (OSError,ValueError):
                            health = {}
                        if health.get('runtime',{}).get('version')=='2026.10.23.2' and frames:
                            break
                        await asyncio.sleep(1)
                print(json.dumps(dict(running_version=health['runtime']['version'],
                    greeting_received=bool(frames),platform_messages=len(frames),real_qq_messages=0,
                    original_goal_verified=health['runtime']['version']=='2026.10.23.2')))
            finally:
                await asyncio.to_thread(supervisor.stop)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
