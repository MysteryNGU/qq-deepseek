"""Request UI and rule APIs backed by one request table and runtime grants."""
import json
import time


class AuthorizationStore:
    def add_approval(self, peer, action, risk, reason):
        return self.add_consent_request(peer, reason, reason, action=action, risk=risk,
                                       conversation_id=f'qq:private:{peer}')

    def get_approval(self, aid):
        row = self.get_consent_request(aid)
        return dict(row, reason=row['why']) if row else None

    def decide_approval(self, aid, approved):
        self.decide_consent_request(aid, approved)

    def pending_approvals(self):
        return [dict(row, reason=row['why']) for row in self.pending_consent_requests()]

    def expire_approvals(self, older_than=900):
        return self.expire_consent_requests(older_than)

    def add_consent_rule(self, owner_id, scope, action_pattern, level='ask', conditions=None,
                         expires_at=0, created_by='owner'):
        conversation = (conditions or {}).get('conversation_id') or f'qq:private:{owner_id}'
        cond = conditions or {}
        descriptor = {'service_rule': True, 'owner_id': owner_id, 'scope': scope,
            'action_pattern': action_pattern, 'level': level, 'conditions': cond,
            'created_by': created_by}
        with self.repo.tx():
            number = self.repo._one("SELECT COALESCE(MAX(CAST(id AS INTEGER)),0)+1 AS n FROM grants")['n']
            scope_id = str(getattr(self,'agent_id','default')) + '|' + conversation
            task = self.repo._one('SELECT v FROM meta WHERE k=?', ('action_task:' + cond.get('task_id', ''),))
            if task:
                scope_id = json.loads(task['v'])['scope_id']
            epoch = self.repo._one('SELECT epoch FROM policies WHERE scope_id=?', (scope_id,))
            self.repo._exec('INSERT INTO grants VALUES(?,?,?,?,?,?,?,0)',
                (str(number), scope_id, cond.get('task_id') or '*',
                 json.dumps(cond.get('effects') or []), json.dumps(descriptor, ensure_ascii=False),
                 int(epoch['epoch']) if epoch else 0, expires_at or 32503680000))
            return int(number)

    def list_consent_rules(self, owner_id=0, active_only=True):
        result = []
        for row in self.repo._all('SELECT * FROM grants ORDER BY rowid'):
            payload = json.loads(row['resources'])
            if not isinstance(payload, dict) or not payload.get('service_rule'):
                continue
            if owner_id and payload['owner_id'] != owner_id:
                continue
            if active_only and (row['revoked'] or row['expires'] <= time.time()):
                continue
            epoch = self.repo._one('SELECT epoch FROM policies WHERE scope_id=?', (row['scope_id'],))
            if active_only and row['epoch'] != (epoch['epoch'] if epoch else 0):
                continue
            result.append(dict(payload, id=int(row['id']), expires_at=row['expires'],
                               active=not row['revoked'], created_ts=0))
        return result

    def revoke_consent_rule(self, rid):
        with self.repo.tx():
            self.repo._exec('UPDATE grants SET revoked=1 WHERE id=?', (str(rid),))
