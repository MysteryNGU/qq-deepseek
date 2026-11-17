"""Read-only projection behind the life page. It never calls the model."""
import json
import re
import time
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
# An accepted command or a saved plan is not a lived result. Old activity rows
# without result/observation evidence remain in the audit log.
LIVED_SQL = """(type='experience' OR (type='activity' AND (
    (json_extract(details,'$.result.status') IN ('succeeded','completed','failed','partial','interrupted')
     AND COALESCE(json_extract(details,'$.result.completion'),'') NOT IN ('accepted','queued'))
    OR json_type(details,'$.body_event')='object')))"""


def _age(created, now):
    delta = max(0.0, now - created)
    if delta < 90:
        return '刚刚'
    if delta < 5400:
        return f'{int(delta // 60)}分钟前'
    if delta < 172800:
        return f'{int(delta // 3600)}小时前'
    return f'{int(delta // 86400)}天前'


def _rows(core, *, limit, cursor, now):
    sql = ("SELECT id,type,text,details,created FROM companion_activity WHERE agent=? "
           "AND created<=? AND (type IN ('recollection','letter') OR " + LIVED_SQL + ')')
    args = [core.life.agent_id, now]
    if cursor is not None:
        sql += ' AND created < ?'
        args.append(float(cursor))
    sql += ' ORDER BY created DESC LIMIT ?'
    args.append(int(limit))
    return core.repo._all(sql, tuple(args))


def _midnight(now):
    return datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def today_rows(core, now=None, limit=40):
    now = time.time() if now is None else now
    return core.repo._all('SELECT id,type,text,details,created FROM companion_activity '
        'WHERE agent=? AND created>=? AND created<=? AND '+LIVED_SQL+' ORDER BY created DESC LIMIT ?',
        (core.life.agent_id, _midnight(now), now, int(limit)))


def _item(row, now, core=None, actions=None):
    try:
        details=json.loads(row['details'] or '{}')
    except (TypeError, ValueError):
        details={}
    result=details.get('result') or {}
    kind='experience' if row['type']=='activity' else row['type']
    item=dict(id=row['id'],kind=kind,text=str(row['text']),age=_age(row['created'],now),created=row['created'])
    if result:
        item['observed_outcome']=str(result.get('output') or row['text'])[:600]
        item['text']=item['observed_outcome']
    if details.get('reflection'):
        item['reflection']=str(details['reflection'])
    if row['type']=='recollection':
        item.update(why=str(details.get('why') or ''),source_ref=details.get('source_ref'))
    prose=str(details.get('reflection') or item['text']).strip()
    try:
        structured=json.loads(prose)
        if isinstance(structured,dict):
            prose=next((str(structured[k]) for k in ('summary','title','text','description') if isinstance(structured.get(k),str)), '')
        else:prose=''
    except (ValueError,TypeError):pass
    technical=not prose or bool(re.search(r'Traceback|SELECT .* FROM|\b(?:action_id|source_ref|workspace_id)\b|^[/\\]|^total \d|^\s*[\[{]',prose))
    action=(actions or {}).get(details.get('action_id'),{})
    readable=_readable_result(action,result)
    if readable:
        prose=readable
        technical=False
    item['technical']=technical
    item['display_text']=('这次尝试没有完成，具体原因留在运行记录里。' if result.get('status')=='failed' else '完成了一次操作，详细结果留在运行记录里。') if technical else prose[:1600]
    item['day_label']=datetime.fromtimestamp(row['created'],BEIJING).strftime('%Y-%m-%d')
    item['activity_kind']=action.get('kind','')
    item['group_key']=str(action.get('kind',''))+':'+str(action.get('project','')) if action else item['id']
    item['time_label']=datetime.fromtimestamp(row['created'],BEIJING).strftime('%m月%d日 %H:%M')
    return item


