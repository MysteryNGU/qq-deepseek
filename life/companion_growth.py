# -*- coding: utf-8 -*-
"""她自己的、可以改的认识：兴趣、表达习惯、自我理解、性格候选。

三条硬规矩，来自设计稿第 5、7 节：
1. 每条都带依据（真实事件或行动回执的 id），没有依据的只是猜测，不进这张表；
2. 每条都带适用范围。私聊里形成的东西默认只对这段关系生效，不自动升级成
   共享人格——那需要主人显式设定人格，不是模型自己说了算；
3. 性格候选只记不应用。跨天、跨独立经历的门槛由宿主算，模型自己说"我变了"
   不算数；真正改人格仍然走 SelfChange 那条带基线和回滚的路。
"""
from __future__ import annotations

import json
import time
import uuid

KINDS = ('interest', 'expression_habit', 'self_understanding', 'trait_candidate')
STATUSES = ('proposed', 'trying', 'established', 'dormant', 'rejected', 'superseded')
# 首次记录就能进入的状态；established 需要额外证据门槛。
OPEN_STATUS = {'interest': 'trying', 'expression_habit': 'trying',
               'self_understanding': 'trying', 'trait_candidate': 'proposed'}
# 这几类说法是身份、权限或对外承诺，不该由学习路径改写。
FORBIDDEN = ('密码', '密钥', 'api key', 'apikey', '权限', '授权给', '永远', '保证')

SCHEMA = '''CREATE TABLE IF NOT EXISTS companion_growth(
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
 topic TEXT NOT NULL, claim TEXT NOT NULL, status TEXT NOT NULL,
 evidence_refs TEXT NOT NULL DEFAULT '[]', counter_refs TEXT NOT NULL DEFAULT '[]',
 applicability TEXT NOT NULL DEFAULT 'global', apply_actor TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL, review_after REAL NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '');'''
INDEX = 'CREATE INDEX IF NOT EXISTS ix_companion_growth ON companion_growth(agent_id,kind,status);'
COLUMNS = ('id,agent_id,kind,topic,claim,status,evidence_refs,counter_refs,applicability,'
           'apply_actor,created_at,updated_at,review_after,revision,note')
# 谁在什么时候改成了什么：撤回和查依据都靠它，不靠记忆。
SCHEMA_LOG = '''CREATE TABLE IF NOT EXISTS companion_growth_log(
 id TEXT PRIMARY KEY, growth_id TEXT NOT NULL, agent_id TEXT NOT NULL, action TEXT NOT NULL,
 at REAL NOT NULL, actor TEXT NOT NULL DEFAULT '', before TEXT NOT NULL DEFAULT '',
 after TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '');'''
# 候选评审只留结论，不改人格；同一候选每次评审一行，便于看它是不是一直被按住。
SCHEMA_REVIEWS = '''CREATE TABLE IF NOT EXISTS companion_growth_reviews(
 id TEXT PRIMARY KEY, growth_id TEXT NOT NULL, agent_id TEXT NOT NULL, at REAL NOT NULL,
 verdict TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]', evidence_count INTEGER NOT NULL DEFAULT 0,
 span_days REAL NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '');'''


