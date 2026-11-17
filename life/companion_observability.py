"""Read-only operational facts for the desktop and phone, without model calls."""
from datetime import datetime, timedelta, timezone
import json
import re
import time

from .spending import reported_cost, UNIT

ZONE = timezone(timedelta(hours=8))
GROUPS = ('dialogue', 'activity', 'work', 'town', 'other')
TOKENS = ('input_tokens', 'cached_input_tokens', 'output_tokens')


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _text(value, limit=600):
    """No raw action arguments, configuration or credentials enter the projection."""
    text = str(value or '')
    text = re.sub(r'-----BEGIN [\w ]*PRIVATE KEY-----[\s\S]*?(?:-----END [\w ]*PRIVATE KEY-----|$)', '[已隐藏密钥]', text)
    text = re.sub(r'(?i)(bearer\s+)[\w.\-~+/=]+', r'\1[已隐藏]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|token|secret|password|authorization|接入码|密码)[\s\"\x27]*[:=：][\s\"\x27]*)[^\s\"\x27,;&}]+', r'\1[已隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[已隐藏密钥]', text)
    return text[:limit]


def _empty_usage():
    return dict(calls=0, input_tokens=0, cached_input_tokens=0, output_tokens=0,
                known_cny=0.0, unknown_calls=0)


def _result(row):
    action, result = _object(row.get('action')), _object(row.get('result'))
    return dict(action_id=row['id'], kind=_text(action.get('kind'), 50),
                status=_text(result.get('status') or row.get('status'), 50),
                completion=_text(result.get('completion'), 50),
                code=_text(result.get('code'), 80),
                summary=_text(result.get('output') or result.get('text') or result.get('error')),
                updated=row.get('updated'), project_ref=row.get('project_ref'))


def build_observability(repo, agent_id, snapshot, *, now=None):
    """SELECT-only projection. ``snapshot`` must contain actual in-memory phase.

    Today's totals use completion/record date in Beijing, matching companion_usage;
    known_cny is a conservative priced subtotal, never an invented provider invoice.
    Unknown costs and incomplete token counts remain visible.
    """
    now = time.time() if now is None else now
    midnight = datetime.fromtimestamp(now, ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    groups = {key: _empty_usage() for key in GROUPS}
    nano = {key: 0 for key in GROUPS}
    rows = repo._all('SELECT c.purpose,c.usage,u.payload FROM companion_usage c '
                     'LEFT JOIN usage_calls u ON u.call_key=c.id '
                     'WHERE c.agent=? AND c.created>=? AND c.created<?',
                     (agent_id, midnight.timestamp(), (midnight+timedelta(days=1)).timestamp()))
    for raw in rows:
        row = dict(raw)
        purpose = 'work' if row['purpose'] in {'executor','bilibili_read'} else row['purpose']
        group = purpose if purpose in groups else 'other'
        tally, usage = groups[group], _object(row['usage'])
        tally['calls'] += 1
        for key in TOKENS:
            value = usage.get(key)
            if type(value) is not int or value < 0:
                tally[key] = None
            elif tally[key] is not None:
                tally[key] += value
        cost = reported_cost(_object(row['payload']))
        if cost is None:
            tally['unknown_calls'] += 1
        else:
            nano[group] += cost
    total = _empty_usage()
    for name, group in groups.items():
        group['known_cny'] = round(nano[name]/UNIT, 9)
        total['calls'] += group['calls']
        total['unknown_calls'] += group['unknown_calls']
        for key in TOKENS:
            total[key] = total[key]+group[key] if total[key] is not None and group[key] is not None else None
    total['known_cny'] = round(sum(nano.values())/UNIT, 9)

    actions = [dict(r) for r in repo._all('SELECT id,project_ref,action,status,result,updated '
               'FROM companion_actions WHERE agent=? ORDER BY updated DESC LIMIT 50', (agent_id,))]
    phase = snapshot.get('phase') or dict(state='idle', running=[])
    phase = dict(state=phase.get('state', 'idle'), running=[_text(x, 80) for x in phase.get('running', [])])
    # Persisted rows alone cannot prove an executor is currently alive.
    current = [dict(id=r['id'],kind=_text(_object(r['action']).get('kind'),50),status=r['status'],
                    project_ref=r['project_ref'],updated=r['updated'])
               for r in actions if r['status']=='running' and phase['state']=='tools']
    recent = [_result(r) for r in actions if _object(r['result'])]
    projects = []
    for project in (snapshot.get('projects') or [])[:30]:
        ref = project.get('ref') or project.get('id') or project.get('key')
        found = repo._all('SELECT id,project_ref,action,status,result,updated FROM companion_actions '
                          "WHERE agent=? AND project_ref=? AND result IS NOT NULL AND result NOT IN ('','null','{}') "
                          'ORDER BY updated DESC LIMIT 1', (agent_id,ref)) if ref else []
        projects.append(dict(id=ref,goal=_text(project.get('goal'),300),status=_text(project.get('status'),50),
                             origin=_text(project.get('origin'),30),checkpoint=_text(project.get('checkpoint'),1200),
                             blocked_on=[_text(item,80) for item in project.get('blocked_on',[])],
                             last_progress_at=project.get('last_progress_at'),
                             next_step=_text(project.get('next_step'),300),
                             latest_result=_result(dict(found[0])) if found else None))
    next_wake = snapshot.get('next_wake')
    external_wait = next((p for p in snapshot.get('projects',[])
        if p.get('status') in {'active','waiting'} and
        p.get('continuation',{}).get('state')=='waiting_result'),None)
    if phase['state']=='model':
        reason = '模型请求进行中'
    elif phase['state']=='tools':
        reason = '执行器正在处理行动'
    elif snapshot.get('next_continuation'):
        reason = '等待继续项目：'+_text(snapshot['next_continuation'].get('goal'),150)
    elif external_wait:
        reason = '等待外部行动的新回执：'+_text(external_wait.get('goal'),150)
    elif not snapshot.get('active'):
        reason = '自主活动已暂停；仍可接收消息'
    elif isinstance(next_wake,(int,float)) and next_wake>now:
        reason = '空闲；等待消息或下一次自主唤醒'
    else:
        reason = '空闲；当前没有模型请求或执行器任务'
    errors = [dict(created=r['created'],summary=_text(r['text'])) for r in repo._all(
        "SELECT created,text FROM companion_activity WHERE agent=? AND type='error' ORDER BY created DESC LIMIT 10", (agent_id,))]
    return dict(generated_at=now,phase=phase,current_actions=current,
                waiting=dict(reason=reason,next_wake=next_wake,
                             next_step=next((p['next_step'] for p in projects if p['status']=='active' and p['next_step']),'')),
                projects=projects,today=dict(date=midnight.date().isoformat(),timezone='Asia/Shanghai',
                    cost_basis='reported_usage_conservative_tariff',groups=groups,total=total),
                recent_results=recent[:20],errors=errors)
