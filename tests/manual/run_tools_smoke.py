"""Real model/DSH checks for the nine metered tools; isolated data, local delivery."""
import asyncio
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from life.expression import Expressions
from life.role_agent import RoleAgent
from life.role_config import load_config
from life.runtime import LifeRuntime
from life.services.dsh_bridge import DshBridge
from tests.manual.helpers import report_path


def verify_results(report):
    events = report['tool_events']
    names = {e['data']['callId']:e['data']['name'] for e in events if e.get('type')=='tool/call'}
    job_outputs = ['\n'.join(b.get('text','') for b in e['data']['message']['content'])
        for e in events if e.get('type')=='tool/result' and names.get(e['data']['message'].get('toolCallId'))=='job_output']
    report['job_verification'] = dict(
        normal_job_completed=any('JOB_READY' in t and 'JOB_DONE' in t and '[status: completed, exit code: 0]' in t for t in job_outputs),
        cancelled_job_settled=any('[status: killed' in t for t in job_outputs))
    expected = {'pwsh','job_output','job_kill'}
    report['native_tools_succeeded'] = sorted({r['tool'] for r in report['native_operations'] if r['status']=='succeeded'} & expected)
    required = {'life_read','life_say','life_remember','life_intent','life_appraise','life_rest'}
    report['all_nine_succeeded'] = (required <= set(report['life_success_counts']) and
        expected <= set(report['native_tools_succeeded']) and report['marker_delivered'] and
        report['adapter_matches_source'] and all(report['job_verification'].values()))
    return report['all_nine_succeeded']


