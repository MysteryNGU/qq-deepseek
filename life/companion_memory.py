"""回查自己做过的动作：只读，不进上下文的细节用这个取。

生活轮只有几百字的行动摘要，工具轮才有完整回执；这条通道让生活轮也能按
关键词或 id 把当时的原文取回来，而不必把工具轮的大上下文整个搬过去。
"""

import json
import re
import time

READ_LIMIT = 4000
MAX_READ_LIMIT = 12000
SEARCH_LIMIT = 8
MAX_SEARCH_LIMIT = 20


def _fields(action):
    """op 和参数可能放在 arguments(JSON 字符串) 里，也可能在顶层。"""
    fields = action.get('arguments')
    if isinstance(fields, str):
        try:
            fields = json.loads(fields or '{}')
        except ValueError:
            fields = {}
    if not isinstance(fields, dict):
        fields = {}
    merged = dict(fields)
    for key in ('op', 'query', 'id', 'limit', 'kind'):
        if action.get(key) not in (None, '') and key not in merged:
            merged[key] = action[key]
    return merged


def _brief(result, limit=160):
    from .companion import result_view
    return result_view(result, limit)


UNPARSED = object()


def _parse_window(value):
    """时间窗：秒级时间戳，或本地时间字符串；看不懂给 UNPARSED（不静默忽略）。"""
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        return UNPARSED
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else UNPARSED
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return time.mktime(time.strptime(text, pattern))
        except ValueError:
            continue
    return UNPARSED


def _window(fields):
    return _parse_window(fields.get('since')), _parse_window(fields.get('until'))


