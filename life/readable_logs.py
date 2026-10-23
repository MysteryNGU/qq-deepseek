"""Two read-only, replaceable views of committed runtime records.

No model calls, state advancement, second database, or append-time deduplication.
Real repeated tool calls remain separate; repeated exports do not add records.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import threading
import os

from .drives import DRIVE_LABELS

LOCAL = timezone(timedelta(hours=8))
EMOTIONS = {'joy': '喜悦', 'concern': '担心', 'frustration': '挫折',
            'hurt': '受伤', 'anticipation': '期待'}
STATUS = {'done': '回合结束', 'queued': '排队', 'running': '执行中',
          'pending': '待执行', 'waiting': '等待条件', 'paused': '暂停',
          'cancelled': '已中止', 'unknown': '结果未知', 'quarantined': '已隔离'}
WRITE_LOCK = threading.Lock()


def unpack(text, default=None):
    try:
        return json.loads(text) if text else default
    except (ValueError, TypeError):
        return default


def stamp(at):
    return datetime.fromtimestamp(float(at), LOCAL).strftime('%Y-%m-%d %H:%M:%S')


def block(label, text):
    return label + str(text or '（空）').replace('\r', '').replace('\n', '\n    ')


def tool_lines(trace, start=1):
    lines = []
    for index, call in enumerate(unpack(trace['tool_calls'], []) or [], start):
        args = call.get('args') or {}
        detail = ' '.join(f'{k}={str(args[k])[:160]}' for k in
                          ('action', 'kind', 'path', 'pattern', 'query', 'slug') if args.get(k))
        when = stamp(call['ts']) if call.get('ts') else '历史未记录单步时间'
        lines.append(f"  工具 {index} [{when}]：{call.get('tool', '?')} {detail}".rstrip())
        # Tool return is evidence, not proof that the user's goal was achieved.
        lines.append(block('    返回：', call.get('result') or
                           ('尚无返回，是否仍在执行未确认' if call.get('status') == 'running' else '未记录返回内容')))
        if call.get('evidence') and call['evidence'] != call.get('result'):
            lines.append(block('    验证证据：', call['evidence']))
    return lines


class ReadableLogs:
    def __init__(self, database, directory, limit=300):
        self.database, self.directory = Path(database), Path(directory)
        self.limit = max(0, int(limit))

    def render(self):
        conn = sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN')
            def rows(table, order):
                return [dict(r) for r in conn.execute(
                    f'SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?', (self.limit or -1,))]
            events = {r['id']: dict(r) for r in conn.execute('SELECT * FROM events')}
            turns = rows('turns', 'created_at')
            inputs = {}
            for r in conn.execute('SELECT * FROM turn_inputs'):
                inputs.setdefault(r['turn_id'], []).append(r['event_id'])
            meta = {r['k']: r['v'] for r in conn.execute(
                "SELECT * FROM meta WHERE k LIKE 'input_intent:%' OR k LIKE 'task_control:%'")}
            from .audit import trace_record
            traces = [trace_record(row, encoded=True) for row in rows('decision_records', 'created_at')
                      if json.loads(row['behavior_plan'] or '{}').get('trace') is not None]
            deliveries = rows('deliveries', 'created')
            journals = rows('state_journal', 'id')
            settlements = rows('drive_settlements', 'created_at')
            jobs = rows('work_jobs', 'created')
            wants = rows('wants', 'expires')
            studies = rows('study_threads', 'created_at')
            thoughts = rows('thought_events', 'id')
            cognition = conn.execute("SELECT v FROM service_meta WHERE k='cognition:status'").fetchone()
            snapshots = [dict(r) for r in conn.execute(
                'SELECT s.* FROM state_snapshots s JOIN '
                '(SELECT aggregate_id,MAX(version) AS version FROM state_snapshots GROUP BY aggregate_id) t '
                'ON s.aggregate_id=t.aggregate_id AND s.version=t.version')]
        finally:
            conn.close()
        visible = {k: r for k, r in events.items() if r['redacted_at'] is None}
        def text(e):
            return (unpack(e['payload'], {}) or {}).get('text', '')
        def live(ids):
            return bool(ids) and all(i in visible for i in ids)
        window = f'每类最近 {self.limit} 条' if self.limit else '全部已保存记录'
        activity = ['活动日志（北京时间；' + window + '）',
                    '依据 life.db 已提交记录生成；回合结束不等于目标已验证。相同工具真实执行多次会分别保留。', '']
        inner = ['内在状态日志（北京时间；' + window + '）',
                 '展示已保存的状态、评价和明确记录的想法；不是模型隐藏思考全文。数值使用系统原始 0~1 标度。', '']
        associated = set()
        for turn in reversed(turns):
            sources = inputs.get(turn['id'], [])
            if not live(sources):
                continue
            source_rows = [visible[i] for i in sources]
            log_commands = {'/日志', '/活动日志', '/内心日志', '/内在日志', '/想法', '/你在想什么', '/心里话'}
            inspecting_log = any(text(e).strip().replace('／', '/', 1).split(' ', 1)[0] in log_commands for e in source_rows)
            activity.append(f"[{stamp(turn['created_at'])}] {turn['conversation_id']} | {turn['id']} | {STATUS.get(turn['status'], turn['status'])}")
            for e in source_rows:
                activity.append(block('  收到：', text(e)))
                intent = unpack(meta.get('input_intent:' + e['id']), {}) or {}
                if intent:
                    activity.append(block('  意图：', intent.get('goal') or intent.get('kind')))
                control = unpack(meta.get('task_control:' + e['id']), {})
                if control:
                    activity.append(block('  中止处理：', json.dumps(control, ensure_ascii=False)))
            matched = []
            for tr in traces:
                exact = tr['session_id'] == turn['id']
                # Old traces lack turn IDs; only associate an unambiguous exact trigger.
                legacy_route = (tr['session_id'] in {f"private:{turn['participant_id']}", f"group:{turn['conversation_id'].split(':')[-1]}"}
                                or turn['conversation_id'] == 'console:local' and tr['session_id'].startswith('console:'))
                candidates = [t for t in turns if t['conversation_id'] == turn['conversation_id']
                              and live(inputs.get(t['id'], []))
                              and tr['trigger'] in {text(visible[i])[:500] for i in inputs[t['id']]}
                              and 0 <= tr['ts'] - t['created_at'] < 60]
                legacy = (legacy_route and len(candidates) == 1
                          and tr['trigger'] in {text(e)[:500] for e in source_rows}
                          and 0 <= tr['ts'] - turn['created_at'] < 60)
                if (exact or legacy) and tr['id'] not in associated:
                    matched.append(tr)
                    associated.add(tr['id'])
            action_lines = []
            for tr in sorted(matched, key=lambda r: r['ts']):
                for index, call in enumerate(unpack(tr['tool_calls'], []) or [], 1):
                    single = dict(tr, tool_calls=json.dumps([call]))
                    action_lines.append((float(call.get('started_at') or call.get('ts') or tr['ts']),
                                         '\n'.join(tool_lines(single, index))))
                for thought in unpack(tr['thoughts'], []) or []:
                    inner.append(block(f"[{stamp(thought.get('ts', tr['ts']))}] {turn['conversation_id']} | 行动说明：", thought.get('text')))
                if tr['lesson']:
                    inner.append(block(f"[{stamp(tr['ts'])}] {turn['conversation_id']} | 经验：", tr['lesson']))
            outgoing = []
            for delivery in sorted((r for r in deliveries if r['turn_id'] == turn['id']), key=lambda r: r['segment']):
                if not live(unpack(delivery['sources'], [])):
                    continue
                body = unpack(delivery['payload'], {}) or {}
                message = (body.get('params') or {}).get('message', '')
                if isinstance(message, list):
                    message = '\n'.join('附件：' + str(p.get('data', {}).get('name') or p.get('data', {}).get('file') or p.get('type'))
                                        if p.get('type') != 'text' else str(p.get('data', {}).get('text', ''))
                                        for p in message if isinstance(p, dict))
                label = '已发送' if delivery['status'] == 'confirmed' else '拟发送/' + delivery['status']
                outgoing.append((float(delivery['created']), block('  ' + label + '：', '日志片段（不重复嵌入日志正文）' if inspecting_log else message)))
            # Local console has confirmed events without OneBot delivery rows.
            for e in sorted(visible.values(), key=lambda r: r['ingest_seq']):
                body = unpack(e['payload'], {}) or {}
                if (e['kind'] == 'delivered_segment' and not body.get('delivery_id')
                        and set(unpack(e['source_event_ids'], [])) & set(sources)):
                    outgoing.append((float(e['received_at']), block('  已发送/本地：', '日志片段（不重复嵌入日志正文）' if inspecting_log else text(e))))
            activity.extend(line for _, line in sorted(action_lines + outgoing, key=lambda pair: pair[0]))
            activity.append('')
        for tr in reversed(traces):
            if tr['id'] in associated:
                continue
            # Do not republish unlinked historical private text after deletion.
            activity.append(f"[{stamp(tr['ts'])}] 未关联轨迹 {tr['id']} | 来源轮次不明确，正文不展开")
        for job in reversed(jobs):
            if job['kind'] in {'respond', 'project'} or (job['root_id'] in events and job['root_id'] not in visible):
                continue
            activity.append(f"[{stamp(job['updated'])}] 后台 {job['kind']} | {job['id']} | {STATUS.get(job['status'], job['status'])} | {job['error'] or '无错误记录'}")
        inner.append('当前已保存的动机与学习计划：')
        for row in reversed(wants):
            body = unpack(row['payload'], {}) or {}
            if live(body.get('sources', [])):
                inner.append(block(f"  {row['scope_id']} | 动机 {row['id']} | {row['status']} | ", body.get('goal')))
                inner.append('    来源：' + ', '.join(body['sources']))
        for row in reversed(studies):
            if live(unpack(row['why_evidence_ids'], [])):
                inner.append(block(f"  {row['conversation_id']} | 学习 {row['id']} | {row['status']} | ", row['topic']))
                if row['next_step']:
                    inner.append(block('    下一步：', row['next_step']))
        inner.append('')
        for row in reversed(journals):
            if row['event_id'] not in visible:
                continue
            before, after = unpack(row['before_state'], {}), unpack(row['after_state'], {})
            appraisal = unpack(row['appraisal'], {}) or {}
            inner.append(f"[{stamp(row['at'])}] {row['scope_id']} | 来源 {row['event_id']}")
            inner.append(block('  触发：', text(visible[row['event_id']])))
            inner.append(block('  评价：', appraisal.get('interpretation') or
                               f"规则评价={appraisal.get('event_type', '未记录')}；相关度={appraisal.get('relevance', '?')}；确定度={appraisal.get('certainty', '?')}"))
            for key, labels in [('affect', EMOTIONS), ('mood', {'pleasure': '愉悦', 'activation': '唤醒', 'energy': '精力'})]:
                b = (before.get(key) or [None, {}])[1]
                a = (after.get(key) or [None, {}])[1]
                bv, av = (b.get('intensity', {}) if key == 'affect' else b), (a.get('intensity', {}) if key == 'affect' else a)
                changes = [f"{label} {float(bv.get(k, 0)):.3f}→{float(av[k]):.3f}" for k, label in labels.items()
                           if k in av and (k not in bv or abs(float(av[k]) - float(bv[k])) >= .0005)]
                inner.append('  ' + ('情绪' if key == 'affect' else '心境') + '：' + ('；'.join(changes) or '无显著变化'))
            inner.append('')
        for row in reversed(settlements):
            if row['revoked_at'] is not None or not live(unpack(row['source_ids'], [])):
                continue
            inner.append(f"[{stamp(row['created_at'])}] {row['scope_id']} | 驱力结算 {row['id']} | "
                         f"{DRIVE_LABELS.get(row['dimension'], row['dimension'])} | {row['effect_kind']} | "
                         f"{row['before_value']:.3f}→{row['after_value']:.3f} | 来源 {row['root_id']}")
        for row in reversed(thoughts):
            source_ids = unpack(row.get('source_event_ids'), [])
            if source_ids and not all(i in visible for i in source_ids):
                continue
            session_id = row.get('session_id') or ''
            associated_turn = session_id in associated or any(
                t['id'] == session_id and live(inputs.get(t['id'], [])) for t in turns)
            if associated_turn or not session_id:
                label = ("明确记录的想法" if session_id else "空闲念头（推想，非事实）")
                inner.append(block(f"[{stamp(row['ts'])}] {label}/{row['kind']}：", row['text']))
        if cognition:
            state = unpack(cognition['v'], {})
            inner.append(f"[{stamp(state.get('at', 0))}] 空闲念想调度：{state.get('reason', '')}")
        inner.extend(['', '当前已保存的驱力状态（读取日志不会推进状态或制造满足）：'])
        found = False
        for row in snapshots:
            if not row['aggregate_id'].startswith('drive:'):
                continue
            found = True
            inner.append(f"  {row['aggregate_id'][6:]} | 保存于 {stamp(row['updated_at'])}")
            drives = (unpack(row['payload'], {}) or {}).get('drives', {})
            for dim, state in drives.items():
                values = ' '.join(f"{label}={float(state.get(k, 0)):.3f}" for k, label in
                                  [('tension', '张力'), ('activation', '激活'), ('satiety', '满足冷却')])
                inner.append(f"    {DRIVE_LABELS.get(dim, dim)}：{values}")
        if not found:
            inner.append('  尚无持久化驱力快照，不从聊天文字猜测当前值。')
        return '\n'.join(activity) + '\n', '\n'.join(inner) + '\n'

    def refresh(self):
        with WRITE_LOCK:
            self._refresh()

    def _refresh(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, content in zip(('activity.log', 'inner.log'), self.render()):
            path = self.directory / name
            if path.exists() and path.read_text('utf-8') == content:
                continue
            temporary = path.with_suffix(f'.log.{os.getpid()}.{threading.get_ident()}.tmp')
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(path)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='从 life.db 生成活动与内在状态日志（只读）')
    parser.add_argument('--database', default='data/life.db')
    parser.add_argument('--directory', default='data')
    parser.add_argument('--limit', type=int, default=300, help='每类记录上限；0 为全部')
    args = parser.parse_args()
    ReadableLogs(args.database, args.directory, args.limit).refresh()


if __name__ == '__main__':
    main()