class GrowthError(ValueError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def _ago(value, now):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    delta = max(0.0, float(now) - float(value))
    if delta < 90:
        return '刚刚'
    if delta < 3600:
        return '%d分钟前' % int(delta // 60)
    if delta < 86400:
        return '%d小时前' % int(delta // 3600)
    return '%d天前' % int(delta // 86400)


class Growth:
    """只读写 companion_growth；不碰人格文件，也不新建数据库。"""

    def __init__(self, repo, agent_id, scope=None, catalog=None):
        self.repo, self.agent_id = repo, agent_id
        self.scope, self.catalog = scope, catalog
        repo.db.executescript(SCHEMA + '\n' + INDEX + '\n' + SCHEMA_LOG + '\n' + SCHEMA_REVIEWS)

    # --- 变更账本 ---
    @staticmethod
    def _snapshot(row):
        row = row if isinstance(row, dict) else dict(row)
        return json.dumps({key: row.get(key) for key in ('kind', 'topic', 'claim', 'status', 'evidence_refs',
                                                         'counter_refs', 'applicability', 'revision')},
                          ensure_ascii=False, sort_keys=True)

    def _log(self, identity, action, *, before=None, after=None, reason='', actor=''):
        """每条改动都留一行；撤回时按 before 恢复，不靠模型回忆改过什么。"""
        try:
            self.repo._exec('INSERT INTO companion_growth_log VALUES(?,?,?,?,?,?,?,?,?)',
                            ('growthlog_' + uuid.uuid4().hex, identity, self.agent_id, action, time.time(),
                             str(actor or ''), before or '', after or '', str(reason or '')[:300]))
        except Exception:
            # 账本写失败不能挡住成长记录本身，但要在审计里能看见（由调用方记）。
            pass

    def get(self, identity):
        row = self.repo._one('SELECT * FROM companion_growth WHERE id=? AND agent_id=?',
                             (identity, self.agent_id))
        return self.view(row) if row else None

    def ledger_since(self):
        row = self.repo._one('SELECT MIN(at) AS first FROM companion_growth_log WHERE agent_id=?',
                             (self.agent_id,))
        return (row['first'] if row else None)

    def changes(self, identity=None, limit=8):
        sql = ('SELECT * FROM companion_growth_log WHERE agent_id=?'
               + (' AND growth_id=?' if identity else '') + ' ORDER BY at DESC LIMIT ?')
        args = [self.agent_id] + ([identity] if identity else []) + [max(1, min(int(limit or 8), 40))]
        rows = self.repo._all(sql, tuple(args))
        topics = {}
        if rows:
            ids = list({row['growth_id'] for row in rows})
            for row in self.repo._all('SELECT id,topic,claim FROM companion_growth WHERE agent_id=? AND id IN (%s)'
                                      % ','.join('?' * len(ids)), tuple([self.agent_id] + ids)):
                topics[row['id']] = (row['topic'], row['claim'])
        out = []
        for row in rows:
            topic, claim = topics.get(row['growth_id'], ('', ''))
            out.append(dict(id=row['id'], growth_id=row['growth_id'], action=row['action'],
                            topic=topic, claim=(claim or '')[:80], at_ago=_ago(row['at'], time.time()),
                            actor=row['actor'], reason=row['reason'],
                            before=json.loads(row['before'] or 'null'), after=json.loads(row['after'] or 'null')))
        return out

    def restore(self, identity, *, to=None, scope=None):
        """退回某个历史版本：默认退到最近一次改动之前。"""
        row = self.repo._one('SELECT * FROM companion_growth WHERE id=? AND agent_id=?', (identity, self.agent_id))
        if not row:
            raise GrowthError('没有这条记录：' + str(identity))
        if to:
            entry = self.repo._one('SELECT * FROM companion_growth_log WHERE id=? AND agent_id=? '
                                   'AND growth_id=?', (to, self.agent_id, identity))
        else:
            entry = self.repo._one('SELECT * FROM companion_growth_log WHERE agent_id=? AND growth_id=? '
                                   "AND before NOT IN ('','null') ORDER BY at DESC LIMIT 1", (self.agent_id, identity))
        if not entry or entry['before'] in ('', 'null'):
            raise GrowthError('这条还没有可退回的旧版本：只有记过或改过的记录才有历史。')
        snapshot = json.loads(entry['before'])
        now = time.time()
        with self.repo.tx():
            self.repo._exec('UPDATE companion_growth SET claim=?,status=?,evidence_refs=?,counter_refs=?,'
                            'applicability=?,updated_at=?,revision=revision+1,note=? WHERE id=?',
                            (snapshot.get('claim') or row['claim'], snapshot.get('status') or row['status'],
                             snapshot.get('evidence_refs') or row['evidence_refs'],
                             snapshot.get('counter_refs') or row['counter_refs'],
                             snapshot.get('applicability') or row['applicability'],
                             now, ('退回 %s 之前的版本' % (entry['at_ago'] if 'at_ago' in entry.keys() else
                                                          _ago(entry['at'], now)))[:400], identity))
            self._log(identity, 'restore', before=self._snapshot(row),
                      after=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                      reason='退回 ' + str(to or entry['id']))
        return self.view(self.repo._one('SELECT * FROM companion_growth WHERE id=?', (identity,)))

    # --- 依据 ---
    def _visible(self, identity, scope):
        """这条依据是不是真的、而且这段关系看得见。"""
        if not isinstance(identity, str) or not identity:
            return False
        scope = scope or self.scope
        row = self.repo._one('SELECT 1 FROM companion_actions WHERE id=? AND agent=?', (identity, self.agent_id))
        if row:
            return True
        if scope is None:
            return self.repo._one('SELECT 1 FROM events WHERE id=? AND agent_id=? AND redacted_at IS NULL',
                                  (identity, self.agent_id)) is not None
        return self.repo._one('SELECT 1 FROM events WHERE id=? AND agent_id=? AND conversation_id=? '
                              'AND audience_policy_id=? AND redacted_at IS NULL',
                              (identity, self.agent_id, scope.conversation_id, scope.audience_policy_id)) is not None

    def _clean_refs(self, refs, scope):
        if not isinstance(refs, (list, tuple)):
            raise GrowthError('evidence 要给字符串数组，里面放真实回执或事件的 id')
        kept, missing = [], []
        for identity in dict.fromkeys(str(x).strip() for x in refs):
            if not identity:
                continue
            if self._visible(identity, scope):
                kept.append(identity)
            else:
                missing.append(identity)
        if missing:
            raise GrowthError('这些依据在这里查不到或不属于这段关系：' + '、'.join(missing[:3]) +
                              '。用 memory op=search 找真实 id；没有依据就先别写。')
        return kept

    def _check_claim(self, topic, claim):
        topic = (topic or '').strip()
        claim = (claim or '').strip()
        if not topic:
            raise GrowthError('topic 要写清是什么方面的：钓鱼、发光生物、说话长短')
        if len(topic) > 40:
            raise GrowthError('topic 太长，压到 40 字以内')
        if not claim:
            raise GrowthError('claim 要写成一句具体的、有范围的判断，不写"以后永远这样"')
        if len(claim) > 220:
            raise GrowthError('claim 太长，压到 220 字以内')
        lowered = claim.lower()
        bad = [word for word in FORBIDDEN if word in lowered]
        if bad:
            raise GrowthError('这条说法涉及身份、权限或对外承诺（%s），不走成长记录；'
                              '要改人格请走主人设定路径。' % '、'.join(bad))
        return topic, claim

    @staticmethod
    def _default_applicability(kind, scope):
        """私聊里长出来的东西，默认只对这段关系生效。"""
        if kind == 'expression_habit' and scope is not None:
            return 'actor:%s' % (getattr(scope, 'participant_id', '') or '')
        if scope is not None and getattr(scope, 'participant_id', None):
            return 'actor:%s' % scope.participant_id
        return 'global'

    @staticmethod
    def _visible_to(row, scope):
        row = row if isinstance(row, dict) else dict(row)
        applicability = row.get('applicability') or 'global'
        if applicability == 'global':
            return True
        if applicability.startswith('actor:'):
            actor = applicability.split(':', 1)[1]
            return bool(scope) and str(getattr(scope, 'participant_id', '')) == actor
        if applicability.startswith('channel:'):
            channel = applicability.split(':', 1)[1]
            return bool(scope) and str(getattr(scope, 'source_kind', '')) == channel
        return False

    # --- 写 ---
    def remember(self, kind, topic, claim, evidence, *, applicability=None, counter=None,
                 scope=None, note=''):
        scope = scope or self.scope
        if kind not in KINDS:
            raise GrowthError('kind 只支持 ' + '/'.join(KINDS))
        topic, claim = self._check_claim(topic, claim)
        refs = self._clean_refs(evidence, scope)
        if not refs:
            raise GrowthError('一条依据都没有：先经历，再记录。用 memory 查真实回执 id。')
        counters = self._clean_refs(counter or [], scope)
        applicability = (applicability or self._default_applicability(kind, scope)).strip()
        now = time.time()
        prior = self.repo._one('SELECT * FROM companion_growth WHERE agent_id=? AND kind=? AND topic=? '
                               'AND status NOT IN (\'rejected\',\'superseded\')', (self.agent_id, kind, topic))
        with self.repo.tx():
            if prior and prior['claim'] == claim:
                merged = list(dict.fromkeys(json.loads(prior['evidence_refs'] or '[]') + refs))
                merged_counter = list(dict.fromkeys(json.loads(prior['counter_refs'] or '[]') + counters))
                self.repo._exec('UPDATE companion_growth SET evidence_refs=?,counter_refs=?,updated_at=?,'
                                'revision=revision+1,note=? WHERE id=?',
                                (json.dumps(merged), json.dumps(merged_counter), now, note[:400], prior['id']))
                identity = prior['id']
                self._log(identity, 'add_evidence', before=self._snapshot(prior),
                          after=json.dumps(dict(evidence_refs=merged, counter_refs=merged_counter), ensure_ascii=False))
            elif prior:
                # 同一主题换了说法：旧的说法退位，不并存两个权威版本。
                self.repo._exec("UPDATE companion_growth SET status='superseded',updated_at=? WHERE id=?",
                                (now, prior['id']))
                self._log(prior['id'], 'supersede', before=self._snapshot(prior),
                          after=json.dumps(dict(status='superseded')), reason='同一主题换成新说法')
                identity = self._insert(kind, topic, claim, refs, counters, applicability, scope, now, note)
            else:
                identity = self._insert(kind, topic, claim, refs, counters, applicability, scope, now, note)
        if not prior or prior['claim'] != claim:
            self._log(identity, 'create', after=json.dumps(self.view(
                self.repo._one('SELECT * FROM companion_growth WHERE id=?', (identity,))), ensure_ascii=False))
        return self.view(self.repo._one('SELECT * FROM companion_growth WHERE id=?', (identity,)))

    def _insert(self, kind, topic, claim, refs, counters, applicability, scope, now, note):
        identity = 'growth_' + uuid.uuid4().hex
        actor = scope.participant_id if (scope is not None and applicability.startswith('actor:')) else ''
        self.repo._exec('INSERT INTO companion_growth(' + COLUMNS + ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (identity, self.agent_id, kind, topic, claim, OPEN_STATUS[kind],
                         json.dumps(refs), json.dumps(counters), applicability, str(actor or ''),
                         now, now, 0.0, 1, note[:400]))
        return identity

    def revise(self, identity, *, claim=None, status=None, note='', scope=None):
        row = self.repo._one('SELECT * FROM companion_growth WHERE id=? AND agent_id=?', (identity, self.agent_id))
        if not row:
            raise GrowthError('没有这条记录：' + str(identity))
        if status is not None and status not in STATUSES:
            raise GrowthError('status 只支持 ' + '/'.join(STATUSES))
        if status == 'established' and not self.eligible(row):
            raise GrowthError('还不够格说"已经形成"：至少要 3 条独立依据、跨 7 天。'
                              '保持 trying，等真实经历再多一点。')
        updates, args = [], []
        if claim is not None:
            _, cleaned = self._check_claim(row['topic'], claim)
            if cleaned != row['claim']:
                updates.append('claim=?')
                args.append(cleaned)
        if status is not None and status != row['status']:
            updates.append('status=?')
            args.append(status)
        if note:
            updates.append('note=?')
            args.append(note[:400])
        if not updates:
            return self.view(row)
        updates.extend(['updated_at=?', 'revision=revision+1'])
        args.extend([time.time(), identity])
        with self.repo.tx():
            self.repo._exec('UPDATE companion_growth SET ' + ','.join(updates) + ' WHERE id=?', tuple(args))
            self._log(identity, 'revise', before=self._snapshot(row),
                      after=json.dumps(dict(claim=claim, status=status or row['status'], note=note),
                                       ensure_ascii=False), reason=note)
        return self.view(self.repo._one('SELECT * FROM companion_growth WHERE id=?', (identity,)))

    def drop(self, identity, reason=''):
        row = self.repo._one('SELECT * FROM companion_growth WHERE id=? AND agent_id=?', (identity, self.agent_id))
        if not row:
            raise GrowthError('没有这条记录：' + str(identity))
        with self.repo.tx():
            self.repo._exec("UPDATE companion_growth SET status='dormant',updated_at=?,revision=revision+1,note=? "
                            'WHERE id=?', (time.time(), ('放下的原因：' + str(reason))[:400], identity))
            self._log(identity, 'drop', before=self._snapshot(row),
                      after=json.dumps(dict(status='dormant'), ensure_ascii=False), reason=reason)
        return self.view(self.repo._one('SELECT * FROM companion_growth WHERE id=?', (identity,)))

    # --- 门槛与读 ---
    def _independent(self, row):
        refs = json.loads(row['evidence_refs'] or '[]')
        days = None
        if row['created_at']:
            days = (time.time() - float(row['created_at'])) / 86400.0
        return dict(count=len(refs), days=round(days, 1) if days is not None else None)

    def eligible(self, row):
        """跨 7 天、3 个独立依据才算够格评审；这是工程门槛，不是心理学定律。"""
        facts = self._independent(row)
        return facts['count'] >= 3 and (facts['days'] or 0) >= 7

    # --- 依据资格：来源被撤销、删除或过期后，这条认识就不该再拿出来用 ---
    def _ref_events(self, ref):
        """动作/交办 id 换成事件 id；认不出来返回 None。"""
        if not isinstance(ref, str) or not ref:
            return None
        row = self.repo._one('SELECT source,result,status FROM companion_actions WHERE id=? AND agent=?',
                             (ref, self.agent_id))
        if row:
            if row['status'] in ('pending', 'running'):
                return []
            return [row['source']] if row['source'] else None
        if self.repo._one('SELECT 1 FROM companion_town_delegations WHERE id=?', (ref,)):
            return None
        return [ref]

    def _ref_ok(self, ref):
        if not self._visible(ref, self.scope):
            return False
        events = self._ref_events(ref)
        if not events or self.catalog is None:
            return True
        from .learning_validity import qualification
        try:
            state = qualification(self.catalog, events)
        except Exception:
            return True
        return bool(state.get('eligible'))

    def _evidence_ok(self, row):
        refs = json.loads(row['evidence_refs'] or '[]')
        return bool(refs) and all(self._ref_ok(ref) for ref in refs)

    def refresh(self, now=None, force=False):
        """定期的依据体检：失效的依据剔掉，全失效就转 dormant，不再进上下文。"""
        now = time.time() if now is None else now
        key = 'companion:growth:refreshed'
        row = self.repo._one('SELECT v FROM meta WHERE k=?', (key,))
        if row and not force and now - float(row['v'] or 0) < 6 * 3600:
            return dict(skipped=True, checked=0)
        changed = []
        for item in self.repo._all("SELECT * FROM companion_growth WHERE agent_id=? "
                                   "AND status IN ('trying','established','proposed')", (self.agent_id,)):
            refs = json.loads(item['evidence_refs'] or '[]')
            good = [ref for ref in refs if self._ref_ok(ref)]
            if len(good) == len(refs):
                continue
            if not good:
                self.repo._exec("UPDATE companion_growth SET status='dormant',updated_at=?,revision=revision+1,"
                                'note=? WHERE id=?', (now, '依据已失效或不复可见，先不用这条。', item['id']))
                changed.append(dict(id=item['id'], action='dormant'))
            else:
                self.repo._exec('UPDATE companion_growth SET evidence_refs=?,updated_at=?,revision=revision+1 '
                                'WHERE id=?', (json.dumps(good), now, item['id']))
                changed.append(dict(id=item['id'], action='drop_refs', removed=len(refs) - len(good)))
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, now))
        return dict(skipped=False, checked=len(changed), changed=changed)

    # --- 性格候选评审：只出结论，不改人格 ---
    def review(self, now=None):
        now = time.time() if now is None else now
        rows = self.repo._all("SELECT * FROM companion_growth WHERE agent_id=? AND kind='trait_candidate' "
                              "AND status IN ('proposed','trying') ORDER BY updated_at", (self.agent_id,))
        results = []
        for row in rows:
            facts = self._independent(row)
            counters = json.loads(row['counter_refs'] or '[]')
            reasons = []
            if facts['count'] < 3:
                reasons.append('evidence_lt_3')
            if (facts['days'] or 0) < 7:
                reasons.append('span_lt_7d')
            if not self._evidence_ok(row):
                reasons.append('evidence_not_current')
            if counters:
                reasons.append('counter_evidence_present')
            verdict = 'would_apply' if not reasons else 'hold'
            self._record_review(row, now, verdict, reasons, facts)
            results.append(dict(id=row['id'], topic=row['topic'], verdict=verdict, reasons=reasons,
                                evidence=facts['count'], days=facts['days']))
        return dict(reviewed=len(results), results=results)

    def _record_review(self, row, now, verdict, reasons, facts):
        try:
            self.repo._exec('INSERT INTO companion_growth_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                            ('growthreview_' + uuid.uuid4().hex, row['id'], self.agent_id, now, verdict,
                             json.dumps(reasons), int(facts['count'] or 0), float(facts['days'] or 0), ''))
        except Exception:
            pass

    def review_log(self, identity=None, limit=6):
        sql = ('SELECT * FROM companion_growth_reviews WHERE agent_id=?'
               + (' AND growth_id=?' if identity else '') + ' ORDER BY at DESC LIMIT ?')
        args = [self.agent_id] + ([identity] if identity else []) + [max(1, min(int(limit or 6), 20))]
        return [dict(id=row['id'], growth_id=row['growth_id'], verdict=row['verdict'],
                     reasons=json.loads(row['reasons'] or '[]'), evidence_count=row['evidence_count'],
                     span_days=row['span_days'], at_ago=_ago(row['at'], time.time()))
                for row in self.repo._all(sql, tuple(args))]

    def for_actor(self, actor, limit=3):
        """对这个人有效的那几条，进当前对话的状态，不外溢给其他人。"""
        actor = str(actor or '')
        if not actor:
            return []
        rows = self.repo._all("SELECT * FROM companion_growth WHERE agent_id=? AND applicability=? "
                              "AND status IN ('trying','established') ORDER BY updated_at DESC LIMIT ?",
                              (self.agent_id, 'actor:' + actor, max(1, min(int(limit or 3), 8))))
        return [self.view(row) for row in rows]

    def view(self, row):
        if not row:
            return None
        facts = self._independent(row)
        return dict(id=row['id'], kind=row['kind'], topic=row['topic'], claim=row['claim'],
                    status=row['status'], applicability=row['applicability'],
                    evidence_count=facts['count'], span_days=facts['days'],
                    eligible_for_review=self.eligible(row),
                    updated_ago=_ago(row['updated_at'], time.time()),
                    revision=row['revision'])

    def listing(self, *, kind=None, status=None, scope=None, limit=12):
        sql = ('SELECT * FROM companion_growth WHERE agent_id=?'
               + (' AND kind=?' if kind else '') + ' AND status IN (%s) ORDER BY updated_at DESC LIMIT ?')
        args = [self.agent_id] + ([kind] if kind else [])
        if status:
            states = [status]
        else:
            states = ['trying', 'established', 'proposed']
        args.extend(states + [max(1, min(int(limit or 12), 50))])
        rows = self.repo._all(sql % ','.join('?' * len(states)), tuple(args))
        visible = [row for row in rows if self._visible_to(row, scope)]
        return [self.view(row) for row in visible]

    def material(self, scope=None, clues=(), limit=3):
        """进上下文的那几条：只给相关的小结，不给整篇自述。"""
        rows = self.listing(scope=scope, limit=24)
        words = [str(word) for word in (clues or []) if isinstance(word, str) and len(word) >= 2]

        def score(item):
            hit = 0
            text = (item['topic'] or '') + (item['claim'] or '')
            hit = sum(1 for word in words if word in text)
            return (-hit, item['status'] != 'established', -item['evidence_count'])

        rows.sort(key=score)
        rows = [item for item in rows if self._evidence_ok(
            self.repo._one('SELECT * FROM companion_growth WHERE id=?', (item['id'],)))]
        return rows[:limit]