def _readable_result(action, result):
    """Explain observed tool effects, never turn generated prose into a physical act."""
    kind=action.get('kind')
    goal=str(action.get('goal') or '').strip()
    if not kind:return ''
    failed=result.get('status') in {'failed','interrupted'}
    if kind=='body':
        if failed:
            return ('想动一动桌面形象，这次桌面没有连接。'
                    if 'Desktop control unavailable' in str(result.get('output') or '') else '这次动作没有完成。')
        return '桌面形象完成了一次动作。'
    if kind=='command':
        if failed:return '整理资料时，有一步命令没有执行成功。'
        return '在云端处理了一次数字资料。'+('原本想做的是：'+goal[:160]+'。' if goal else '')
    if kind=='web':
        title=str(result.get('title') or '').strip()
        if failed:return '想上网查些资料，这次没有读到页面。'
        if action.get('op')=='search':
            query=str(result.get('query') or '')
            return '上网找了'+('“'+query[:120]+'”的相关资料。' if query else '一些资料。')+'搜索结果还需要继续打开阅读。'
        return '浏览了'+('《'+title[:160]+'》。' if title else '一个网页。')
    if kind=='bilibili':
        output=str(result.get('output') or '')
        title=''
        match=re.search(r'来源[：:]\s*([^\n]+?)(?:https?://|\n|$)',output)
        if match:title=match.group(1).strip()
        if action.get('op')=='bili_search':return '在B站找感兴趣的内容。'+('这次想找：'+goal[:180]+'。' if goal else '')
        op=action.get('op','')
        if op not in {'bili_watch','bili_subtitle'}:
            if failed:return '在B站尝试了一次操作，没有完成。'
            return {'bili_comments':'读了B站视频下的评论。','bili_video_info':'查看了B站视频的标题与简介。',
                    'bili_reply':'在B站提交了一条评论。','bili_send_message':'在B站提交了一条私信。'}.get(op,'查看了B站的一些信息。')
        if failed or result.get('completion')=='not_read':return '想读一条B站内容，但这次没有读到正文。'+('原本想了解：'+goal[:150]+'。' if goal else '')
        coverage=result.get('coverage') or {}
        visual='视觉' in output or '九宫格' in output
        return ('读了《'+title+'》。' if title else '读了一条B站内容。')+('看了抽样画面，画面之外的细节还不能确认。' if visual else '根据可用字幕阅读。')+('这次关注：'+goal[:180]+'。' if goal else '')
    if kind in {'phone','computer'}:
        if failed:return '想操作'+('手机' if kind=='phone' else '电脑')+'，这一步没有完成。'
        return '在'+('手机' if kind=='phone' else '电脑')+'上完成了一步操作。'+('当时想做的是：'+goal[:180]+'。' if goal else '')
    if kind=='town':
        if failed:return '想去小镇看看，这次没有完成。'
        return '查看了小镇的活动情况。'+('当时的打算是：'+goal[:180]+'。' if goal else '')
    return ''


def _readable_page(core, rows, now):
    ids=[]
    for row in rows:
        try:action_id=json.loads(row['details'] or '{}').get('action_id')
        except (ValueError,TypeError):continue
        if action_id:ids.append(action_id)
    actions={}
    # Chunk to stay below SQLite parameter limits; no per-entry queries.
    for start in range(0,len(ids),400):
        batch=ids[start:start+400]
        for saved in core.repo._all('SELECT id,action FROM companion_actions WHERE agent=? AND id IN ('+','.join('?' for _ in batch)+')',
                                    (core.life.agent_id,*batch)):
            try:actions[saved['id']]=json.loads(saved['action'])
            except (ValueError,TypeError):pass
    items=[]
    for row in rows:
        item=_item(row,now,core,actions)
        if item['technical']:continue
        # A short burst of searches/reads on one project is one browsing episode.
        if (items and item['activity_kind'] in {'web','bilibili','command'}
                and items[-1]['group_key']==item['group_key']
                and items[-1]['created']-item['created']<900):
            prior=items[-1]
            if item['display_text'] not in prior['display_text'] and len(prior['display_text'])<900:
                prior['display_text']+=' '+item['display_text']
            prior.setdefault('source_ids',[prior['id']]).append(item['id'])
        else:items.append(item)
    return items


