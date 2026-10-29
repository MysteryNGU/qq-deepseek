"""Grounded voluntary contact, counted from delivery receipts, never draft text."""
import json
import re

from .agent_session import SessionFailure


def closing_text(text):
    # Deliberately narrow: a question, request or additional topic must pass.
    value = re.sub(r'[\s。！!，,～~.]', '', str(text)).lower()
    return value in {'晚安', '晚安啦', '晚安呀', '好梦', '拜拜', '再见', '回头见', 'goodnight', 'bye'}


class ContactPolicy:
    def __init__(self, role):
        self.role, self.life, self.repo = role, role.life, role.repo
        self.repo.db.execute('''CREATE TABLE IF NOT EXISTS contact_expressions(
            id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, forget_epoch INTEGER NOT NULL,
            proactive INTEGER NOT NULL, closing INTEGER NOT NULL, input_seq INTEGER NOT NULL,
            created REAL NOT NULL)''')
        self.repo.db.execute('CREATE INDEX IF NOT EXISTS contact_scope ON contact_expressions(scope_id,forget_epoch,input_seq)')

    def latest_input(self, scope):
        row = self.repo._one("SELECT * FROM events WHERE agent_id=? AND conversation_id=? "
            "AND kind='message' AND source_kind='user_report' AND redacted_at IS NULL "
            "ORDER BY ingest_seq DESC LIMIT 1", scope.private_key)
        return self.repo.get_event(row['id']) if row else None

    def confirmed(self, scope, *, exclude=''):
        return self.repo._all("SELECT c.*,MAX(d.updated) delivered_at FROM contact_expressions c "
            "JOIN deliveries d ON d.turn_id=c.id AND d.scope_id=c.scope_id "
            "WHERE c.scope_id=? AND c.forget_epoch=? AND c.id!=? AND d.status='confirmed' "
            "GROUP BY c.id ORDER BY delivered_at DESC,c.id DESC",
            (scope.scope_id, self.repo.forget_epoch(scope.scope_id), exclude))

    def state(self, scope, *, exclude=''):
        latest = self.latest_input(scope)
        seq = latest.ingest_seq if latest else 0
        rows = [r for r in self.confirmed(scope, exclude=exclude) if r['proactive'] and r['input_seq'] >= seq]
        count = len(rows)
        # At most three voluntary contacts without a new user message; 1h then 4h.
        next_at = rows[0]['delivered_at'] + (3600 if count == 1 else 14400) if rows else 0
        return dict(unanswered=count, paused=count >= 3, next_contact_at=next_at,
                    available=count < 3 and self.life.clock() >= next_at,
                    meaning='voluntary_contact_only', read_kind='contact_policy',
                    missing_you=self.missing_you(scope))

    def missing_you(self, scope):
        """Current connection needs can motivate contact without an external topic."""
        if not self.role.motivations.owner(scope):
            return []
        now = self.life.clock()
        drives = self.life.drive_view(scope, now)
        return [dict(kind='missing_you', dimension=dimension, cycle_id=row['cycle_id'])
                for dimension in ('relatedness', 'intimacy')
                if (row := drives[dimension])['pressure'] >= row['expression_threshold']
                and row['recovery_until'] <= now]

    def voluntary(self, scope, root):
        if not scope.conversation_id.startswith('qq:private:'):
            return False
        turn = self.role.turn_number(root['session_id'])
        rows = self.repo._all('SELECT e.* FROM agent_inputs i JOIN events e ON e.id=i.input_id '
            'WHERE i.session_id=? AND i.claimed_turn IS ?', (root['session_id'], turn))
        if not rows:
            return False
        # User requests, explicit reminders and tool results retain their own flow.
        for row in rows:
            payload = json.loads(row['payload'])
            plans = payload.get('wake_plans', [])
            kinds = [p.get('kind') for p in plans] if plans else [payload.get('wake_kind')]
            if (row['kind'] != 'runtime_wake'
                    or any(kind not in {'idle_probe', 'motivation', 'self_scheduled'} for kind in kinds)):
                return False
        # A current user-origin task is an explicit obligation, not a check-in.
        task = self.role.tasks.get(root['intent_ref'], scope.scope_id) if root.get('intent_ref') else None
        return not (task and task.get('origin') == 'user' and task.get('status') == 'active')

    def check(self, scope, proposal, root, source_ids, *, logical=''):
        if not scope.conversation_id.startswith('qq:private:'):
            return False
        latest = self.latest_input(scope)
        parts = proposal.get('parts')
        plain = proposal.get('text', '') if parts is None else ''.join(p.get('text', '') for p in parts if p.get('kind') == 'text')
        text_only = parts is None or all(p.get('kind') == 'text' for p in parts)
        previous = self.confirmed(scope, exclude=logical)
        if (text_only and closing_text(plain) and latest and closing_text(latest.text) and previous
                and previous[0]['closing'] and abs(latest.received_at - previous[0]['delivered_at']) < 1800
                and self.life.clock() - max(latest.received_at, previous[0]['delivered_at']) < 1800):
            raise SessionFailure('contact_closing_silence', '已经送达收尾，对方仅回应道别；请用 life_rest 安静结束，不再补一句')
        proactive = self.voluntary(scope, root)
        if not proactive:
            return False
        state = self.state(scope, exclude=logical)
        if not state['available']:
            raise SessionFailure('contact_backoff', '自发联系暂缓；继续自己的活动或 life_rest，不换话题追问', **state)
        basis = proposal.get('contact_basis') or {}
        if not isinstance(basis, dict):
            raise SessionFailure('contact_basis_required', '联系理由须是对象')
        if basis.get('kind') == 'missing_you':
            if (type(basis.get('cycle_id')) is not int
                    or not any(all(basis.get(key) == value for key, value in candidate.items())
                               for candidate in self.missing_you(scope))):
                raise SessionFailure('contact_desire_changed', '当前亲近意愿已变化；重读 contact_policy，不编造想念或借此追问')
            return True
        refs = basis.get('sources')
        if (basis.get('kind') not in {'followup', 'shared_experience', 'anniversary'}
                or not isinstance(refs, list) or not 1 <= len(refs) <= 4
                or any(not isinstance(ref, str) or ref not in source_ids for ref in refs)):
            raise SessionFailure('contact_basis_required', '自发联系须引用真实依据，或使用 contact_policy.missing_you 中当前的想念理由')
        ctx = self.life.life_access.bind(scope, session_id=root['session_id'], generation=root['generation'])
        for ref in refs:
            resource, _ = self.life.life_access.load(ctx, ref)
            event = self.repo.get_event(resource['native_id']) if resource['kind'] == 'event' else None
            if not event or event.source_kind not in {'user_report', 'tool_observation', 'self_output'}:
                raise SessionFailure('contact_basis_required', '念头、唤醒与计划不能冒充已发生的联系依据')
            if event.source_kind == 'self_output' and not self.repo._one(
                    "SELECT 1 FROM deliveries WHERE id=? AND scope_id=? AND status='confirmed'",
                    (event.payload.get('delivery_id'), scope.scope_id)):
                raise SessionFailure('contact_basis_required', '共同经历中的自身表达须有实际送达回执')
        return True

    def register(self, scope, logical, proposal, proactive):
        latest = self.latest_input(scope)
        parts = proposal.get('parts')
        text_only = parts is None or all(p.get('kind') == 'text' for p in parts)
        plain = proposal.get('text', '') if parts is None else ''.join(p.get('text', '') for p in parts if p.get('kind') == 'text')
        # No duplicate text store. This marker alone never increments the counter.
        self.repo._exec('INSERT OR IGNORE INTO contact_expressions VALUES(?,?,?,?,?,?,?)',
            (logical, scope.scope_id, self.repo.forget_epoch(scope.scope_id), int(proactive),
             int(text_only and closing_text(plain)), latest.ingest_seq if latest else 0,
             self.life.clock()))
        basis = proposal.get('contact_basis') or {}
        if proactive and basis.get('kind') == 'missing_you':
            self.repo._exec('INSERT OR IGNORE INTO meta VALUES(?,?)',
                ('contact_motive:' + logical, json.dumps(dict(dimension=basis['dimension'], cycle_id=basis['cycle_id']))))

    def delivered(self, scope, logical):
        """Settle the frozen motive only after actual delivery, including RPC replay."""
        marker = self.repo._one('SELECT * FROM contact_expressions WHERE id=? AND scope_id=? AND forget_epoch=?',
            (logical, scope.scope_id, self.repo.forget_epoch(scope.scope_id)))
        motive = self.repo._one('SELECT v FROM meta WHERE k=?', ('contact_motive:' + logical,))
        if not marker or not motive:
            return
        receipts = self.repo._all('SELECT status FROM deliveries WHERE scope_id=? AND turn_id=?', (scope.scope_id, logical))
        if not receipts or any(r['status'] != 'confirmed' for r in receipts):
            return
        value = json.loads(motive['v'])
        # Sent is not satisfied: only later real interaction can settle the need.
        self.repo._exec("UPDATE motivation_decisions SET status='expressed',reason='contact_delivered',"
            "revision=revision+1,updated=?,next_due=? WHERE scope_id=? AND forget_epoch=? "
            "AND dimension=? AND cycle_id=? AND status='pending' AND updated<=?",
            (self.life.clock(), self.life.clock()+self.role.resources.motivation_cooldown_seconds,
             scope.scope_id, self.repo.forget_epoch(scope.scope_id), value['dimension'], value['cycle_id'], marker['created']))