async def main():
    root = ROOT / 'data' / 'test-results' / ('tools-smoke-' + uuid.uuid4().hex[:12])
    root.mkdir(parents=True)
    (root / 'skills').mkdir()
    (root / 'persona.md').write_text('你是合成工具验收角色。按要求实际调用工具，以真实回执核对结果。所有输入、记忆和关系仅属于测试。', encoding='utf-8')
    marker = 'tool-probe-' + uuid.uuid4().hex
    (root / 'marker.txt').write_text(marker, encoding='utf-8')
    cfg = replace(load_config(ROOT / 'config.toml'), owner=1, persona='persona.md',
        dsh_root=str(ROOT / 'vendor/dsh-bridge-work'), dsh_home=str(root / 'dsh-home'),
        consult_config='', bili_plugin='', continuity_enabled=False, dsh_timeout=120)
    life = LifeRuntime(root / 'life.db')
    bridge = DshBridge(cfg, root)
    frames, calls, phases = [], [], []
    async def transport(action, params):
        frames.append(dict(action=action, params=params))
        return dict(retcode=0, data=dict(message_id=len(frames)))
    expressions = Expressions(life, None, None, transport)
    role = RoleAgent(life, bridge, cfg, root, expressions)
    original = role.callback
    async def observed(call):
        started = time.monotonic()
        result = await original(call)
        if call.get('kind', '').startswith('life.'):
            entry = dict(tool=call['kind'].replace('.', '_'), proposal=call.get('proposal'),
                receipt=result, seconds=round(time.monotonic()-started, 3))
            calls.append(entry)
            print(json.dumps(dict(tool=entry['tool'], ok=result.get('ok'), code=result.get('code')), ensure_ascii=False), flush=True)
        return result
    role.callback = observed
    report = dict(workspace=str(root), dsh_source=cfg.dsh_root, delivery='local test transport; no QQ messages', phases=phases, life_calls=calls)
    try:
        await role.start()
        adapter = Path(cfg.dsh_root) / 'packages/sdk/server/src/qq-life-sessions.ts'
        adapter_hash = hashlib.sha256(adapter.read_bytes())
        for name in ('qq-life-context.ts', 'qq-life-consult.ts'):
            adapter_hash.update(hashlib.sha256(adapter.with_name(name).read_bytes()).hexdigest().encode())
        report['loaded_adapter_version'] = bridge.loaded_adapter_version
        report['expected_adapter_version'] = adapter_hash.hexdigest()
        report['adapter_matches_source'] = bridge.loaded_adapter_version == adapter_hash.hexdigest()
        print('adapter_matches_source=' + str(report['adapter_matches_source']), flush=True)
        assert report['adapter_matches_source'], 'Loaded DSH adapter differs from the Git source'
        scope = life.scope('qq:private:1', '1')
        prompts = [
            ('state', '这是合成工具验收。我的测试偏好是“验收回复简短”，请通过 life_remember 保存这个偏好。'
                '请显式分别调用 life_intent(action=resolve) 确认输入、life_read(kind=self)、life_appraise（对本条普通请求做中性评价，不改变关系）。'
                '这些调用用于逐项验收，不能合并到 say 的 appraisal。最后 life_say 简短回复“状态验收完成”，end_turn:true。'),
            ('shell_jobs', '这是本机工具验收，请先确认输入并简短告知，再实际调用 pwsh 读取当前目录 marker.txt 的完整内容。'
                '然后通过 pwsh(run_in_background:true) 启动一个输出 JOB_READY、等待3秒、输出 JOB_DONE 的 PowerShell 测试进程，'
                '用 job_output 等待并核对其正常完成。再用 pwsh(run_in_background:true) 启动一个只等待120秒的测试进程，'
                '用 job_kill 终止刚创建的第二个任务，再用 job_output 核对停止状态。只操作你本轮新建的测试任务。'
                '最后 life_say 返回 marker.txt 的完整内容及两个任务实际结果，end_turn:true。'),
            ('rest', '现在进行静默结束工具验收。请确认已理解本条输入，然后实际调用 life_rest，reason 为“合成验收结束”。不要发送回复，不建立新任务。'),
        ]
        for name, text in prompts:
            before = len(calls)
            event, _ = life.ingest_message(scope, text)
            await role.ingest(event)
            sid = role.sessions.binding(scope)['session_id']
            async with asyncio.timeout(240):
                while True:
                    await asyncio.sleep(.5)
                    done = [c for c in calls[before:] if c['receipt'].get('ok') and
                        (c['tool']=='life_rest' if name=='rest' else c['tool']=='life_say' and c['receipt'].get('turn_complete'))]
                    if done:
                        break
            async with asyncio.timeout(60):
                while True:
                    binding = role.sessions.binding(scope)
                    page = await asyncio.to_thread(bridge.session_events, sid, -1, 1, binding['generation'])
                    if page.get('agent_status') == 'idle':
                        break
                    await asyncio.sleep(.5)
            await role.replay(sid)
            phases.append(dict(name=name, completed=True))
            print('phase_completed=' + name, flush=True)
        events = [json.loads(r['payload']) for r in life.repo._all('SELECT payload FROM agent_events ORDER BY rowid')]
        report['tool_events'] = [e for e in events if 'tool' in e.get('type', '')]
        report['main_session_id'] = sid
        report['request_headers'] = [dict(session_id=row['session_id'],
            tools=[t['name'] for t in json.loads(row['payload'])['data']['header'].get('tools',[])])
            for row in life.repo._all("SELECT session_id,payload FROM agent_events WHERE json_extract(payload,'$.type')='request/header' ORDER BY rowid")]
        usage_path = root/'dsh-home/qq-life/usage.jsonl'
        latest = {}
        for line in usage_path.read_text('utf-8').splitlines():
            record = json.loads(line)
            if record['revision'] > latest.get(record['call_key'],{}).get('revision',0):
                latest[record['call_key']] = record
        report['model_usage'] = list(latest.values())
        native = [dict(r) for r in life.repo._all('SELECT tool,status FROM role_operations')]
        report['native_operations'] = native
        report['life_success_counts'] = dict(Counter(c['tool'] for c in calls if c['receipt'].get('ok')))
        report['life_failure_counts'] = dict(Counter(c['tool'] for c in calls if not c['receipt'].get('ok')))
        report['local_delivery_frames'] = frames
        report['marker_delivered'] = marker in json.dumps(frames, ensure_ascii=False)
        verify_results(report)
        print(json.dumps({k:v for k,v in report.items() if k not in {'tool_events','life_calls','local_delivery_frames','model_usage','request_headers'}}, ensure_ascii=False), flush=True)
        assert report['all_nine_succeeded'], 'Not all nine tools have successful execution evidence'
    except BaseException as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        try:
            await role.close()
        finally:
            life.close()
            destination = report_path('tools-smoke.json')
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print('report=' + str(destination), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
