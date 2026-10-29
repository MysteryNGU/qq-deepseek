"""Local operator requests, consumed by the existing memory maintenance loop.

There is no model tool or remote listener. Share requests may contain an
operator-selected ordinary quote; receipts contain references and status only.
"""
import asyncio
import json
import math
import re

from .agent_session import canonical
from .life_access import RecallError


class MemoryAdministration:
    def __init__(self, worker):
        self.worker=worker
        self.role,self.life,self.repo=worker.role,worker.life,worker.repo
        self.folder=self.role.root/'data'/'memory-admin'

    def status(self):
        prefix='memory_review_exclusion:'+self.life.agent_id+':'
        exclusions=[json.loads(r['v']) for r in self.repo._all(
            'SELECT v FROM meta WHERE substr(k,1,?)=?',(len(prefix),prefix))]
        jobs=[dict(r) for r in self.repo._all("SELECT id,status,error,attempts FROM work_jobs "
            "WHERE kind='memory_organize' AND scope_id=? AND status NOT IN ('done','cancelled')",
            (self.life.scope('__memory_internal__').scope_id,))]
        return dict(organize=self.worker.status(),exclusions=exclusions,jobs=jobs,
                    spending=self.role.spending.snapshot())

    def metadata(self, job_id):
        prefix=self.worker.PREFIX
        for row in self.repo._all('SELECT k,v FROM meta WHERE substr(k,1,?)=?',(len(prefix),prefix)):
            value=json.loads(row['v'])
            if value['job_id']==job_id:
                sid=row['k'][len(prefix):]
                binding=self.repo._one('SELECT * FROM agent_sessions WHERE session_id=?',(sid,))
                if binding and binding['scope_id'].startswith(self.life.agent_id+'|'):
                    return sid,value,dict(binding)
        raise RecallError('job_session_not_found')

    async def reconcile(self, target):
        sid,metadata,root=self.metadata(target)
        await self.role.replay_usage(sid,current=False)
        pending=[]
        for row in self.repo._all('SELECT input_id FROM agent_inputs WHERE session_id=? AND handled=0',(sid,)):
            result=await asyncio.to_thread(self.role.bridge.input_status,sid,row['input_id'])
            pending.append(dict(input_id=row['input_id'],state=result.get('state','unknown')))
        return dict(session_id=sid,inputs=pending,spending=self.role.spending.snapshot())

    async def pause(self,target):
        sid,metadata,root=self.metadata(target)
        job=self.repo._one('SELECT status,checkpoint FROM work_jobs WHERE id=?',(target,))
        if not job or job['status']!='running':
            raise RecallError('pause_requires_running_job')
        receipt=await asyncio.to_thread(self.role.bridge.session_control,sid,root['generation'])
        if receipt.get('state')!='stopped':
            return dict(state='stopping',job_ref=target)
        with self.repo.tx():
            lease=self.worker._lease(metadata)
            if not self.life.jobs.finish(lease,'paused',checkpoint=json.loads(job['checkpoint']),error='operator_paused'):
                raise RecallError('stale_source')
        await self.worker._retire(sid,metadata)
        return dict(state='paused',job_ref=target)

    async def park(self,target):
        """Explicitly release a scan window, retaining unresolved work and cost."""
        sid,metadata,root=self.metadata(target)
        key='memory_review_window:'+self.life.agent_id
        job=self.repo._one('SELECT status FROM work_jobs WHERE id=?',(target,))
        saved=self.repo._one('SELECT v FROM meta WHERE k=?',(key,))
        window=json.loads(saved['v']) if saved else {}
        if not job or job['status'] not in {'paused','quarantined'} or window.get('job_ref')!=target:
            raise RecallError('park_requires_blocking_paused_job')
        pending_stop=self.repo._one('SELECT 1 FROM meta WHERE k=?',('life_retire_reader:'+sid,))
        # Retired readers without a pending stop were already stopped. Rebinding
        # them to cancel again can fail the host's current-session fence.
        if root['status']!='retired' or pending_stop:
            stopped=await asyncio.to_thread(self.role.bridge.session_control,sid,root['generation'])
            if stopped.get('state')!='stopped':
                raise RecallError('reader_stop_pending')
            if pending_stop:
                raise RecallError('reader_stop_pending')  # Let the normal retirement drain complete.
        if not await self.worker._retire(sid,metadata):
            raise RecallError('reader_stop_pending')
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                ('memory_review_parked:'+self.life.agent_id+':'+target,canonical(dict(
                    job_ref=target,batch_through=window['batch_through'],
                    source_count=len(metadata['sources']),coverage='unresolved',
                    accounting='preserved_requires_reconciliation'))))
            cursor=self.repo._one('SELECT v FROM meta WHERE k=?',('memory_organize_cursor:'+self.life.agent_id,))
            # This is a scan cursor, not a claim of summary coverage.
            through=max(int(cursor['v']) if cursor else 0,window['batch_through'])
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                ('memory_organize_cursor:'+self.life.agent_id,str(through)))
            for field in ('job_ref','batch_through','error'):
                window.pop(field,None)
            self.repo._exec('UPDATE meta SET v=? WHERE k=?',(canonical(window),key))
        return dict(state='parked',job_ref=target,coverage='unresolved',scan_through=through)

    def share(self,request):
        context=self.life.life_access.bind(self.life.scope('__memory_internal__'),mode='internal')
        proposal=request.get('proposal',{})
        resource=self.life.catalog.resolve(proposal.get('source_ref',''))
        if not resource:
            raise RecallError('source_not_found')
        # The ordinary policy still checks source version, full surrounding text,
        # confidentiality and real delivery evidence. No direct ACL override.
        if proposal.get('source_version')!=resource['source_version']:
            raise RecallError('stale_source')
        ref=self.worker.sharing.prepare(context,dict(proposal,source_ref=resource['resource_id']),
            {resource['resource_id']:resource['source_version']})
        scope=self.life.scope('qq:private:'+str(self.role.cfg.owner),str(self.role.cfg.owner))
        audience=self.life.life_access.bind(scope)
        loaded,_=self.life.life_access.load(audience,ref)
        query=(proposal.get('quotes') or [''])[0]
        recalled=self.life.life_recall.search(audience,query,mode='text',limit=10)
        return dict(resource_ref=ref,owner_readable=bool(loaded),
            owner_recall_found=any(i['resource_ref']==ref for i in recalled['items']))

    async def retry(self, target, request_id, *, repair=False):
        worker=self.worker
        if worker.protocol!=1 or not worker.config.enabled:
            raise RecallError('protocol_missing')
        if worker._pending_internal_stops():
            raise RecallError('reader_stop_pending')
        if self.repo._one("SELECT 1 FROM work_jobs WHERE kind='memory_organize' AND scope_id=? AND status='running'",
                          (self.life.scope('__memory_internal__').scope_id,)):
            raise RecallError('organize_busy')
        if not self.role.spending.background_allowed():
            raise RecallError('budget_unavailable')
        context=self.life.life_access.bind(self.life.scope('__memory_internal__'),mode='internal')
        old=None
        if repair:
            key='memory_review_exclusion:'+self.life.agent_id+':'+target
            if not self.repo._one('SELECT 1 FROM meta WHERE k=?',(key,)):
                raise RecallError('exclusion_not_found')
            refs=[target]
        else:
            old=self.repo._one("SELECT * FROM work_jobs WHERE id=? AND kind='memory_organize' AND scope_id=?",
                (target,self.life.scope('__memory_internal__').scope_id))
            if not old or old['status'] not in {'paused','quarantined'}:
                raise RecallError('explicit_retry_requires_paused_or_quarantined')
            sid,metadata,root=self.metadata(target)
            stopped=await asyncio.to_thread(self.role.bridge.session_control,sid,root['generation'])
            if stopped.get('state')!='stopped':
                raise RecallError('reader_stop_pending')
            await self.role.replay_usage(sid,current=False)
            for item in self.repo._all('SELECT input_id FROM agent_inputs WHERE session_id=? AND handled=0',(sid,)):
                receipt=await asyncio.to_thread(self.role.bridge.input_status,sid,item['input_id'])
                if receipt.get('state') not in {'settled','not_found','rejected'}:
                    raise RecallError('input_outcome_requires_reconciliation')
            if self.repo._one("SELECT 1 FROM spending_calls WHERE session_id IN "
                    "(SELECT session_id FROM agent_sessions WHERE session_id=? OR parent_session_id=?) AND state='reserved'",(sid,sid)):
                raise RecallError('unsettled_cost_requires_reconciliation')
            from .spending import reported_cost
            for row in self.repo._all("SELECT payload FROM usage_calls WHERE json_extract(payload,'$.root_session_id')=? "
                    "OR json_extract(payload,'$.session_id')=?",(sid,sid)):
                usage=json.loads(row['payload'])
                if not usage.get('finished_at') or reported_cost(usage) is None:
                    raise RecallError('unsettled_cost_requires_reconciliation')
            refs=list(metadata['sources'])
            with self.repo.tx():
                self.repo._exec("UPDATE agent_sessions SET status='retired' WHERE session_id=?",(sid,))
        eligible,excluded=[],[]
        for ref in refs:
            resource=self.life.catalog.resolve(ref)
            if not resource or not self.life.catalog.live(resource):
                excluded.append(dict(resource_ref=ref,classification='deleted_or_stale'))
                continue
            if not self.life.life_access.internal_allowed(context,resource):
                excluded.append(dict(resource_ref=ref,classification='purpose_denied'))
                continue
            self.life.life_access.load(context,resource['resource_id'],resource['source_version'])
            eligible.append(resource['resource_id'])
        if not eligible:
            raise RecallError('no_eligible_sources')
        with self.repo.tx():
            packet=self.life.memory_organize.prepare(context,eligible,kind='period',
                topic_ref='operator:'+request_id,coverage_excluded=excluded)
            lease=self.life.jobs.claim(packet['job_ref'],ttl=worker.config.lease_seconds)
            if not lease:
                raise RecallError('organize_admission_unavailable')
            if old:
                self.repo._exec("UPDATE work_jobs SET status='cancelled',lease_token='',lease_until=0,error='operator_replaced' WHERE id=?",(target,))
                key='memory_review_window:'+self.life.agent_id
                saved=self.repo._one('SELECT v FROM meta WHERE k=?',(key,))
                window=json.loads(saved['v']) if saved else {}
                if window.get('job_ref')==target:
                    window.update(job_ref=lease.id)
                    window.pop('error',None)
                    self.repo._exec('UPDATE meta SET v=? WHERE k=?',(canonical(window),key))
            else:
                # Old manifests remain truthful; this is a separately audited
                # repair run, not a rewrite of the completed hourly coverage.
                self.repo._exec('DELETE FROM meta WHERE k=?',('memory_review_exclusion:'+self.life.agent_id+':'+target,))
        await worker.dispatch(lease)
        return dict(job_ref=lease.id,replaces=target if old else None,source_count=len(eligible),excluded_count=len(excluded))

    def import_bill(self, request):
        key=request.get('target')
        bill=request.get('bill',{})
        row=self.repo._one('SELECT * FROM spending_calls WHERE call_key=? AND session_id=?',
            (key,'memory:'+self.life.agent_id))
        if not row:
            raise RecallError('external_call_not_found')
        payload=json.loads(row['payload'])
        if (not isinstance(bill,dict) or not all(isinstance(bill.get(k),str) and bill[k].strip()
                for k in ('invoice_id','provider','price_version'))
                or bill['price_version']!=payload['price_version']
                or type(bill.get('actual_nano')) is not int or bill['actual_nano']<0
                or type(bill.get('finished_at')) not in (float,int) or not math.isfinite(bill['finished_at'])
                or bill['finished_at']<payload['started_at']):
            raise RecallError('verified_bill_required')
        with self.repo.tx():
            prior=payload.get('operator_bill')
            if prior and prior!=bill:
                raise RecallError('bill_conflict')
            if row['state']=='settled':
                if row['charged']!=bill['actual_nano']:
                    raise RecallError('bill_conflict')
                return dict(state='settled',duplicate=True)
            if payload.get('finished_at') is not None and abs(payload['finished_at']-bill['finished_at'])>1:
                raise RecallError('completion_time_conflict')
            payload.update(operator_bill=bill,finished_at=payload.get('finished_at',bill['finished_at']))
            self.repo._exec('UPDATE spending_calls SET payload=? WHERE call_key=?',(canonical(payload),key))
            self.role.spending.settle_external(key,bill['actual_nano'])
        return dict(state='settled',call_key=key)

    async def execute(self, request):
        action=request.get('action')
        if action=='verify-autonomy':
            from .persona_files import PersonaFiles
            from .agent_session import digest
            from .self_change import file_hash
            owner=self.life.scope('qq:private:'+str(self.role.cfg.owner),str(self.role.cfg.owner))
            group=self.life.scope('qq:group:'+str((getattr(self.role.cfg,'allow_groups',[]) or [0])[0]),'')
            other=self.life.scope('qq:private:0','0')
            files=PersonaFiles(self.role.root,self.role.cfg)
            hashes=[digest(self.role.stable_context(scope,{})) for scope in (owner,group,other)]
            return dict(persona_mode='unified' if files.unified else 'legacy',
                persona_path=files.private.relative_to(self.role.root).as_posix(),persona_hash=file_hash(files.private),
                all_scenes_same_static_context=len(set(hashes))==1,
                person_continuity=dict(same_person_group_recall=self.life.life_access.same_person_group_recall,
                    immediate_recall=self.role.people.verify_sync(owner),
                    review_enabled=getattr(self.role.cfg,'profile_review_enabled',False),
                    known_actors=self.repo._one("SELECT COUNT(DISTINCT p.actor_id) n FROM person_encounters p JOIN events e ON e.id=p.event_id WHERE p.agent_id=? AND e.redacted_at IS NULL",(self.life.agent_id,))['n'],
                    review_states=[dict(r) for r in self.repo._all("SELECT p.state,COUNT(*) n FROM person_encounters p JOIN events e ON e.id=p.event_id WHERE p.agent_id=? AND e.redacted_at IS NULL GROUP BY p.state",(self.life.agent_id,))]),
                idle_policy=dict(min_seconds=self.role.resources.owner_idle_min_seconds,max_seconds=self.role.resources.owner_idle_max_seconds,
                    starts='after_sequence_completion',pending=[dict(r) for r in self.repo._all(
                        "SELECT kind,state,due,plan_revision FROM role_wakes WHERE scope_id=? AND state IN ('pending','dispatching','waiting_resource')",(owner.scope_id,))],
                    awaiting_completion=bool(self.repo._one('SELECT 1 FROM meta WHERE k=?',('idle_after_sequence:'+owner.scope_id,)))),
                motivation=self.role.motivations.status(owner),profile_count=self.repo._one(
                    "SELECT COUNT(*) n FROM person_profiles WHERE agent_id=? AND status='active'",(self.life.agent_id,))['n'])
        if action=='verify-history':
            rows=self.repo._all("SELECT e.id FROM events e JOIN meta m ON m.k='memory_policy_floor:'||e.agent_id||'|'||e.conversation_id "
                "WHERE e.agent_id=? AND e.source_kind='user_report' AND e.redacted_at IS NULL AND e.received_at<CAST(m.v AS REAL) LIMIT 30",
                (self.life.agent_id,))
            for row in rows:
                event=self.repo.get_event(row['id'])
                ctx=self.life.life_access.bind(event.scope)
                try:
                    result=self.life.life_recall.read(ctx,event.id)
                except RecallError:
                    continue
                return dict(history_readable=True,learning_eligible=result['learning']['eligible'],
                    resource_ref=result['resource_ref'],read_scope=result['read_scope'])
            raise RecallError('eligible_old_history_sample_not_found')
        if action=='verify-qq':
            scope=self.life.scope('qq:private:'+str(self.role.cfg.owner),str(self.role.cfg.owner))
            text='新版记忆 R1～R7 修复已部署，本机检索和整理恢复验收中。'
            event,_=self.life.ingest(scope,'maintenance_probe','tool_observation',text,
                external_id='operator-probe:'+request['id'])
            return await self.role.send(scope,'operator-probe:'+request['id'],dict(text=text),[event.id])
        if action=='status':
            return self.status()
        if action=='retry':
            return await self.retry(request.get('target'),request['id'])
        if action=='repair':
            return await self.retry(request.get('target'),request['id'],repair=True)
        if action=='reconcile':
            return await self.reconcile(request.get('target'))
        if action=='pause':
            return await self.pause(request.get('target'))
        if action=='park':
            return await self.park(request.get('target'))
        if action=='share':
            return self.share(request)
        if action=='import-bill':
            return self.import_bill(request)
        raise RecallError('unknown_admin_action')

    async def poll(self):
        if not self.folder.is_dir():
            return
        processed=0
        for path in sorted(self.folder.glob('*.request.json')):
            identity=path.name.removesuffix('.request.json')
            if not re.fullmatch(r'[a-f0-9]{32}',identity):
                continue
            output=self.folder/(identity+'.receipt.json')
            if output.exists():
                continue
            if processed>=4:
                break
            processed+=1
            key='memory_admin_receipt:'+self.life.agent_id+':'+identity
            saved=self.repo._one('SELECT v FROM meta WHERE k=?',(key,))
            if saved:
                receipt=json.loads(saved['v'])
            else:
                try:
                    if path.stat().st_size>32768:
                        raise RecallError('request_too_large')
                    request=json.loads(path.read_text('utf-8'))
                    if request.get('id')!=identity or request.get('agent_id')!=self.life.agent_id:
                        raise RecallError('scope_mismatch')
                    # Survives a crash after dispatch and before its response.
                    # The same request is never automatically executed twice.
                    with self.repo.tx():
                        self.repo._exec('INSERT INTO meta VALUES(?,?)',
                            (key,canonical(dict(ok=False,code='operator_outcome_unknown',request_id=identity))))
                    receipt=dict(ok=True,result=await self.execute(request),request_id=identity)
                except Exception as error:
                    receipt=dict(ok=False,code=getattr(error,'code','admin_unavailable'),request_id=identity)
                with self.repo.tx():
                    self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,canonical(receipt)))
            temporary=output.with_suffix('.writing')
            temporary.write_text(canonical(receipt),'utf-8')
            temporary.replace(output)
