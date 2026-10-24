"""Real SDK profile probe in an isolated home, with no QQ or model requests."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from life.role_config import Config
from life.services.dsh_bridge import DshBridge
from life.tool_boundary import outcome_ok


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='qq-continuity-sdk-') as directory:
        base=Path(directory)
        cfg=Config(owner=1,continuity_enabled=True,dsh_root=str(ROOT.parent/'dsh-bridge-work'),
            dsh_home=str(base/'home'),bili_plugin=str(base/'no-plugin'),
            api_key='offline-fixture',base_url='http://127.0.0.1:9',model='offline-unrequested',dsh_timeout=60)
        bridge=DshBridge(cfg,base)
        callbacks=[]
        def host(call):
            callbacks.append(call.get('kind'))
            return dict(ok=True,stable_text='Synthetic fixture role.',text='Synthetic material.',
                continuity_protocol=1,continuity_enabled=True,operation_id='fixture-operation')
        bridge.register_handler('qq_life/life/read',host)
        bridge.register_handler('qq_life/life/commit',host)
        try:
            hello=bridge.hello()
            assert hello.get('continuity_protocol')==1 and hello.get('continuity_enabled') is True
            sid='continuity-probe-'+uuid.uuid4().hex
            page=bridge.session_events(sid,-1,generation=1)
            assert page.get('agent_status')=='idle',page
            tools=bridge.tools(scope=sid)
            descriptions={row['name']:row['description'] for row in tools}
            for name in ('life_read','life_remember','life_intent','life_rest','life_say'):
                assert 'continuity_protocol:1' in descriptions[name],name
            fixture=base/'read-probe.txt'
            fixture.write_text('actual isolated continuity SDK file evidence',encoding='utf-8')
            operation='continuity-read-'+uuid.uuid4().hex
            arguments=dict(file_path=str(fixture))
            result=bridge.execute('read',arguments,operation_id=operation,scope=sid)
            assert outcome_ok(result),result
            assert fixture.read_text('utf-8') in json.dumps(result,ensure_ascii=False)
            assert bridge.execute('read',arguments,operation_id=operation,scope=sid)==result
            usage=bridge.resource_events(sid,generation=1)
            assert usage.get('records')==[],usage
            adapter=Path(cfg.dsh_root)/'packages/sdk/server/src/qq-life-sessions.ts'
            actual=hashlib.sha256(adapter.read_bytes()).hexdigest()
            assert hello.get('roleAdapterVersion')==actual
            report=dict(schema=1,continuity_protocol=1,continuity_enabled=True,
                adapter_source_match=True,adapter_sha256=actual,scoped_tools_visible=True,
                real_read_confirmed=True,idempotent_read=True,model_requests=0,qq_messages=0,
                profile_home='isolated temporary directory',callbacks=callbacks)
            args.out.parent.mkdir(parents=True,exist_ok=True)
            args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            bridge.close()


if __name__=='__main__':
    main()
