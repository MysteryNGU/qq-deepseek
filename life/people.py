"""Evidence-backed person profiles, with audience checks at every read."""
import json
import re

from .agent_session import SessionFailure,canonical,digest
from .life_access import RecallError
from .text import stable_id


class People:
    FIELDS={'address','preference','interest','boundary','fact'}

    def __init__(self,role):
        self.role,self.life,self.repo=role,role.life,role.repo
        self.repo.db.execute('''CREATE TABLE IF NOT EXISTS person_profiles(
            id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,actor_id TEXT NOT NULL,
            scope_id TEXT NOT NULL,field TEXT NOT NULL,topic TEXT NOT NULL,
            text TEXT NOT NULL,origin TEXT NOT NULL,sources TEXT NOT NULL,
            revision INTEGER NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,
            expires REAL NOT NULL,operation_hash TEXT NOT NULL)''')
        self.repo.db.execute('CREATE INDEX IF NOT EXISTS person_profiles_actor ON person_profiles(agent_id,actor_id,status)')
        self.repo.db.execute('''CREATE TABLE IF NOT EXISTS person_encounters(
            event_id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,actor_id TEXT NOT NULL,scope_id TEXT NOT NULL,
            state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',updated REAL NOT NULL)''')
        self.repo.db.execute('CREATE INDEX IF NOT EXISTS person_encounters_actor ON person_encounters(agent_id,actor_id,scope_id,state)')
        if getattr(role.cfg,'profile_review_enabled',False):
            # Register identities from real retained speakers, never invent traits.
            with self.repo.tx():
                self.repo._exec("INSERT OR IGNORE INTO person_encounters(event_id,agent_id,actor_id,scope_id,state,updated) "
                    "SELECT e.id,e.agent_id,e.actor_id,e.agent_id||'|'||e.conversation_id,'pending',e.received_at FROM events e "
                    "LEFT JOIN meta m ON m.k='memory_policy_floor:'||e.agent_id||'|'||e.conversation_id "
                    "WHERE e.agent_id=? AND e.kind='message' AND e.source_kind='user_report' AND e.redacted_at IS NULL "
                    "AND e.conversation_id LIKE 'qq:%' AND e.actor_id!='' AND e.actor_id NOT GLOB '*[^0-9]*' "
                    "AND e.received_at>=COALESCE(CAST(m.v AS REAL),0)",(self.life.agent_id,))

    def observe(self,event):
        if (not getattr(self.role.cfg,'profile_review_enabled',False) or event.kind!='message' or event.source_kind!='user_report'
                or not re.fullmatch(r'[0-9]+',event.actor_id) or not event.scope.conversation_id.startswith('qq:') or event.redacted_at):return
        with self.repo.tx():
            self.repo._exec("INSERT OR IGNORE INTO person_encounters(event_id,agent_id,actor_id,scope_id,state,updated) VALUES(?,?,?,?,'pending',?)",
                (event.id,event.scope.agent_id,event.actor_id,event.scope.scope_id,event.received_at))

    def identity(self,scope,actor):
        ctx=self.life.life_access.bind(scope)
        seen=False;reviewed=False
        cross=bool(getattr(self.role.cfg,'same_person_group_recall',False) and scope.conversation_id=='qq:private:'+str(actor))
        for row in self.repo._all("SELECT event_id,state FROM person_encounters WHERE agent_id=? AND actor_id=? "
                "AND (scope_id=? OR (? AND scope_id LIKE ?)) ORDER BY CASE state WHEN 'reviewed' THEN 0 ELSE 1 END,updated DESC",
                (scope.agent_id,str(actor),scope.scope_id,cross,scope.agent_id+'|qq:group:%')):
            try:self.life.life_access.load(ctx,row['event_id'],track=False)
            except RecallError:continue
            seen=True;reviewed=reviewed or row['state']=='reviewed'
            break
        return dict(actor_id=str(actor),recognized=seen,reviewed_evidence=reviewed,
            meaning='stable_traits_require_evidence',profile_read_kind='person_profile')

    def verify_sync(self,scope):
        actor=scope.conversation_id.removeprefix('qq:private:')
        ctx=self.life.life_access.bind(scope)
        for row in self.repo._all("SELECT id FROM events WHERE agent_id=? AND actor_id=? AND conversation_id LIKE 'qq:group:%' "
                "AND kind='message' AND source_kind='user_report' AND redacted_at IS NULL ORDER BY ingest_seq DESC LIMIT 40",(scope.agent_id,actor)):
            try:resource,_=self.life.life_access.load(ctx,row['id'],track=False)
            except RecallError:continue
            found=self.life.life_recall.search(ctx,'',mode='text',exact_refs=[resource['resource_id']])
            return dict(source_ref=resource['resource_id'],readable=True,recall_found=any(r['resource_ref']==resource['resource_id'] for r in found['items']),
                requires_hourly_summary=False)
        return dict(readable=None,reason='no_current_accessible_own_group_message')

    def due(self,scope,root):
        if not getattr(self.role.cfg,'profile_review_enabled',False):return []
        turn=self.role.turn_number(root['session_id'])
        rows=self.repo._all("SELECT p.event_id,p.actor_id FROM person_encounters p JOIN agent_inputs i ON i.input_id=p.event_id "
            "JOIN events e ON e.id=p.event_id WHERE p.scope_id=? AND p.state='pending' AND e.redacted_at IS NULL "
            "AND i.session_id=? AND (i.claimed_turn IS ? OR i.handled=0) ORDER BY i.ordinal LIMIT 24",(scope.scope_id,root['session_id'],turn))
        ctx=self.life.life_access.bind(scope,session_id=root['session_id'],generation=root['generation'])
        result=[]
        for row in rows:
            try:self.life.life_access.load(ctx,row['event_id'])
            except RecallError:continue
            result.append(dict(actor_id=row['actor_id'],source_ref=row['event_id']))
        return result

    def review(self,scope,p,root):
        actor=str(p.get('actor_id',''));reason=p.get('reason','')
        if not re.fullmatch(r'[0-9]+',actor) or not isinstance(reason,str) or not reason.strip() or len(reason)>240:
            raise SessionFailure('invalid_profile_review','画像复核需要 actor_id 和具体无新增原因，不能编造人物特征')
        if not isinstance(p.get('sources'),list) or not 1<=len(p['sources'])<=24:
            raise SessionFailure('invalid_profile_review','画像复核须明确列出 1～24 条本人消息来源')
        sources=self.role._sources(scope,p,root)
        ctx=self.life.life_access.bind(scope,session_id=root['session_id'],generation=root['generation'])
        for source in sources:
            event=self.repo.get_event(source)
            if not event or event.actor_id!=actor or event.kind!='message' or event.source_kind!='user_report':
                raise SessionFailure('actor_mismatch','只能复核该发言人自己的实际消息')
            self.life.life_access.load(ctx,source)
        with self.repo.tx():
            for source in sources:
                self.observe(self.repo.get_event(source))
                self.repo._exec("UPDATE person_encounters SET state='reviewed',reason=?,updated=? WHERE event_id=?",(reason,self.life.clock(),source))
        return dict(actor_id=actor,reviewed=True,source_refs=sources,profile_change='none')

    def finish(self,scope,root,p):
        if not getattr(self.role.cfg,'profile_review_enabled',False):return
        proposals=p.get('profile_reviews',[])
        if not isinstance(proposals,list) or len(proposals)>24 or any(not isinstance(v,dict) for v in proposals):
            raise SessionFailure('invalid_profile_review','profile_reviews 最多 24 项')
        with self.repo.tx():
            for proposal in proposals:self.review(scope,proposal,root)
            pending=self.due(scope,root)
            if pending:raise SessionFailure('profile_review_required','请逐人保存有依据的画像更新，或用 profile_reviews 记录无新增判断后结束；无需为寒暄编造偏好',pending=pending)

    def recent_group(self,scope,root):
        if not getattr(self.role.cfg,'same_person_group_recall',False) or not scope.conversation_id.startswith('qq:private:'):return []
        actor=scope.conversation_id.removeprefix('qq:private:')
        ctx=self.life.life_access.bind(scope,session_id=root['session_id'],generation=root['generation'])
        rows=self.repo._all("SELECT id FROM events WHERE agent_id=? AND conversation_id LIKE 'qq:group:%' AND redacted_at IS NULL "
            "AND received_at>=? AND ((source_kind='user_report' AND actor_id=?) OR (kind='delivered_segment' AND source_kind='self_output')) "
            "ORDER BY ingest_seq DESC LIMIT 80",(scope.agent_id,self.life.clock()-86400,actor))
        result=[]
        for row in rows:
            try:resource,body=self.life.life_access.load(ctx,row['id'])
            except RecallError:continue
            event=self.repo.get_event(row['id'])
            result.append(dict(ref=resource['resource_id'],source_version=resource['source_version'],actor_id=event.actor_id,
                conversation_id=event.scope.conversation_id,occurred_at=event.occurred_at,source_kind=event.source_kind,
                text=event.text[:480],has_more=len(event.text)>480,read_kind='life_source'))
            if len(result)>=4:break
        return list(reversed(result))

    def group_material(self,scope,root,limit=12):
        """近期群聊原话；角色自己安排的群内一轮同样要先看见大家在说什么。"""
        if not scope.conversation_id.startswith('qq:group:'):return []
        ctx=self.life.life_access.bind(scope,session_id=root['session_id'],generation=root['generation'])
        rows=self.repo._all("SELECT id FROM events WHERE agent_id=? AND conversation_id=? AND redacted_at IS NULL "
            "AND kind IN ('message','delivered_segment') AND received_at>=? ORDER BY ingest_seq DESC LIMIT ?",
            (scope.agent_id,scope.conversation_id,self.life.clock()-43200,max(limit*4,16)))
        result=[]
        for row in rows:
            try:resource,body=self.life.life_access.load(ctx,row['id'])
            except RecallError:continue
            event=self.repo.get_event(row['id'])
            result.append(dict(ref=resource['resource_id'],source_version=resource['source_version'],
                actor_id=event.actor_id,speaker=str((event.payload.get('actor') or {}).get('display_name') or ''),
                occurred_at=event.occurred_at,source_kind=event.source_kind,
                text=event.text[:480],has_more=len(event.text)>480,read_kind='life_source'))
            if len(result)>=limit:break
        return list(reversed(result))

    def visible(self,scope,actor,root=None,*,offset=0,limit=12):
        ctx=self.life.life_access.bind(scope,**(dict(session_id=root['session_id'],generation=root['generation']) if root else {}))
        result=[];seen=set()
        for row in self.repo._all("SELECT * FROM person_profiles WHERE agent_id=? AND actor_id=? AND status='active' AND expires>? ORDER BY CASE field WHEN 'boundary' THEN 0 WHEN 'address' THEN 1 ELSE 2 END,created DESC,id",
                (scope.agent_id,str(actor),self.life.clock())):
            try:
                for ref,version in json.loads(row['sources']).items():
                    self.life.life_access.load(ctx,ref,version)
            except RecallError:
                continue
            key=(row['field'],row['topic'])
            if key in seen:continue
            seen.add(key)
            result.append(dict(ref=row['id'],actor_id=row['actor_id'],field=row['field'],topic=row['topic'],
                text=row['text'],origin=row['origin'],revision=row['revision'],expires=row['expires'],
                evidence_refs=list(json.loads(row['sources'])),evidence_versions=json.loads(row['sources']),
                authority='person_material_not_persona'))
            if len(result)>=offset+limit:
                break
        return result[offset:offset+limit]

    def project(self,scope,actors,root,query,budget):
        """Select existing visible entries with one shared optional allowance.

        Boundaries remain required even when they exceed the soft allowance.
        Source visibility and expiry are rechecked on every read; this projection
        neither generates text nor writes person facts.
        """
        from .resource_policy import estimate_tokens
        from .text import terms as tokenize
        terms=set(tokenize(query))
        rows={str(actor):self.visible(scope,str(actor),root,limit=10000) for actor in dict.fromkeys(actors)}
        selected={actor:[] for actor in rows}
        candidates=[]
        for order,(actor,items) in enumerate(rows.items()):
            for item in items:
                priority=0 if item['field']=='boundary' else 1 if item['field']=='address' else 2
                relevance=len(terms & set(tokenize(item['topic']+' '+item['text'])))
                candidates.append((priority,-relevance,order,item))
        for priority,_,_,item in sorted(candidates,key=lambda row:row[:3]):
            draft={actor:list(items) for actor,items in selected.items()}
            draft[item['actor_id']].append(item)
            if priority==0 or estimate_tokens(draft)<=budget:
                selected=draft
        details=dict(read_kind='person_profile',actor_ids=list(rows),page_size=12,
            more_available={actor:len(items)>len(selected[actor]) for actor,items in rows.items()},
            version=digest(rows),mandatory_overflow=estimate_tokens(selected)>budget)
        return selected,details

    def save(self,scope,p,root,call_key):
        actor=str(p.get('actor_id',''))
        field,topic=p.get('field'),p.get('topic_key','')
        origin=p.get('origin','interpretation')
        action=p.get('operation','save')
        text=p.get('text','')
        if (not re.fullmatch(r'[0-9]+',actor) or field not in self.FIELDS or
                not isinstance(topic,str) or not re.fullmatch(r'[a-z0-9_.-]{1,80}',topic) or
                origin not in {'explicit_statement','interpretation'} or action not in {'save','revoke'} or
                not isinstance(text,str) or not text.strip() or len(text)>240 or not call_key):
            raise SessionFailure('invalid_profile','画像需要真实 actor_id、field、稳定 topic_key、简短 text 和明确 origin')
        identity=stable_id('person-profile',scope.agent_id,call_key,str(self.repo.forget_epoch(scope.scope_id)))
        checksum=digest(p)
        prior=self.repo._one('SELECT operation_hash FROM person_profiles WHERE id=?',(identity,))
        if prior:
            if prior['operation_hash']!=checksum:raise SessionFailure('idempotency_conflict','画像调用编号冲突')
            return dict(profile_ref=identity,saved=True)
        sources=self.role._sources(scope,p,root)
        events=self.life.relationships.require_sources(scope,sources)
        # The platform actor, not a quoted name or mentioned QQ number, owns
        # self-reported preferences. Other people's claims remain interpretations.
        speakers={e.actor_id for e in events if e.kind=='message' and e.source_kind=='user_report'}
        if not speakers or origin=='explicit_statement' and speakers!={actor}:
            raise SessionFailure('actor_mismatch','本人陈述只能引用该人物自己的消息；转述须标为 interpretation')
        if not self.repo._one("SELECT 1 FROM events WHERE agent_id=? AND conversation_id=? AND actor_id=? AND source_kind='user_report' AND redacted_at IS NULL",(*scope.private_key,actor)):
            raise SessionFailure('actor_mismatch','此场景没有该人物的实际消息')
        ctx=self.life.life_access.bind(scope,session_id=root['session_id'],generation=root['generation'])
        refs={}
        for event in events:
            resource,_=self.life.life_access.load(ctx,event.id)
            refs[resource['resource_id']]=resource['source_version']
        old=None
        for candidate in self.repo._all("SELECT * FROM person_profiles WHERE agent_id=? AND actor_id=? AND field=? AND topic=? AND status='active' ORDER BY created DESC,id",
                (scope.agent_id,actor,field,topic)):
            if candidate['expires']<=self.life.clock():continue
            try:
                for ref,version in json.loads(candidate['sources']).items():
                    self.life.life_access.load(ctx,ref,version)
            except RecallError:
                continue
            old=candidate;break
        expected=p.get('base_revision',0)
        if type(expected) is not int or expected!=(old['revision'] if old else 0):
            raise SessionFailure('stale_context','画像版本已变化；先读取 person_profile')
        if old and old['origin']=='explicit_statement' and origin!='explicit_statement':
            raise SessionFailure('profile_authority','推测不能覆盖本人陈述')
        if old and origin=='explicit_statement':
            previous=[]
            for ref,version in json.loads(old['sources']).items():
                resource,_=self.life.life_access.load(ctx,ref,version)
                event=self.repo.get_event(resource['native_id']) if resource['kind']=='event' else None
                if event:previous.append(event.received_at)
            if previous and max(e.received_at for e in events)<=max(previous):
                raise SessionFailure('stale_profile_evidence','更正画像须引用更新的本人陈述，不能用旧消息覆盖新更正')
        if action=='revoke' and not old:raise SessionFailure('stale_context','没有可撤销的画像条目')
        revision=expected+1
        days=p.get('lifetime_days',90 if origin=='explicit_statement' else 7)
        if type(days) is not int or not 1<=days<=(365 if origin=='explicit_statement' else 7):
            raise SessionFailure('invalid_profile','本人陈述期限 1～365 天，推测最多 7 天')
        with self.repo.tx():
            if old:self.repo._exec("UPDATE person_profiles SET status='superseded' WHERE id=?",(old['id'],))
            self.repo._exec('INSERT INTO person_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity,scope.agent_id,actor,scope.scope_id,field,topic,text,origin,canonical(refs),revision,
                 'revoked' if action=='revoke' else 'active',self.life.clock(),self.life.clock()+days*86400,checksum))
            for event in events:
                if event.actor_id==actor and event.kind=='message' and event.source_kind=='user_report':
                    self.observe(event)
                    self.repo._exec("UPDATE person_encounters SET state='reviewed',reason='profile_saved',updated=? WHERE event_id=?",(self.life.clock(),event.id))
        return dict(profile_ref=identity,revision=revision,saved=True,origin=origin)
