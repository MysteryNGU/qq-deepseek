"""Progress fences derived from current-turn receipts and real deliveries."""
import json

from .agent_session import SessionFailure,digest
from .interaction_mode import WORK_CONTROLS


EXEMPT={'life_say','life_rest','life_intent','life_appraise',*WORK_CONTROLS}


def state(role,scope,root):
    empty=dict(due=False,completed_since_reply=0,repeat=None)
    if scope.conversation_id!='qq:private:'+str(role.cfg.owner) or role.interaction_mode.state(scope)['mode']!='tools':
        return empty
    turn=role.turn_number(root['session_id'])
    if turn is None:return empty
    # Autonomous/background wakes do not invent an obligation to message anyone.
    user=role.repo._one("SELECT e.received_at FROM agent_inputs i JOIN events e ON e.id=i.input_id "
            "WHERE i.session_id=? AND i.claimed_turn=? AND e.source_kind='user_report' AND e.redacted_at IS NULL "
            'ORDER BY e.received_at DESC LIMIT 1',
            (root['session_id'],turn))
    if not user:
        return empty
    rows=role.repo._all("SELECT payload,applied FROM agent_events WHERE session_id=? "
        "AND json_extract(payload,'$.data.turn')=? AND json_extract(payload,'$.type') IN "
        "('turn/start','step/start','tool/call','tool/result') ORDER BY seq",(root['session_id'],turn))
    events=[(json.loads(r['payload']),r['applied']) for r in rows]
    if not events:return empty
    at=lambda e,fallback:e.get('time',fallback*1000)/1000
    start=at(*events[0])
    delivered=role.repo._one("SELECT MAX(updated) at FROM deliveries WHERE scope_id=? "
        "AND status='confirmed' AND forget_epoch=? AND updated>=?",
        (scope.scope_id,role.repo.forget_epoch(scope.scope_id),start))
    last_reply=delivered['at'] if delivered and delivered['at'] is not None else start
    calls={};completed=[]
    for e,applied in events:
        d=e['data']
        if e['type']=='tool/call':
            try:args=json.loads(d['arguments'])
            except (ValueError,TypeError):continue
            calls[d['callId']]=(d['name'],args)
        elif e['type']=='tool/result':
            message=d['message'];call=calls.get(message.get('toolCallId'))
            if not call or call[0] in EXEMPT:continue
            body='\n'.join(b.get('text','') for b in message.get('content',[]) if b.get('type')=='text')
            # Denials requesting progress are not completed work or new evidence.
            if message.get('isError'):
                try:
                    if json.loads(body.removeprefix('Error: ')).get('code') in {
                        'progress_required','work_repeated','input_pending','acknowledgement_required'}:continue
                except ValueError:pass
            completed.append(dict(name=call[0],arguments_hash=digest(call[1]),
                result_hash=digest(dict(body=body,error=bool(message.get('isError')))),at=at(e,applied),seq=e['seq']))
    since=[r for r in completed if r['at']>last_reply]
    repeat=None;n=role.resources.work_repeat_limit
    if len(completed)>=n:
        tail=completed[-n:]
        keys={(r['name'],r['arguments_hash'],r['result_hash']) for r in tail}
        if len(keys)==1 and tail[0]['at']>=user['received_at']:
            repeat=dict(tool=tail[-1]['name'],arguments_hash=tail[-1]['arguments_hash'],evidence_seqs=[r['seq'] for r in tail])
    elapsed=max(0,role.life.clock()-last_reply)
    due=bool(since) and (len(since)>=role.resources.work_progress_tool_limit
        or elapsed>=role.resources.work_progress_seconds)
    return dict(due=due,completed_since_reply=len(since),seconds_since_reply=round(elapsed),repeat=repeat,
        instruction='先自然说明实际发现、尚未找到的材料或具体阻碍，再继续；不要重复开工告知或编造完成。相同操作和结果连续重复时换有依据的方法，或保存真实阻碍结束本轮。')


def require(role,scope,root,name,arguments):
    if name in EXEMPT:return
    progress=state(role,scope,root)
    if progress['due']:
        raise SessionFailure('progress_required','操作尚未执行。先单独 life_say(purpose:notice,end_turn:false) '
            '说明已有发现或具体阻碍；真实送达后继续，不重试原命令，也不等待用户催问。',work_progress=progress)
    repeat=progress['repeat']
    if repeat and repeat['tool']==name and repeat['arguments_hash']==digest(arguments):
        raise SessionFailure('work_repeated','同一操作已经连续得到相同结果，原样重试未执行。'
            '用已有证据说明阻碍，换有依据的查找方法，或保存检查点和真实 activity_blocker 后结束本轮。',work_progress=progress)
