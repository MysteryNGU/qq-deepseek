import asyncio
import json
from pathlib import Path
import time
import unittest
import uuid

from life.life_access import RecallError
from life.memory_admin import MemoryAdministration
from tests.role_fixture import Fixture


class MemoryAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.worker=self.f.role.memory_worker;self.worker.protocol=1
        self.admin=MemoryAdministration(self.worker)
        self.repo=self.f.life.repo
        self.admitted=[]
        self.f.bridge.session_input=lambda p:self.admitted.append(p) or dict(state='accepted',message_id='one')
        self.f.bridge.session_control=lambda *a,**kw:dict(state='stopped')
        self.f.bridge.resource_events=lambda *a,**kw:dict(records=[],cursor=0,has_more=False)
        self.f.bridge.input_status=lambda *a,**kw:dict(state='settled')

    def exclude(self):
        key='memory_review_exclusion:default:'+self.f.event.id
        with self.repo.tx():
            self.repo._exec('INSERT INTO meta VALUES(?,?)',(key,json.dumps(dict(
                event_ref=self.f.event.id,classification='waiting_repair'))))
        return key

    async def paused(self):
        await self.worker.tick()
        row=self.repo._one("SELECT * FROM work_jobs WHERE kind='memory_organize' AND status='running'")
        with self.repo.tx():
            self.repo._exec("UPDATE work_jobs SET status='quarantined',error='internal_lease_expired' WHERE id=?",(row['id'],))
        return row['id']

    async def test_repaired_source_gets_separate_audited_run_without_rewinding_history(self):
        key=self.exclude()
        with self.repo.tx():
            self.repo._exec("INSERT OR REPLACE INTO meta VALUES('memory_organize_cursor:default','777')")
        result=await self.admin.retry(self.f.event.id,uuid.uuid4().hex,repair=True)
        self.assertEqual(result['source_count'],1)
        self.assertEqual(len(self.admitted),1)
        self.assertFalse(self.repo._one('SELECT 1 FROM meta WHERE k=?',(key,)))
        self.assertEqual(self.repo._one("SELECT v FROM meta WHERE k='memory_organize_cursor:default'")['v'],'777')

    async def test_repair_does_not_override_deny_or_forget(self):
        self.exclude()
        self.f.life.life_access.set_rule(self.f.event.id,'internal_use','*','deny')
        with self.assertRaises(RecallError):
            await self.admin.retry(self.f.event.id,uuid.uuid4().hex,repair=True)
        self.repo.redact_events([self.f.event.id])
        with self.assertRaises(RecallError):
            await self.admin.retry(self.f.event.id,uuid.uuid4().hex,repair=True)
        self.assertFalse(self.admitted)

    async def test_operator_retry_requires_confirmed_stop_and_replaces_window_job(self):
        identity=await self.paused()
        self.f.bridge.session_control=lambda *a,**kw:dict(state='cancel_requested')
        with self.assertRaises(RecallError) as got:
            await self.admin.retry(identity,uuid.uuid4().hex)
        self.assertEqual(got.exception.code,'reader_stop_pending')
        self.assertEqual(len(self.admitted),1)
        self.f.bridge.session_control=lambda *a,**kw:dict(state='stopped')
        result=await self.admin.retry(identity,uuid.uuid4().hex)
        self.assertEqual(len(self.admitted),2)
        window=json.loads(self.repo._one("SELECT v FROM meta WHERE k='memory_review_window:default'")['v'])
        self.assertEqual(window['job_ref'],result['job_ref'])
        self.assertEqual(self.repo._one('SELECT status FROM work_jobs WHERE id=?',(identity,))['status'],'cancelled')

    async def test_true_unknown_cannot_be_operator_retried(self):
        identity=await self.paused()
        with self.repo.tx():
            self.repo._exec("UPDATE work_jobs SET status='unknown' WHERE id=?",(identity,))
        with self.assertRaises(RecallError) as got:
            await self.admin.retry(identity,uuid.uuid4().hex)
        self.assertEqual(got.exception.code,'explicit_retry_requires_paused_or_quarantined')
        self.assertEqual(len(self.admitted),1)

    async def test_park_retains_checkpoint_cost_and_unresolved_coverage(self):
        identity=await self.paused()
        before=dict(self.repo._one('SELECT * FROM work_jobs WHERE id=?',(identity,)))
        sid,_,_=self.admin.metadata(identity)
        with self.repo.tx():
            self.repo._exec("INSERT INTO spending_calls VALUES(?,?,?,100,NULL,'reserved','{}')",('park-cost',sid,'2026-10-26'))
        result=await self.admin.park(identity)
        self.assertEqual(result['coverage'],'unresolved')
        self.assertEqual(dict(self.repo._one('SELECT * FROM work_jobs WHERE id=?',(identity,))),before)
        self.assertEqual(self.worker.status()['parked_batches'],1)
        self.assertEqual(self.repo._one("SELECT state FROM spending_calls WHERE call_key='park-cost'")['state'],'reserved')
        self.assertEqual(len(self.admitted),1)
        self.assertNotIn('job_ref',json.loads(self.repo._one("SELECT v FROM meta WHERE k='memory_review_window:default'")['v']))

    async def test_park_needs_confirmed_stop_and_rejects_unknown_job(self):
        identity=await self.paused()
        self.f.bridge.session_control=lambda *a,**kw:dict(state='cancel_requested')
        with self.assertRaises(RecallError):await self.admin.park(identity)
        self.f.bridge.session_control=lambda *a,**kw:dict(state='stopped')
        with self.repo.tx():self.repo._exec("UPDATE work_jobs SET status='unknown' WHERE id=?",(identity,))
        with self.assertRaises(RecallError):await self.admin.park(identity)
        self.assertEqual(len(self.admitted),1)

    async def test_parked_retired_reader_does_not_rebind_and_new_window_can_run(self):
        identity=await self.paused()
        sid,metadata,_=self.admin.metadata(identity)
        await self.worker._retire(sid,metadata)
        self.f.bridge.session_control=lambda *a,**kw:dict(ok=False,code='stale_context')
        await self.admin.park(identity)
        await self.worker.tick()  # Finish the original scan window.
        event,_=self.f.life.ingest_message(self.f.scope,'下一小时的新经历。')
        with self.repo.tx():
            self.repo._exec("UPDATE meta SET v='0' WHERE k='memory_review_checked:default'")
        await self.worker.tick()
        self.assertEqual(len(self.admitted),2)
        window=json.loads(self.repo._one("SELECT v FROM meta WHERE k='memory_review_window:default'")['v'])
        _,new_metadata,_=self.admin.metadata(window['job_ref'])
        self.assertIn(self.f.life.catalog.resolve(event.id)['resource_id'],new_metadata['sources'])
        self.assertNotIn(self.f.life.catalog.resolve(self.f.event.id)['resource_id'],new_metadata['sources'])
        self.assertEqual(self.worker.status()['parked_batches'],1)

    async def test_operator_share_is_versioned_and_found_by_normal_recall(self):
        event,_=self.f.life.ingest_message(self.f.life.scope('qq:group:9','2'),'今天发现了一本星空图鉴。')
        resource=self.f.life.catalog.resolve(event.id)
        proposal=dict(source_ref=resource['resource_id'],source_version=resource['source_version'],
            classification='ordinary',category='everyday_event',uncertain=False,quotes=[event.text])
        result=self.admin.share(dict(proposal=proposal))
        self.assertTrue(result['owner_readable'])
        self.assertTrue(result['owner_recall_found'])
        proposal['source_version']+=1
        with self.assertRaises(RecallError):self.admin.share(dict(proposal=proposal))

    async def test_retry_keeps_unsettled_reservation_and_does_not_dispatch(self):
        identity=await self.paused()
        sid,_,_=self.admin.metadata(identity)
        with self.repo.tx():
            self.repo._exec("INSERT INTO spending_calls VALUES(?,?,?,100,NULL,'reserved','{}')",('unknown-cost',sid,'2026-10-26'))
        with self.assertRaises(RecallError) as got:
            await self.admin.retry(identity,uuid.uuid4().hex)
        self.assertEqual(got.exception.code,'unsettled_cost_requires_reconciliation')
        self.assertEqual(len(self.admitted),1)
        self.assertEqual(self.repo._one("SELECT state FROM spending_calls WHERE call_key='unknown-cost'")['state'],'reserved')

    async def test_reconcile_queries_receipts_without_resending_input(self):
        identity=await self.paused()
        result=await self.admin.reconcile(identity)
        self.assertEqual(result['inputs'][0]['state'],'settled')
        self.assertEqual(len(self.admitted),1)

    async def test_stopped_execution_with_unknown_input_still_cannot_retry(self):
        identity=await self.paused()
        self.f.bridge.input_status=lambda *a,**kw:dict(state='unknown')
        with self.assertRaises(RecallError) as got:
            await self.admin.retry(identity,uuid.uuid4().hex)
        self.assertEqual(got.exception.code,'input_outcome_requires_reconciliation')
        self.assertEqual(len(self.admitted),1)

    async def test_missing_supplier_usage_cannot_be_bypassed_by_absent_reservation(self):
        identity=await self.paused()
        sid,_,_=self.admin.metadata(identity)
        with self.repo.tx():
            self.repo._exec('INSERT INTO usage_calls VALUES(?,?,1,?)',('unknown-usage',json.dumps(dict(
                call_key='unknown-usage',session_id=sid,root_session_id=sid,status='unknown',finished_at=None)),time.time()))
        with self.assertRaises(RecallError) as got:
            await self.admin.retry(identity,uuid.uuid4().hex)
        self.assertEqual(got.exception.code,'unsettled_cost_requires_reconciliation')
        self.assertEqual(len(self.admitted),1)

    def request(self,**fields):
        identity=uuid.uuid4().hex
        self.admin.folder.mkdir(parents=True,exist_ok=True)
        request=dict(id=identity,agent_id='default',**fields)
        (self.admin.folder/(identity+'.request.json')).write_text(json.dumps(request),'utf-8')
        return identity

    async def test_duplicate_request_and_lost_local_response_never_dispatch_twice(self):
        self.exclude()
        identity=self.request(action='repair',target=self.f.event.id)
        await self.admin.poll()
        output=self.admin.folder/(identity+'.receipt.json')
        first=json.loads(output.read_text('utf-8'));self.assertTrue(first['ok'])
        output.unlink()
        await self.admin.poll()
        self.assertEqual(json.loads(output.read_text('utf-8')),first)
        self.assertEqual(len(self.admitted),1)

    async def test_crashed_started_request_is_unknown_and_not_automatically_repeated(self):
        self.exclude()
        identity=self.request(action='repair',target=self.f.event.id)
        with self.repo.tx():
            self.repo._exec('INSERT INTO meta VALUES(?,?)',('memory_admin_receipt:default:'+identity,
                json.dumps(dict(ok=False,code='operator_outcome_unknown'))))
        await self.admin.poll()
        self.assertFalse(self.admitted)
        got=json.loads((self.admin.folder/(identity+'.receipt.json')).read_text('utf-8'))
        self.assertEqual(got['code'],'operator_outcome_unknown')

    async def test_wrong_agent_request_cannot_dispatch(self):
        identity=self.request(action='repair',target=self.f.event.id)
        path=self.admin.folder/(identity+'.request.json')
        value=json.loads(path.read_text('utf-8'));value['agent_id']='other';path.write_text(json.dumps(value),'utf-8')
        await self.admin.poll()
        self.assertFalse(self.admitted)

    async def test_completed_requests_do_not_starve_later_commands(self):
        identities=[self.request(action='status') for _ in range(6)]
        await self.admin.poll()
        await self.admin.poll()
        self.assertTrue(all((self.admin.folder/(i+'.receipt.json')).exists() for i in identities))

    async def test_status_is_metadata_only(self):
        self.exclude()
        got=self.admin.status()
        self.assertNotIn(self.f.event.text,json.dumps(got))
        self.assertEqual(got['exclusions'][0]['classification'],'waiting_repair')

    async def test_live_history_probe_reports_qualification_without_returning_raw_text(self):
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('memory_policy_floor:'+self.f.scope.scope_id,
                str(self.f.event.received_at+1)))
        result=await self.admin.execute(dict(action='verify-history'))
        self.assertTrue(result['history_readable']);self.assertFalse(result['learning_eligible'])
        self.assertNotIn(self.f.event.text,json.dumps(result))

    async def test_qq_probe_uses_owned_expression_path_and_original_evidence(self):
        captured=[]
        async def send(scope,logical,proposal,sources):
            captured.append((scope,logical,sources));return dict(sent_confirmed=True)
        self.f.role.send=send
        identity=uuid.uuid4().hex
        result=await self.admin.execute(dict(action='verify-qq',id=identity))
        self.assertTrue(result['sent_confirmed'])
        scope,logical,sources=captured[0]
        self.assertEqual(scope.conversation_id,'qq:private:1')
        self.assertEqual(self.f.life.repo.get_event(sources[0]).kind,'maintenance_probe')

    async def test_slow_receipt_io_does_not_expire_the_live_lease(self):
        now=[self.f.life.clock()]
        self.f.life.clock=self.f.life.jobs.clock=lambda:now[0]
        await self.worker.tick()
        row=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")
        _,metadata,_=self.admin.metadata(row['id'])
        lease=self.worker._lease(metadata)
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow_replay(sid):
            entered.set();await release.wait()
        self.f.role.replay=slow_replay
        task=asyncio.create_task(self.worker.tick())
        await entered.wait()
        now[0]+=100
        self.worker.renew_active()
        now[0]+=100
        self.assertTrue(self.f.life.jobs.current(lease))
        self.worker.renew_active()
        release.set();await task
        self.assertEqual(len(self.admitted),1)

    async def test_heartbeat_never_reacquires_expired_or_timed_out_work(self):
        now=[self.f.life.clock()]
        self.f.life.clock=self.f.life.jobs.clock=lambda:now[0]
        await self.worker.tick()
        row=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")
        _,metadata,_=self.admin.metadata(row['id']);lease=self.worker._lease(metadata)
        now[0]+=self.worker.config.lease_seconds+1
        self.worker.renew_active()
        self.assertFalse(self.f.life.jobs.current(lease))
        now[0]=metadata['created']+self.worker.config.timeout_seconds+1
        with self.repo.tx():
            self.repo._exec('UPDATE work_jobs SET lease_until=? WHERE id=?',(now[0]+10,lease.id))
        self.worker.renew_active()
        self.assertEqual(self.repo._one('SELECT lease_until FROM work_jobs WHERE id=?',(lease.id,))['lease_until'],now[0]+10)

    async def test_internal_read_without_cursor_continues_confirmed_host_progress(self):
        self.f.life.ingest(self.f.scope,'observation','tool_observation','第二条需要独立读取的原始经历')
        await self.worker.tick()
        job=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")
        sid,_,root=self.admin.metadata(job['id'])
        async def read(identity):
            return await self.worker.callback(root,dict(kind='life.read',caller='root',session_id=sid,call_id=identity,
                proposal=dict(kind='memory_batch',limit=1)))
        first=await read('first');self.assertTrue(first['has_more'])
        repeated=await read('first');self.assertEqual(first,repeated)
        second=await read('second')
        self.assertNotEqual(first['items'][0]['resource_ref'],second['items'][0]['resource_ref'])
        self.assertFalse(second['has_more'])
        finished=await read('finished');self.assertFalse(finished['has_more']);self.assertEqual(finished['items'],[])

    async def test_operator_pause_does_not_claim_stopped_until_bridge_confirms(self):
        await self.worker.tick()
        job=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")['id']
        self.f.bridge.session_control=lambda *a,**kw:dict(state='cancel_requested')
        result=await self.admin.pause(job);self.assertEqual(result['state'],'stopping')
        self.assertEqual(self.repo._one('SELECT status FROM work_jobs WHERE id=?',(job,))['status'],'running')
        self.f.bridge.session_control=lambda *a,**kw:dict(state='stopped')
        result=await self.admin.pause(job);self.assertEqual(result['state'],'paused')
        self.assertEqual(self.repo._one('SELECT error FROM work_jobs WHERE id=?',(job,))['error'],'operator_paused')
        self.assertEqual(len(self.admitted),1)

    async def test_sdk_end_seed_does_not_hide_an_ended_internal_turn(self):
        await self.worker.tick()
        job=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")['id']
        sid,_,_=self.admin.metadata(job)
        with self.repo.tx():
            self.repo._exec('UPDATE work_jobs SET checkpoint=? WHERE id=?',
                (json.dumps(dict(pages_read=2,read_refs=['already-read-source'])),job))
        self.f.role.sessions.apply_event(sid,dict(seq=0,type='turn/end',data=dict(turn=1)))
        self.f.role.sessions.apply_event(sid,dict(seq=1,type='session/end-seed',data={}))
        await self.worker.tick()
        self.assertEqual(self.repo._one('SELECT status FROM work_jobs WHERE id=?',(job,))['status'],'paused')
        self.assertEqual(self.worker.status()['state'],'attention_required')
        progress=json.loads(self.repo._one('SELECT checkpoint FROM work_jobs WHERE id=?',(job,))['checkpoint'])
        self.assertEqual(progress['pages_read'],2)
        self.assertEqual(progress['read_refs'],['already-read-source'])

    async def test_source_validation_batches_durable_read_tracking(self):
        for number in range(12):
            self.f.life.ingest(self.f.scope,'observation','tool_observation',f'isolated evidence {number}')
        await self.worker.tick()
        job=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")['id']
        sid,metadata,root=self.admin.metadata(job)
        statements=[];self.repo.db.set_trace_callback(statements.append)
        try:
            await self.worker.callback(root,dict(kind='before_tool',tool='life_read',caller='root',session_id=sid))
        finally:
            self.repo.db.set_trace_callback(None)
        self.assertLessEqual(sum(s=='COMMIT' for s in statements),1)
        tracked=json.loads(self.repo._one('SELECT v FROM meta WHERE k=?',('life_read_session:default:'+sid,))['v'])
        self.assertTrue(set(metadata['sources']).issubset(tracked))

    async def test_memory_pages_fit_sdk_preview_without_hiding_source_refs(self):
        from life.resource_policy import estimate_tokens
        for number in range(20):
            self.f.life.ingest(self.f.scope,'observation','tool_observation',f'{number} '+('中文原始材料和引号"换行\n'*50))
        await self.worker.tick()
        job=self.repo._one("SELECT id FROM work_jobs WHERE kind='memory_organize' AND status='running'")['id']
        sid,metadata,root=self.admin.metadata(job)
        policy=self.f.role.resources
        seen=set()
        for page in range(self.worker.config.max_source_pages):
            result=await self.worker.callback(root,dict(kind='life.read',caller='root',session_id=sid,call_id=f'bounded-read-{page}',
                proposal=dict(kind='memory_batch')))
            self.assertLessEqual(estimate_tokens(result),policy.text_preview_target_tokens)
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()),policy.text_result_max_utf8_bytes)
            seen.update(item['resource_ref'] for item in result['items'] if not item['has_more'])
            if not result['has_more']:break
        self.assertFalse(result['has_more'])
        self.assertEqual(seen,set(metadata['sources']))

    def bill(self):
        self.f.role.spending.reserve_external('external',purpose='memory_query',maximum_nano=100,
            price_version='test-price',source_refs=[])
        return dict(invoice_id='invoice-1',provider='test-supplier',price_version='test-price',
                    actual_nano=50,finished_at=time.time())

    async def test_import_bill_requires_evidence_and_matching_price(self):
        bill=self.bill()
        for bad in [{},dict(bill,price_version='other'),dict(bill,actual_nano=-1),dict(bill,finished_at=float('nan'))]:
            with self.subTest(bill=bad),self.assertRaises(RecallError):
                self.admin.import_bill(dict(target='external',bill=bad))
        self.assertEqual(self.repo._one("SELECT state FROM spending_calls WHERE call_key='external'")['state'],'reserved')

    async def test_invoice_is_idempotent_and_conflicting_invoice_cannot_overwrite(self):
        bill=self.bill()
        self.admin.import_bill(dict(target='external',bill=bill))
        self.assertTrue(self.admin.import_bill(dict(target='external',bill=bill))['duplicate'])
        with self.assertRaises(RecallError):
            self.admin.import_bill(dict(target='external',bill=dict(bill,actual_nano=0)))
        row=self.repo._one("SELECT * FROM spending_calls WHERE call_key='external'")
        self.assertEqual(row['charged'],50)
        self.assertEqual(json.loads(row['payload'])['operator_bill'],bill)


if __name__=='__main__':
    unittest.main()
