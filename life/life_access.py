"""Host-issued read bindings. Tool parameters never select a bot or a purpose."""
from __future__ import annotations

from dataclasses import dataclass, field
import json


class RecallError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReadContext:
    agent_id: str
    mode: str
    scope_id: str
    audience_key: str
    purpose: str
    policy_revision: int
    membership_revision: str
    session_id: str
    generation: int
    _seal: object = field(repr=False, compare=False)


class LifeAccess:
    def __init__(self, catalog, binding_current=None):
        self.catalog = catalog
        self._seal = object()
        self.binding_current = binding_current
        self.same_person_group_recall = False

    def same_person_group_event(self,ctx,resource):
        """A person's own group statement may follow them into their own DM.

        This never grants group-wide membership or exports private statements.
        Explicit denies and lineage checks still run in allowed/load.
        """
        prefix=ctx.agent_id+'|qq:private:'
        if not self.same_person_group_recall or ctx.mode!='audience' or not ctx.scope_id.startswith(prefix) or resource['kind']!='event':return False
        actor=ctx.scope_id[len(prefix):]
        if not actor.isascii() or not actor.isdigit():return False
        event=self.catalog.repo.get_event(resource['native_id'])
        if not event or not event.scope.conversation_id.startswith('qq:group:'):return False
        from .contracts import Scope
        from .memory_policy import reset_boundary
        floor=max(reset_boundary(self.catalog.repo,event.scope),reset_boundary(self.catalog.repo,Scope(ctx.agent_id,'qq:private:'+actor,actor)))
        if event.received_at<floor:return False
        if event.kind=='message' and event.source_kind=='user_report':return event.actor_id==actor
        if event.kind!='delivered_segment' or event.source_kind!='self_output' or not event.source_event_ids:return False
        delivery=self.catalog.repo._one("SELECT 1 FROM deliveries WHERE id=? AND scope_id=? AND status='confirmed'",(event.payload.get('delivery_id'),event.scope.scope_id))
        if not delivery or event.payload.get('confirmed') is not True or event.external_id!='delivery:'+str(event.payload.get('delivery_id')):return False
        parents=[self.catalog.repo.get_event(ref) for ref in event.source_event_ids]
        return all(p and not p.redacted_at and p.kind=='message' and p.source_kind=='user_report' and p.actor_id==actor and p.scope.scope_id==event.scope.scope_id for p in parents)

    def bind(self, scope, *, mode='audience', session_id='', generation=0,
             membership_revision='', inspect_authorized=False):
        if scope.agent_id != self.catalog.agent_id or mode not in {'audience', 'internal', 'diagnostic'}:
            raise RecallError('not_available')
        if mode == 'diagnostic' and not inspect_authorized:
            raise RecallError('not_available')
        purpose = {'internal': 'internal_use', 'audience': 'disclose', 'diagnostic': 'inspect'}[mode]
        audience = scope.scope_id + '|' + scope.audience_policy_id
        return ReadContext(scope.agent_id, mode, scope.scope_id, audience, purpose,
                           self.catalog.epoch('policy'), str(membership_revision),
                           session_id, generation, self._seal)

    def require_binding(self, ctx):
        if (not isinstance(ctx, ReadContext) or ctx._seal is not self._seal
                or ctx.agent_id != self.catalog.agent_id
                or ctx.policy_revision != self.catalog.epoch('policy')
                or self.binding_current and not self.binding_current(ctx)):
            raise RecallError('not_available')
        if ctx.session_id:
            row=self.catalog.repo._one('SELECT status,generation,scope_json FROM agent_sessions WHERE session_id=?',(ctx.session_id,))
            if (not row or row['status'] not in {'current','delegate'} or row['generation']!=ctx.generation
                    or json.loads(row['scope_json'])['agent_id']!=ctx.agent_id):
                raise RecallError('not_available')

    def allowed(self, ctx, resource, *, ancestry=False):
        self.require_binding(ctx)
        if not resource or resource['agent_id'] != ctx.agent_id:
            return False
        forbidden = self.catalog.repo._one("SELECT 1 FROM life_acl WHERE resource_id=? AND purpose='internal_use' "
            "AND effect='deny' AND audience_key='*' AND (expires_at IS NULL OR expires_at>?)",
            (resource['resource_id'],self.catalog.clock()))
        if forbidden:
            return False
        rules = self.catalog.repo._all(
            'SELECT effect FROM life_acl WHERE resource_id=? AND purpose=? '
            "AND audience_key IN (?, '*') AND (expires_at IS NULL OR expires_at>?)",
            (resource['resource_id'], ctx.purpose, ctx.audience_key, self.catalog.clock()))
        effects = {row['effect'] for row in rules}
        if 'deny' in effects:
            return False
        if ctx.mode == 'internal':
            return True
        if ctx.mode == 'diagnostic':
            return 'allow' in effects
        # A snippet may disclose its own checked text, never its ancestors.
        if resource['origin_scope'] == ctx.audience_key:
            return True
        if self.same_person_group_event(ctx,resource):
            return True
        return 'allow' in effects and not ancestry

    def denied(self, ctx, ref, visiting=None):
        """Policy gate for history reads with explicit historical semantics."""
        self.require_binding(ctx)
        resource = self.catalog.resolve(ref)
        if not resource:
            return True
        visiting = set(visiting or ())
        if resource['resource_id'] in visiting:
            return True
        visiting.add(resource['resource_id'])
        row = self.catalog.repo._one("SELECT 1 FROM life_acl WHERE resource_id=? AND effect='deny' "
            "AND purpose IN (?, 'internal_use') AND audience_key IN (?, '*') AND (expires_at IS NULL OR expires_at>?)",
            (resource['resource_id'],ctx.purpose,ctx.audience_key,self.catalog.clock()))
        return bool(row) or any(self.denied(ctx,e['parent_id'],visiting) for e in self.catalog.parents(resource['resource_id']))

    def internal_allowed(self, ctx, resource, visiting=None):
        """Check the internal lineage policy without loading any source body."""
        self.require_binding(ctx)
        if ctx.mode!='internal':
            raise RecallError('not_available')
        visiting=set(visiting or ())
        if resource['resource_id'] in visiting:
            raise RecallError('source_cycle')
        if not self.allowed(ctx,resource):
            return False
        for edge in self.catalog.parents(resource['resource_id']):
            parent=self.catalog.resolve(edge['parent_id'])
            if not parent or parent['source_version']!=edge['parent_version']:
                raise RecallError('stale_source')
            if not self.internal_allowed(ctx,parent,visiting|{resource['resource_id']}):
                return False
        return True

    def set_rule(self, resource_ref, purpose, audience_key, effect, *, expires_at=None):
        """Trusted administration/policy hook, deliberately absent from role tools."""
        resource = self.catalog.resolve(resource_ref)
        if (not resource or purpose not in {'internal_use', 'disclose', 'inspect'}
                or effect not in {'allow', 'deny'} or not audience_key):
            raise RecallError('not_available')
        with self.catalog.repo.tx():
            revision = self.catalog.bump('policy')
            self.catalog.repo._exec(
                'INSERT INTO life_acl VALUES(?,?,?,?,?,?) ON CONFLICT(resource_id,purpose,audience_key) '
                'DO UPDATE SET effect=excluded.effect,policy_revision=excluded.policy_revision,expires_at=excluded.expires_at',
                (resource['resource_id'], purpose, audience_key, effect, revision, expires_at))
            if effect=='deny':
                affected={resource['resource_id']}
                pending=list(affected)
                while pending:
                    for row in self.catalog.repo._all('SELECT child_id FROM life_source_edges WHERE parent_id=?',(pending.pop(),)):
                        if row['child_id'] not in affected:
                            affected.add(row['child_id'])
                            pending.append(row['child_id'])
                self.catalog._retire_readers(affected)

    def load(self, ctx, ref, expected_version=None, *, track=True, history=False):
        self.require_binding(ctx)
        resource = self.catalog.resolve(ref)
        if not resource or not self.allowed(ctx, resource):
            raise RecallError('not_available')
        if not self.catalog.live(resource,history=history):
            raise RecallError('not_available')
        if expected_version is not None and resource['source_version'] != expected_version:
            raise RecallError('stale_source')
        # Ordinary derivation inherits the intersection. Published spans retain
        # validity dependencies but do not grant access to the parent archive.
        if not self._dependencies(ctx, resource, set(),history=history):
            raise RecallError('not_available')
        if track and ctx.session_id:
            with self.catalog.repo.tx():
                key='life_read_session:'+ctx.agent_id+':'+ctx.session_id
                row=self.catalog.repo._one('SELECT v FROM meta WHERE k=?',(key,))
                refs=set(json.loads(row['v'])) if row else set()
                if resource['resource_id'] not in refs:
                    refs.add(resource['resource_id'])
                    self.catalog.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,json.dumps(sorted(refs))))
        return resource, self.catalog.body(resource) if history else self.catalog.current_body(resource)

    def _dependencies(self, ctx, resource, visiting, *, history=False):
        ref = resource['resource_id']
        if ref in visiting:
            return False
        visiting = visiting | {ref}
        for edge in self.catalog.parents(ref):
            parent = self.catalog.resolve(edge['parent_id'])
            if (not parent or parent['source_version'] != edge['parent_version']
                    or not self.catalog.live(parent,history=history)):
                return False
            if ctx.mode == 'internal':
                if not self.allowed(ctx, parent) or not self._dependencies(ctx, parent, visiting,history=history):
                    return False
            elif edge['relation'] != 'published_from':
                if not self.allowed(ctx, parent) or not self._dependencies(ctx, parent, visiting,history=history):
                    return False
            else:
                denies = self.catalog.repo._one("SELECT 1 FROM life_acl WHERE resource_id=? AND effect='deny' "
                    "AND purpose IN ('disclose','internal_use') AND audience_key IN (?, '*') AND (expires_at IS NULL OR expires_at>?)",
                    (parent['resource_id'],ctx.audience_key,self.catalog.clock()))
                if denies:
                    return False
        return True