class Lookup:
    """按 id 或关键词取回历史动作的原始回执。"""

    def __init__(self, repo, agent_id):
        self.repo, self.agent_id = repo, agent_id

    async def run(self, action):
        fields = _fields(action)
        op = str(fields.get('op') or 'search')
        if op not in ('search', 'read'):
            return dict(status='failed', op=op,
                        output='memory 的 op 支持 search/read，收到的是 %s' % op)
        if op == 'read':
            return self._read(fields)
        return self._search(fields, str(action.get('goal') or ''))

    def _row_view(self, row):
        action = json.loads(row['action'] or '{}')
        result = json.loads(row['result'] or '{}') or {}
        return dict(id=row['id'], at=time.strftime('%m-%d %H:%M', time.localtime(row['updated'] or row['created'])),
                    at_ts=row['updated'] or row['created'],
                    kind=action.get('kind'), status=row['status'],
                    project=row['project_ref'] or action.get('project'),
                    goal=(action.get('goal') or '')[:80],
                    summary=_brief(result, 140),
                    read_op=dict(kind='memory', op='read',
                                 arguments=json.dumps(dict(op='read', id=row['id']),
                                                      ensure_ascii=False)))

    # 小镇回执不在 companion_actions 的 result 里：交办时只存 waiting，
    # 真正的回执（下水、泡着晒太阳、花了多少）落在 companion_town_delegations。
    # 不回查这张表，她会以为自己在镇上的经历不存在。
    @staticmethod
    def _town_view(row):
        receipt = row['receipt'] or ''
        reason, state = '', row['state'] or ''
        try:
            payload = json.loads(receipt or '{}') or {}
            inner = payload.get('receipt') or {}
            if isinstance(inner, dict):
                reason = str(inner.get('reason') or '')
                state = str(inner.get('state') or state)
        except (TypeError, ValueError):
            reason = str(receipt)[:140]
        return dict(id=row['id'], at=time.strftime('%m-%d %H:%M', time.localtime(row['updated'] or row['created'])),
                    at_ts=row['updated'] or row['created'],
                    kind='town', status=state or 'answered',
                    project=row['project_ref'], goal=(row['goal'] or '')[:80],
                    summary=(reason or str(receipt))[:140])

    def _search(self, fields, goal):
        query = str(fields.get('query') or '').strip()
        try:
            limit = int(fields.get('limit') or SEARCH_LIMIT)
        except (TypeError, ValueError):
            limit = SEARCH_LIMIT
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        terms = [part for part in re.split(r'[\s,，、;；]+', query) if part][:4]
        # 可按动作种类与时间窗收窄：M-2 那种「这个时间窗内有没有对应 kind 的回执」
        # 要能直接查，不能靠人肉读全量结果再挑。
        kinds = fields.get('kind') or fields.get('kinds') or ''
        if isinstance(kinds, str):
            kinds = [part for part in kinds.replace('，', ',').split(',') if part.strip()]
        kinds = [str(item).strip() for item in kinds if str(item).strip()]
        since, until = _window(fields)
        bad = [key for key, value in (('since', since), ('until', until)) if value is UNPARSED]
        if bad:
            return dict(status='failed', op='search', query=query, results=[],
                        output='时间窗看不懂（%s）。只能是秒级时间戳或 2026-11-17 09:00 这种写法；'
                               '这次没有按你给的条件查，所以不能当成「查无此事」。' % '/'.join(bad))
        where, args = ['agent=?'], [self.agent_id]
        if terms:
            where.append(' AND '.join('(action LIKE ? OR result LIKE ?)' for _ in terms))
            for term in terms:
                like = '%' + term.replace('%', '').replace('_', '') + '%'
                args.extend([like, like])
        if kinds:
            where.append("COALESCE(json_extract(action,'$.kind'),'') IN (%s)" % ','.join('?' for _ in kinds))
            args.extend(kinds)
        if since is not None:
            where.append('COALESCE(updated,created)>=?')
            args.append(float(since))
        if until is not None:
            where.append('COALESCE(updated,created)<=?')
            args.append(float(until))
        args.append(limit)
        rows = self.repo._all('SELECT id,status,action,result,project_ref,created,updated '
                              'FROM companion_actions WHERE ' + ' AND '.join(where) + ' '
                              "AND COALESCE(json_extract(action,'$.kind'),'') NOT IN ('memory','growth') "
                              "AND result IS NOT NULL AND result NOT IN ('{}','null') "
                              'ORDER BY updated DESC LIMIT ?', tuple(args))
        towns = self._search_town(terms, limit) if not (kinds or since or until) else []
        found = [self._row_view(row) for row in rows]
        seen = {item['id'] for item in found}
        found.extend(item for item in towns if item['id'] not in seen)
        # 跨两张表按真实时间排序；格式化后的 %m-%d 字符串跨年比不出先后。
        found.sort(key=lambda item: item.get('at_ts') or 0, reverse=True)
        found = found[:limit]
        if not found:
            # 「查了、没有」和「这次没查成」不是一回事：前者是事实，后者是故障。
            # 用 status=failed 表示后者，用 completed + 空结果 + 明说「查无」表示前者。
            hint = ('没有匹配「%s」的动作。换个词，或者不带 query 看最近几条。' % query) if query \
                else '这个账号还没有任何动作记录。'
            if kinds or since is not None or until is not None:
                hint += '筛选条件是 kind=%s、时间窗 %s～%s。' % (
                    ','.join(kinds) or '不限',
                    time.strftime('%Y-%m-%d %H:%M', time.localtime(since)) if since else '不限',
                    time.strftime('%Y-%m-%d %H:%M', time.localtime(until)) if until else '不限')
            return dict(status='completed', op='search', query=query, count=0, results=[],
                        matched=False, terms=terms,
                        evidence_scope='stored_index',
                        evidence_note='这是检索到的目录，不是原文；要原文用 op=read 加 id。',
                        output=hint)
        lines = ['%s %s %s｜%s｜%s' % (item['at'], item['kind'], item['status'],
                                       item['goal'] or '（无目标）', item['summary'])
                 for item in found]
        return dict(status='completed', op='search', query=query, count=len(found),
                    results=found, evidence_scope='stored_index',
                    evidence_note='这是检索到的目录，不是原文；要原文用 op=read 加 id。',
                    output='找到 %d 条：\n%s' % (len(found), '\n'.join(lines)))

    def _search_town(self, terms, limit):
        """小镇回执单独查：goal/receipt 里常常是真正发生过的细节。"""
        where, args = '', [self.agent_id + '|town:%']
        if terms:
            parts = []
            for term in terms:
                like = '%' + term.replace('%', '').replace('_', '') + '%'
                parts.append('(goal LIKE ? OR receipt LIKE ?)')
                args.extend([like, like])
            where = ' AND ' + ' AND '.join(parts)
        args.append(limit)
        rows = self.repo._all('SELECT id,scope,project_ref,goal,state,receipt,wake_result,created,updated '
                              'FROM companion_town_delegations WHERE scope LIKE ?' + where + ' '
                              'AND receipt IS NOT NULL AND receipt<>%s ORDER BY updated DESC LIMIT ?' % "'{}'",
                              tuple(args))
        return [self._town_view(row) for row in rows]

    def _read(self, fields):
        identity = str(fields.get('id') or '').strip()
        if not identity:
            return dict(status='failed', op='read',
                        output='read 需要 id；先用 op=search 找到 id，再按 id 取原文。')
        try:
            limit = int(fields.get('limit') or READ_LIMIT)
        except (TypeError, ValueError):
            limit = READ_LIMIT
        limit = max(200, min(limit, MAX_READ_LIMIT))
        row = self.repo._one('SELECT id,status,action,result,project_ref,created,updated '
                             'FROM companion_actions WHERE agent=? AND id=?', (self.agent_id, identity))
        town = self.repo._one('SELECT id,scope,project_ref,goal,state,receipt,wake_result,created,updated '
                              'FROM companion_town_delegations WHERE id=? AND scope LIKE ?',
                              (identity, self.agent_id + '|town:%'))
        if not row and not town:
            return dict(status='failed', op='read', id=identity,
                        output='没有这个动作 id：%s。用 op=search 找一个真实的 id。' % identity)
        action = json.loads((row['action'] if row else '') or '{}')
        result = json.loads((row['result'] if row else '') or '{}') or {}
        text = _brief(result, limit) if row else ''
        kind = action.get('kind') if row else None
        goal = action.get('goal') if row else None
        saved_status = row['status'] if row else None
        project = (row['project_ref'] or action.get('project')) if row else None
        at = time.localtime((row['updated'] or row['created'])) if row else None
        if town:
            # 同一 id 在两边都有：小镇交办表中保存的是最终回执，优先它。
            town_text = self._town_text(town)
            if len(town_text) > len(str(text or '')):
                text, kind, goal = town_text, 'town', town['goal']
                saved_status = town['state']
                project = town['project_ref']
                at = time.localtime(town['updated'] or town['created'])
            elif not row:
                text = town_text
        if not str(text or '').strip():
            # 回执被清了、或者当时没跑完：这是「读不到」，不是「没发生过」。
            # 台账行仍在，状态与动作种类仍可引用；正文另找台账原文，别猜。
            at_ts = (town['updated'] or town['created']) if town and not row else (
                (row['updated'] or row['created']) if row else None)
            return dict(status='unreadable', op='read', id=identity, action_kind=kind,
                        saved_status=saved_status, project=project,
                        at=time.strftime('%m-%d %H:%M', time.localtime(at_ts)) if at_ts else None,
                        at_ts=at_ts,
                        evidence_scope='stored_receipt',
                        evidence_note='这一行的回执正文已经取不到了；它能证明动作发生过与最终状态，'
                                      '不能证明当时具体看到了什么。',
                        output='这条动作还在台账里（' + str(kind or '?') + '，'
                               + str(saved_status) + '），但存下的回执正文是空的：'
                               '可能是当时没跑完、也可能是回执被清理了。'
                               '这能说明这件事发生过，不能说明当时看到了什么；'
                               '要具体内容就换一条有正文的 id，别凭这一条推结论。')
        return dict(status='completed', op='read', id=identity, at=time.strftime('%m-%d %H:%M', at),
                    at_ts=(town['updated'] or town['created']) if town and not row else
                          (row['updated'] or row['created']) if row else None,
                    action_kind=kind, goal=goal,
                    target=action.get('arguments') or action.get('command') or '',
                    saved_status=saved_status, project=project,
                    output_path=result.get('output_path') or result.get('source_path'),
                    text=text, output=text, complete=len(text) < limit,
                    evidence_scope='stored_output',
                    evidence_note='这是当时保存下来的原始回执，不是你重新执行它的证据。')

    @staticmethod
    def _town_text(row):
        """小镇回执压成一行可读事实，再附原始 JSON 备查。"""
        raw = row['receipt'] or ''
        try:
            payload = json.loads(raw or '{}') or {}
        except (TypeError, ValueError):
            return str(raw)[:1200]
        inner = payload.get('receipt') or {}
        if not isinstance(inner, dict):
            return json.dumps(payload, ensure_ascii=False)[:1200]
        bits = [str(inner.get(key) or '') for key in ('reason',)]
        where = inner.get('where') or {}
        if isinstance(where, dict) and where.get('place_name'):
            bits.append('地点：' + str(where['place_name']))
        for key in ('cost', 'spent', 'money'):
            if inner.get(key) not in (None, ''):
                bits.append('花费：' + str(inner[key]))
                break
        bits.append('状态：' + str(inner.get('state') or row['state'] or ''))
        head = '｜'.join(bit for bit in bits if bit)
        return (head + '\n\n原始回执：' + json.dumps(payload, ensure_ascii=False))[:4000]