def _town_experiences(core, now, *, before=None):
    rows=core.repo._all("SELECT id,payload,occurred_at FROM events WHERE agent_id=? AND conversation_id=? AND kind='town_delegation_result' AND redacted_at IS NULL AND occurred_at<=? ORDER BY occurred_at DESC LIMIT 160",
                       (core.life.agent_id,core.scope.conversation_id,now))
    result=[]
    for row in rows:
        at=row['occurred_at']
        if (before is None and at<_midnight(now)) or (before is not None and at>=before):continue
        try:
            payload=json.loads(row['payload'])
            receipt=(payload.get('receipt') or {}).get('receipt') or {}
        except (ValueError,TypeError):continue
        reason=str(receipt.get('reason') or '').strip()
        if not reason or receipt.get('state') not in {'done','knocking','waiting'}:continue
        result.append(_item(dict(id=row['id'],type='experience',text='在小镇：'+reason,
            details=json.dumps(dict(source_ref=row['id'])),created=at),now))
    return result


def build(core, *, now=None, limit=24, cursor=None):
    now=time.time() if now is None else now
    limit=max(1,min(80,int(limit)))
    rows=_rows(core,limit=limit,cursor=cursor,now=now)
    page=_readable_page(core,rows,now)
    today=_readable_page(core,today_rows(core,now,1000),now)
    today=sorted(today+_town_experiences(core,now),key=lambda i:i['created'],reverse=True)
    # Sample across the day when an unusually busy day exceeds the display size.
    if len(today)>limit:
        indices=sorted({i*(len(today)-1)//(limit-1) for i in range(limit)}) if limit>1 else [0]
        today=[today[i] for i in indices]
    past_rows=_rows(core,limit=1000,cursor=_midnight(now),now=now)
    past=sorted(_readable_page(core,past_rows,now)+_town_experiences(core,now,before=_midnight(now)),key=lambda i:i['created'],reverse=True)
    latest={}
    for kind in ('letter','recollection'):
        row=core.repo._one('SELECT id,type,text,details,created FROM companion_activity WHERE agent=? AND type=? AND created<=? ORDER BY created DESC LIMIT 1',
                          (core.life.agent_id,kind,now))
        latest[kind]=_item(row,now,core) if row else None
    interests=[]
    for project in core._projects():
        if project.get('origin')!='self' or project.get('status') not in {'active','waiting'}:continue
        interests.append(dict(key=project.get('key',project['ref']),goal=project.get('goal'),
            question=project.get('question',''),next_idea=project.get('next_idea') or project.get('next_step',''),status=project.get('status')))
    changes=[]
    for row in core.repo._all("SELECT id,text,details,created FROM companion_activity WHERE agent=? AND type='note_revision' AND created<=? ORDER BY created DESC LIMIT 8",(core.life.agent_id,now)):
        try:d=json.loads(row['details'])
        except (ValueError,TypeError):continue
        before=str(d.get('before') or '');after=str(d.get('after') or '')
        if not before or not after:continue
        changes.append(dict(id=row['id'],before=before,after=after,reason=str(d.get('reason') or ''),
            display_text='之前记着：'+before+' 现在修订为：'+after,time_label=datetime.fromtimestamp(row['created'],BEIJING).strftime('%m月%d日 %H:%M')))
    summary=' '.join(i['display_text'] for i in reversed(today[-3:]))
    return dict(now=datetime.fromtimestamp(now,BEIJING).strftime('%Y-%m-%d %H:%M'),today_preview=today,
        interest_preview=interests[:6],recollection=latest['recollection'],latest_letter=latest['letter'],
        page=page,next_cursor=rows[-1]['created'] if len(rows)==limit else None,generated_at=now,
        past_preview=past[:80],past_has_more=len(past)>80 or len(past_rows)==1000,
        ideas_changes=changes,absence_summary=summary)
